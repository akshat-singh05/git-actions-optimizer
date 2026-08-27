"""
SQLite-backed storage for workflow run history, predictions, and model
versions.  Designed to be upgradeable to PostgreSQL by changing the
connection string.

Uses plain ``sqlite3`` (no ORM) to keep dependencies minimal.
"""
from __future__ import annotations

import json
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "gha_optimizer.db"


class Database:
    """Lightweight SQLite database for the gha-optimizer server."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    commit_sha TEXT,
                    repo TEXT,
                    job_id TEXT NOT NULL,
                    conclusion TEXT,
                    duration_seconds REAL,
                    cache_hit INTEGER,
                    author TEXT,
                    branch TEXT,
                    runner_label TEXT,
                    files_changed TEXT,  -- JSON array
                    loc_added INTEGER DEFAULT 0,
                    loc_deleted INTEGER DEFAULT 0,
                    timestamp TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(run_id, job_id)
                );

                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    predicted_cache_miss REAL,
                    predicted_failure REAL,
                    predicted_duration REAL,
                    actual_cache_miss INTEGER,
                    actual_failure INTEGER,
                    actual_duration REAL,
                    model_version TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(run_id, job_id)
                );

                CREATE TABLE IF NOT EXISTS model_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_type TEXT NOT NULL,
                    version TEXT NOT NULL,
                    metrics TEXT,  -- JSON
                    artifact_path TEXT,
                    training_samples INTEGER,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS optimization_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT,
                    workflow_name TEXT,
                    baseline_wall_clock REAL,
                    optimized_wall_clock REAL,
                    baseline_cost REAL,
                    optimized_cost REAL,
                    baseline_energy REAL,
                    optimized_energy REAL,
                    jobs_total INTEGER,
                    jobs_skipped INTEGER,
                    plan_json TEXT,  -- full SchedulePlan as JSON
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_runs_repo
                    ON workflow_runs(repo, timestamp);
                CREATE INDEX IF NOT EXISTS idx_predictions_run
                    ON predictions(run_id);
                CREATE INDEX IF NOT EXISTS idx_models_active
                    ON model_versions(model_type, is_active);
                """
            )

    # ------------------------------------------------------------------
    # Workflow runs
    # ------------------------------------------------------------------

    def insert_run(self, record: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_runs
                    (run_id, commit_sha, repo, job_id, conclusion,
                     duration_seconds, cache_hit, author, branch,
                     runner_label, files_changed, loc_added, loc_deleted,
                     timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_id"],
                    record.get("commit_sha"),
                    record.get("repo"),
                    record["job_id"],
                    record.get("conclusion"),
                    record.get("duration_seconds"),
                    int(record.get("cache_hit", False)),
                    record.get("author"),
                    record.get("branch"),
                    record.get("runner_label"),
                    json.dumps(record.get("files_changed", [])),
                    record.get("loc_added", 0),
                    record.get("loc_deleted", 0),
                    record.get("timestamp"),
                ),
            )

    def get_recent_runs(
        self, repo: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if repo:
                rows = conn.execute(
                    "SELECT * FROM workflow_runs WHERE repo = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (repo, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM workflow_runs ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def insert_prediction(self, record: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO predictions
                    (run_id, job_id, predicted_cache_miss, predicted_failure,
                     predicted_duration, model_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["run_id"],
                    record["job_id"],
                    record.get("predicted_cache_miss"),
                    record.get("predicted_failure"),
                    record.get("predicted_duration"),
                    record.get("model_version"),
                ),
            )

    def update_prediction_actuals(
        self, run_id: str, job_id: str, actuals: dict[str, Any]
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE predictions SET
                    actual_cache_miss = ?,
                    actual_failure = ?,
                    actual_duration = ?
                WHERE run_id = ? AND job_id = ?
                """,
                (
                    actuals.get("actual_cache_miss"),
                    actuals.get("actual_failure"),
                    actuals.get("actual_duration"),
                    run_id,
                    job_id,
                ),
            )

    def get_prediction_accuracy(
        self, limit: int = 100
    ) -> dict[str, float]:
        """Compute prediction accuracy over recent predictions."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT predicted_cache_miss, actual_cache_miss,
                       predicted_failure, actual_failure,
                       predicted_duration, actual_duration
                FROM predictions
                WHERE actual_cache_miss IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

        if not rows:
            return {"cache_accuracy": 0.0, "failure_accuracy": 0.0, "duration_mae": 0.0}

        cache_correct = 0
        failure_correct = 0
        duration_errors = []
        total = len(rows)

        for r in rows:
            if r["actual_cache_miss"] is not None:
                pred = 1 if r["predicted_cache_miss"] >= 0.5 else 0
                if pred == r["actual_cache_miss"]:
                    cache_correct += 1
            if r["actual_failure"] is not None:
                pred = 1 if r["predicted_failure"] >= 0.5 else 0
                if pred == r["actual_failure"]:
                    failure_correct += 1
            if r["actual_duration"] is not None and r["predicted_duration"] is not None:
                duration_errors.append(
                    abs(r["actual_duration"] - r["predicted_duration"])
                )

        return {
            "cache_accuracy": cache_correct / total if total > 0 else 0.0,
            "failure_accuracy": failure_correct / total if total > 0 else 0.0,
            "duration_mae": (
                sum(duration_errors) / len(duration_errors)
                if duration_errors
                else 0.0
            ),
            "sample_count": total,
        }

    # ------------------------------------------------------------------
    # Model versions
    # ------------------------------------------------------------------

    def insert_model_version(
        self,
        model_type: str,
        version: str,
        metrics: dict,
        artifact_path: str,
        training_samples: int,
    ) -> None:
        with self._conn() as conn:
            # Deactivate previous versions
            conn.execute(
                "UPDATE model_versions SET is_active = 0 WHERE model_type = ?",
                (model_type,),
            )
            conn.execute(
                """
                INSERT INTO model_versions
                    (model_type, version, metrics, artifact_path,
                     training_samples, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    model_type,
                    version,
                    json.dumps(metrics),
                    artifact_path,
                    training_samples,
                ),
            )

    def get_active_model(self, model_type: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM model_versions WHERE model_type = ? "
                "AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
                (model_type,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Optimization results
    # ------------------------------------------------------------------

    def insert_optimization_result(self, result: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO optimization_results
                    (repo, workflow_name, baseline_wall_clock,
                     optimized_wall_clock, baseline_cost, optimized_cost,
                     baseline_energy, optimized_energy,
                     jobs_total, jobs_skipped, plan_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.get("repo"),
                    result.get("workflow_name"),
                    result.get("baseline_wall_clock"),
                    result.get("optimized_wall_clock"),
                    result.get("baseline_cost"),
                    result.get("optimized_cost"),
                    result.get("baseline_energy"),
                    result.get("optimized_energy"),
                    result.get("jobs_total"),
                    result.get("jobs_skipped"),
                    json.dumps(result.get("plan", {})),
                ),
            )

    def get_optimization_history(
        self, repo: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if repo:
                rows = conn.execute(
                    "SELECT * FROM optimization_results WHERE repo = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (repo, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM optimization_results "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]
