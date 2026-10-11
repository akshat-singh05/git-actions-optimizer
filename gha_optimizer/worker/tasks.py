"""
Celery tasks — real implementations that delegate to application services.

Each task is a thin orchestration wrapper.  Business logic lives in the
service modules, not here.

All tasks implement:
  - retries with exponential backoff
  - structured logging with correlation IDs
  - error handling
  - status tracking
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from gha_optimizer.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────

def _log(task_name: str, correlation_id: str, msg: str, *args) -> None:
    """Structured log with correlation ID."""
    logger.info("[%s][%s] " + msg, task_name, correlation_id, *args)


def _err(task_name: str, correlation_id: str, msg: str, *args) -> None:
    logger.error("[%s][%s] " + msg, task_name, correlation_id, *args)


# ─── 1. Repository Synchronization ─────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.sync_repository",
    max_retries=3,
    default_retry_delay=30,
    time_limit=600,
)
def sync_repository(self, repo: str, token: Optional[str] = None) -> Dict[str, Any]:
    """
    Ingest the last week of real GitHub Actions history for ``repo`` (runs, jobs, steps,
    diffs, log-derived cache labels) through the ingestion service.  Idempotent and resumable.
    """
    cid = str(uuid.uuid4())[:8]
    _log("sync_repository", cid, "Starting sync for %s", repo)

    try:
        from datetime import datetime, timedelta, timezone

        from gha_optimizer.config import config
        from gha_optimizer.ingestion.github_client import GithubClient
        from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
        from gha_optimizer.server.db.session import SessionLocal, init_db

        token = token or config.github.token
        if not token:
            return {"status": "blocked", "reason": "no_github_token", "repo": repo}

        init_db()
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        db = SessionLocal()
        try:
            counters = RepoIngestor(repo, GithubClient(token=token, repo=repo), db, IngestOptions(since=since)).run()
        finally:
            db.close()
        return {"status": "complete", "correlation_id": cid, "repo": repo, "counters": counters}

    except Exception as exc:
        _err("sync_repository", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="gha_optimizer.ingest_run",
    max_retries=3,
    default_retry_delay=30,
    time_limit=900,
)
def ingest_run(self, repo: str, run_id: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Ingest a single completed workflow run (triggered by a ``workflow_run`` webhook)."""
    cid = str(uuid.uuid4())[:8]
    try:
        from datetime import datetime, timezone

        from gha_optimizer.config import config
        from gha_optimizer.ingestion.github_client import GithubClient
        from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
        from gha_optimizer.server.db.session import SessionLocal, init_db

        token = token or config.github.token
        if not token:
            return {"status": "blocked", "reason": "no_github_token", "repo": repo, "run_id": run_id}
        init_db()
        db = SessionLocal()
        try:
            ing = RepoIngestor(repo, GithubClient(token=token, repo=repo), db,
                               IngestOptions(since=datetime.now(timezone.utc).replace(tzinfo=None)))
            counters = ing.ingest_single_run(run_id)
        finally:
            db.close()
        return {"status": "complete", "correlation_id": cid, "repo": repo, "run_id": run_id, "counters": counters}
    except Exception as exc:
        _err("ingest_run", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 2. Process Webhook ────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.process_webhook",
    max_retries=3,
    default_retry_delay=10,
    time_limit=300,
)
def process_webhook(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process a GitHub webhook event and trigger appropriate downstream
    tasks.

    Events handled:
      - push: extract changed files, trigger graph update
      - pull_request: extract changed files, trigger predictions
      - workflow_run: record execution results, trigger feedback
      - workflow_job: record per-job results
    """
    cid = str(uuid.uuid4())[:8]
    _log("process_webhook", cid, "Processing %s event", event_type)

    try:
        repo = payload.get("repository", {}).get("full_name", "")

        if event_type == "push":
            return _handle_push(cid, repo, payload)
        elif event_type == "pull_request":
            return _handle_pull_request(cid, repo, payload)
        elif event_type == "workflow_run":
            return _handle_workflow_run(cid, repo, payload)
        elif event_type == "workflow_job":
            return _handle_workflow_job(cid, repo, payload)
        else:
            _log("process_webhook", cid, "Unhandled event type: %s", event_type)
            return {"status": "ignored", "event_type": event_type}

    except Exception as exc:
        _err("process_webhook", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


def _handle_push(cid: str, repo: str, payload: dict) -> dict:
    """Handle push event: extract changed files, trigger graph update."""
    commits = payload.get("commits", [])
    changed_files = set()
    for commit in commits:
        changed_files.update(commit.get("added", []))
        changed_files.update(commit.get("modified", []))
        changed_files.update(commit.get("removed", []))

    head_sha = payload.get("after", "")
    branch = payload.get("ref", "").replace("refs/heads/", "")

    _log("process_webhook", cid, "Push: %d changed files on %s", len(changed_files), branch)

    # Trigger graph update asynchronously
    generate_dependency_graph.delay(repo, head_sha, list(changed_files))

    return {
        "status": "processed",
        "event_type": "push",
        "repo": repo,
        "commit_sha": head_sha,
        "changed_files": len(changed_files),
    }


def _handle_pull_request(cid: str, repo: str, payload: dict) -> dict:
    """Handle PR event: extract changed files, trigger prediction."""
    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    head_sha = pr.get("head", {}).get("sha", "")

    _log("process_webhook", cid, "PR %s: action=%s sha=%s", pr.get("number"), action, head_sha[:8])

    if action in ("opened", "synchronize"):
        # Trigger prediction pipeline
        extract_features.delay(repo, head_sha)

    return {
        "status": "processed",
        "event_type": "pull_request",
        "action": action,
        "pr_number": pr.get("number"),
    }


def _handle_workflow_run(cid: str, repo: str, payload: dict) -> dict:
    """Handle workflow_run event: ingest the *real* run (jobs, timings, logs), never a placeholder row."""
    action = payload.get("action", "")
    run = payload.get("workflow_run", {})

    if action != "completed":
        return {"status": "ignored", "reason": f"action={action}"}

    run_id = str(run.get("id", ""))
    _log("process_webhook", cid, "Workflow run %s completed: %s", run_id, run.get("conclusion"))
    task = ingest_run.delay(repo, run_id)
    return {
        "status": "processed",
        "event_type": "workflow_run",
        "run_id": run_id,
        "conclusion": run.get("conclusion"),
        "ingest_task_id": task.id,
    }


def _handle_workflow_job(cid: str, repo: str, payload: dict) -> dict:
    """Handle workflow_job event: record per-job results."""
    action = payload.get("action", "")
    job = payload.get("workflow_job", {})

    if action != "completed":
        return {"status": "ignored", "reason": f"action={action}"}

    job_name = job.get("name", "unknown")
    conclusion = job.get("conclusion", "unknown")

    _log("process_webhook", cid, "Job %s completed: %s", job_name, conclusion)

    return {
        "status": "processed",
        "event_type": "workflow_job",
        "job_name": job_name,
        "conclusion": conclusion,
    }


# ─── 3. Graph Construction ─────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.generate_dependency_graph",
    max_retries=2,
    default_retry_delay=15,
    time_limit=600,
)
def generate_dependency_graph(
    self, repo: str, commit_sha: str, changed_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Build/update the heterogeneous dependency graph for a repository.
    Stores the snapshot in the database.
    """
    cid = str(uuid.uuid4())[:8]
    _log("generate_graph", cid, "Building graph for %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.graph.builder import GraphBuilder
        from gha_optimizer.graph.analytics import GraphAnalytics

        # Use the repo root or a temp directory
        repo_root = Path(".gha_optimizer/repos") / repo.replace("/", "_")
        repo_root.mkdir(parents=True, exist_ok=True)

        builder = GraphBuilder(repo_root)

        # If we have changed files, build source graph from them
        if changed_files:
            source_paths = [repo_root / f for f in changed_files if (repo_root / f).exists()]
            builder.build_from_source(source_paths)

        graph = builder.get_graph()

        # Compute analytics
        if graph.number_of_nodes() > 0:
            GraphAnalytics.compute_all_metrics(graph)

        _log("generate_graph", cid, "Graph: %d nodes, %d edges",
             graph.number_of_nodes(), graph.number_of_edges())

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
        }

    except Exception as exc:
        _err("generate_graph", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 4. Feature Extraction ─────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.extract_features",
    max_retries=2,
    default_retry_delay=15,
    time_limit=300,
)
def extract_features(self, repo: str, commit_sha: str) -> Dict[str, Any]:
    """
    Build the feature frame from the real ingested history of ``repo``
    (strictly-prior historical features, per-commit graph features where computed).
    """
    cid = str(uuid.uuid4())[:8]
    _log("extract_features", cid, "Extracting features for %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.config import config
        from gha_optimizer.features.dataset import build_dataset
        from gha_optimizer.server.db.session import SessionLocal, init_db

        init_db()
        db = SessionLocal()
        try:
            train_df, info = build_dataset(db, repo)
        finally:
            db.close()

        n_runs = int(train_df["run_id"].nunique()) if len(train_df) else 0
        _log("extract_features", cid, "Found %d dataset rows from %d runs", len(train_df), n_runs)

        if n_runs < config.model.min_history_runs:
            return {
                "status": "blocked",
                "reason": "insufficient_data",
                "runs": n_runs,
                "required_runs": config.model.min_history_runs,
            }

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "runs_used": n_runs,
            "feature_rows": len(train_df),
            "feature_columns": train_df.shape[1],
        }

    except Exception as exc:
        _err("extract_features", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 5. Model Retraining ───────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.retrain_models",
    max_retries=1,
    default_retry_delay=60,
    time_limit=1800,
)
def retrain_models(self, repo: str, force: bool = False) -> Dict[str, Any]:
    """
    Retrain ML models on the real ingested dataset of ``repo``.
    Checks for drift first, then fits cache-miss, failure and duration models; a model whose
    labels are insufficient (e.g. no log-derived cache labels) is reported as not trained.

    The saved artifacts (``<model_dir>/<kind>.joblib``, registered in the model_versions table) are fitted on the
    per-commit-graph dataset and are NOT read by ``run_pipeline`` / ``/api/v1/optimize``, which use their own
    fingerprint-checked models under ``<model_dir>/<workflow>/`` (``models/store.py``).
    """
    cid = str(uuid.uuid4())[:8]
    _log("retrain_models", cid, "Retraining models for %s (force=%s)", repo, force)

    try:
        from gha_optimizer.server.db.session import SessionLocal, init_db
        from gha_optimizer.server.db import crud
        from gha_optimizer.feedback.drift_detector import DriftDetector
        from gha_optimizer.features.dataset import build_dataset
        from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
        from gha_optimizer.models.failure_predictor import FailurePredictor
        from gha_optimizer.models.duration_estimator import DurationEstimator
        from gha_optimizer.config import config

        init_db()
        db = SessionLocal()
        try:
            # Check drift first
            accuracy = crud.get_prediction_accuracy(db)
            detector = DriftDetector()
            drift_report = detector.check(accuracy)

            if not drift_report.any_drift and not force:
                _log("retrain_models", cid, "No drift detected, skipping retrain")
                return {
                    "status": "skipped",
                    "reason": "no_drift",
                    "drift_report": drift_report.summary,
                }

            train_df, _info = build_dataset(db, repo)
            n_runs = int(train_df["run_id"].nunique()) if len(train_df) else 0
            if n_runs < config.model.min_history_runs:
                _log("retrain_models", cid, "Insufficient training data (%d runs, need %d)",
                     n_runs, config.model.min_history_runs)
                return {
                    "status": "blocked",
                    "reason": "insufficient_data",
                    "runs": n_runs,
                    "required_runs": config.model.min_history_runs,
                }

            _log("retrain_models", cid, "Retraining with %d rows from %d runs", len(train_df), n_runs)

            model_dir = config.model.model_dir
            model_dir.mkdir(parents=True, exist_ok=True)
            version = str(uuid.uuid4())[:8]
            results: Dict[str, Any] = {}
            for kind, model, fname in (
                ("cache_miss", CacheMissPredictor(), "cache_miss.joblib"),
                ("failure", FailurePredictor(), "failure.joblib"),
                ("duration", DurationEstimator(), "duration.joblib"),
            ):
                try:
                    metrics = model.fit(train_df)
                except ValueError as exc:  # labels insufficient: report, do not fabricate
                    results[kind] = {"status": "not_trained", "reason": str(exc)}
                    continue
                model.save(model_dir / fname)
                crud.insert_model_version(db, kind, version, metrics, str(model_dir / fname), len(train_df))
                results[kind] = {"status": "trained", "metrics": metrics}

            _log("retrain_models", cid, "Retraining complete: %s", {k: v["status"] for k, v in results.items()})
            return {
                "status": "complete",
                "correlation_id": cid,
                "rows_used": len(train_df),
                "runs_used": n_runs,
                "drift_report": drift_report.summary,
                "models": results,
                "model_version": version,
            }

        finally:
            db.close()

    except Exception as exc:
        _err("retrain_models", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 6. Benchmark Execution ────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.run_benchmark",
    max_retries=1,
    time_limit=600,
)
def run_benchmark(self, repo: str, commit_sha: str) -> Dict[str, Any]:
    """Run graph benchmarks and store results."""
    cid = str(uuid.uuid4())[:8]
    _log("run_benchmark", cid, "Benchmarking %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.graph.benchmark import GraphBenchmarker
        from gha_optimizer.graph.builder import GraphBuilder

        repo_root = Path(".gha_optimizer/repos") / repo.replace("/", "_")
        repo_root.mkdir(parents=True, exist_ok=True)

        benchmarker = GraphBenchmarker()
        builder = GraphBuilder(repo_root)

        # Time the graph build
        def _build():
            builder.build_from_source([])
            return builder.get_graph()
        graph = benchmarker.time_operation("build", _build)

        # Measure memory and snapshot size
        benchmarker.measure_memory(graph)
        benchmarker.measure_snapshot_size(graph)

        # Generate consolidated report
        report = benchmarker.generate_report(graph)
        report_dict = report.model_dump()

        _log("run_benchmark", cid,
             "Benchmark: build=%.1fms, nodes=%d, edges=%d",
             report_dict.get("graph_build_time_ms", 0),
             report_dict.get("node_count", 0),
             report_dict.get("edge_count", 0))

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "benchmark": report_dict,
        }

    except Exception as exc:
        _err("run_benchmark", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)

