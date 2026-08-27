"""
Layer 2: turns (dependency graph + historical execution records + a
proposed commit diff) into ML-ready tabular feature vectors, one row
per (job, historical-run) pair for training, or one row per (job,) for
live inference on the current diff.

Enhanced features beyond the baseline scaffold:
  - ``author_failure_rate``   — per-author historical failure tendency
  - ``recent_streak``         — consecutive failures/successes for this job
  - ``loc_delta``             — lines-of-code changed (not just file count)
  - ``dep_graph_distance``    — shortest path from changed files to job
  - ``cache_key_stability``   — rolling hit-rate for the specific cache key
  - Rolling-window stats with exponential decay
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import networkx as nx
import pandas as pd

from gha_optimizer.graph.dependency_graph import (
    cache_keys_touched_by,
    graph_distance_to_job,
    impacted_jobs_for_diff,
)
from gha_optimizer.ingestion.github_client import WorkflowRunRecord


# ---------------------------------------------------------------------------
# Feature row definition
# ---------------------------------------------------------------------------

@dataclass
class JobFeatureRow:
    """One observation — either a historical training sample or a live inference row."""

    job_id: str
    run_id: str

    # File-change features
    files_changed_count: int
    loc_added: int = 0
    loc_deleted: int = 0

    # Structural features (from dependency graph)
    job_structurally_impacted: int = 0  # 1/0
    cache_key_touched: int = 0          # 1/0
    dep_graph_distance: float = 0.0     # min shortest-path distance

    # Historical features (per-job)
    historical_failure_rate: float = 0.0
    historical_mean_duration: float = 0.0
    recent_failure_streak: int = 0      # consecutive recent failures
    recent_success_streak: int = 0      # consecutive recent successes

    # Historical features (per-author)
    author_failure_rate: float = 0.0

    # Cache stability
    cache_key_stability: float = 0.0    # rolling cache hit rate

    # Temporal features
    hour_of_day: int = 12
    day_of_week: int = 0                # 0=Monday
    is_weekend: int = 0

    # Labels (training only)
    label_cache_hit: int | None = None
    label_failed: int | None = None
    label_duration: float | None = None


# ---------------------------------------------------------------------------
# Rolling-window statistics with exponential decay
# ---------------------------------------------------------------------------

def _exponential_decay_mean(
    values: list[float], decay: float = 0.95
) -> float:
    """
    Compute a recency-weighted mean with exponential decay.

    Most-recent values get weight 1.0, older values get weight decay^i.
    This adapts to recent repository behavior changes rather than being
    dominated by old history (addresses the Known Modeling Simplification
    documented in the README).
    """
    if not values:
        return 0.0
    total_weight = 0.0
    weighted_sum = 0.0
    for i, v in enumerate(reversed(values)):
        w = decay ** i
        weighted_sum += v * w
        total_weight += w
    return weighted_sum / total_weight if total_weight > 0 else 0.0


def _streak_count(outcomes: list[int], target: int) -> int:
    """Count the length of the most-recent consecutive streak of ``target``."""
    streak = 0
    for outcome in reversed(outcomes):
        if outcome == target:
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Training frame builder
# ---------------------------------------------------------------------------

def build_training_frame(
    g: nx.DiGraph, history: list[WorkflowRunRecord]
) -> pd.DataFrame:
    """
    Build a training dataframe from historical execution records.

    Uses exponential-decay rolling stats and per-author failure rates.
    """
    # Pre-compute per-job ordered outcomes
    by_job_failures: dict[str, list[int]] = defaultdict(list)
    by_job_durations: dict[str, list[float]] = defaultdict(list)
    by_job_cache_hits: dict[str, list[int]] = defaultdict(list)

    # Per-author stats
    by_author_failures: dict[str, list[int]] = defaultdict(list)

    for rec in history:
        failed = 1 if rec.conclusion == "failure" else 0
        by_job_failures[rec.job_id].append(failed)
        by_job_durations[rec.job_id].append(rec.duration_seconds)
        by_job_cache_hits[rec.job_id].append(int(rec.cache_hit))
        by_author_failures[rec.author].append(failed)

    rows: list[dict] = []
    for rec in history:
        impacted = impacted_jobs_for_diff(g, rec.files_changed)
        touched_keys = cache_keys_touched_by(g, rec.files_changed)

        fail_hist = by_job_failures[rec.job_id]
        dur_hist = by_job_durations[rec.job_id]
        cache_hist = by_job_cache_hits[rec.job_id]
        author_hist = by_author_failures[rec.author]

        # Min graph distance from any changed file to this job
        distances = [
            graph_distance_to_job(g, f, rec.job_id)
            for f in rec.files_changed
        ]
        valid_distances = [d for d in distances if d >= 0]
        min_distance = min(valid_distances) if valid_distances else -1.0

        # Parse temporal features
        hour = 12
        day_of_week = 0
        if "T" in rec.timestamp:
            try:
                hour = int(rec.timestamp.split("T")[1].split(":")[0])
            except (IndexError, ValueError):
                pass
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
                day_of_week = dt.weekday()
            except (ValueError, ImportError):
                pass

        row = JobFeatureRow(
            job_id=rec.job_id,
            run_id=rec.run_id,
            files_changed_count=len(rec.files_changed),
            loc_added=getattr(rec, "loc_added", 0),
            loc_deleted=getattr(rec, "loc_deleted", 0),
            job_structurally_impacted=int(rec.job_id in impacted),
            cache_key_touched=int(len(touched_keys) > 0),
            dep_graph_distance=min_distance,
            historical_failure_rate=_exponential_decay_mean(
                [float(x) for x in fail_hist]
            ),
            historical_mean_duration=_exponential_decay_mean(dur_hist),
            recent_failure_streak=_streak_count(fail_hist, 1),
            recent_success_streak=_streak_count(fail_hist, 0),
            author_failure_rate=_exponential_decay_mean(
                [float(x) for x in author_hist]
            ),
            cache_key_stability=_exponential_decay_mean(
                [float(x) for x in cache_hist]
            ),
            hour_of_day=hour,
            day_of_week=day_of_week,
            is_weekend=int(day_of_week >= 5),
            label_cache_hit=int(rec.cache_hit),
            label_failed=int(rec.conclusion == "failure"),
            label_duration=rec.duration_seconds,
        )
        rows.append(row.__dict__)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Inference frame builder
# ---------------------------------------------------------------------------

def build_inference_frame(
    g: nx.DiGraph,
    job_ids: list[str],
    changed_files: list[str],
    history_stats: dict[str, dict[str, float]],
    hour_of_day: int = 12,
    day_of_week: int = 0,
    author: str = "unknown",
    author_stats: dict[str, dict[str, float]] | None = None,
    loc_added: int = 0,
    loc_deleted: int = 0,
) -> pd.DataFrame:
    """
    Build a single-row-per-job frame for the *current* diff, to be scored
    by the trained models at pipeline-trigger time.

    ``history_stats``: ``{job_id: {"failure_rate", "mean_duration",
    "cache_miss_rate", "recent_failure_streak", "recent_success_streak",
    "cache_key_stability"}}``
    """
    impacted = impacted_jobs_for_diff(g, changed_files)
    touched_keys = cache_keys_touched_by(g, changed_files)

    author_stats = author_stats or {}

    rows = []
    for job_id in job_ids:
        stats = history_stats.get(
            job_id,
            {
                "failure_rate": 0.1,
                "mean_duration": 60.0,
                "cache_miss_rate": 0.15,
                "recent_failure_streak": 0,
                "recent_success_streak": 5,
                "cache_key_stability": 0.85,
            },
        )

        # Min graph distance
        distances = [graph_distance_to_job(g, f, job_id) for f in changed_files]
        valid_distances = [d for d in distances if d >= 0]
        min_distance = min(valid_distances) if valid_distances else -1.0

        a_stats = author_stats.get(
            author, {"failure_rate": 0.1}
        )

        rows.append(
            {
                "job_id": job_id,
                "files_changed_count": len(changed_files),
                "loc_added": loc_added,
                "loc_deleted": loc_deleted,
                "job_structurally_impacted": int(job_id in impacted),
                "cache_key_touched": int(len(touched_keys) > 0),
                "dep_graph_distance": min_distance,
                "historical_failure_rate": stats["failure_rate"],
                "historical_mean_duration": stats["mean_duration"],
                "recent_failure_streak": stats.get("recent_failure_streak", 0),
                "recent_success_streak": stats.get("recent_success_streak", 5),
                "author_failure_rate": a_stats.get("failure_rate", 0.1),
                "cache_key_stability": stats.get("cache_key_stability", 0.85),
                "hour_of_day": hour_of_day,
                "day_of_week": day_of_week,
                "is_weekend": int(day_of_week >= 5),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Feature column definitions
# ---------------------------------------------------------------------------

# Core features used by all tabular models
FEATURE_COLUMNS = [
    "files_changed_count",
    "loc_added",
    "loc_deleted",
    "job_structurally_impacted",
    "cache_key_touched",
    "dep_graph_distance",
    "historical_failure_rate",
    "historical_mean_duration",
    "recent_failure_streak",
    "recent_success_streak",
    "author_failure_rate",
    "cache_key_stability",
    "hour_of_day",
    "day_of_week",
    "is_weekend",
]

# Subset for models that don't need all features
CACHE_FEATURE_COLUMNS = [
    "files_changed_count",
    "loc_added",
    "loc_deleted",
    "cache_key_touched",
    "dep_graph_distance",
    "cache_key_stability",
    "historical_mean_duration",
]

FAILURE_FEATURE_COLUMNS = [
    "files_changed_count",
    "loc_added",
    "loc_deleted",
    "job_structurally_impacted",
    "dep_graph_distance",
    "historical_failure_rate",
    "recent_failure_streak",
    "recent_success_streak",
    "author_failure_rate",
    "hour_of_day",
    "day_of_week",
    "is_weekend",
]
