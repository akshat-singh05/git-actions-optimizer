"""
Layer 2: turns (dependency graph + real historical execution records + a proposed
commit diff) into ML-ready tabular feature vectors — one row per (job, historical
run) for training, one row per job for live inference on the current diff.

Leakage discipline (verified by ``tests/test_feature_leakage.py``)
------------------------------------------------------------------
Every *historical* feature — failure rate, mean duration, failure/success streak,
author failure rate, cache stability — is computed **only from runs strictly
earlier than the target run**.  Rows are processed in chronological order, grouped
by run, and the running state is updated *after* the run's rows are built, so a
row never sees its own outcome, its siblings in the same run, or the future.

Missing is missing: a job with no prior history gets ``NaN`` for its historical
features (the models accept NaN) — never an invented default such as "0.1 failure
rate" or "60 s".  Labels follow the same rule: a cancelled/skipped job has no
failure label, a job without a log-derived cache outcome has no cache label.

Features
--------
  files_changed_count, loc_added, loc_deleted   — GitHub commit/compare API
  job_structurally_impacted, cache_key_touched,
  dep_graph_distance                            — dependency graph of the repo at the run's commit
  historical_failure_rate, historical_mean_duration,
  recent_failure_streak, recent_success_streak  — prior runs of this (workflow, job)
  author_failure_rate                           — prior runs by the same actor
  cache_key_stability                           — prior log-derived hit rate of this job
  hour_of_day, day_of_week, is_weekend          — run created_at (UTC)
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import networkx as nx
import pandas as pd

from gha_optimizer.graph.dependency_graph import job_graph_features
from gha_optimizer.graph.models import NodeType
from gha_optimizer.ingestion.github_client import WorkflowRunRecord

NAN = float("nan")
FAILURE_CONCLUSIONS = ("failure", "timed_out")


# ---------------------------------------------------------------------------
# Feature row definition
# ---------------------------------------------------------------------------

@dataclass
class JobFeatureRow:
    """One observation — either a historical training sample or a live inference row."""

    job_id: str
    run_id: str

    # File-change features
    files_changed_count: float
    loc_added: float = NAN
    loc_deleted: float = NAN

    # Structural features (dependency graph at the run's commit)
    job_structurally_impacted: float = NAN
    cache_key_touched: float = NAN
    dep_graph_distance: float = NAN

    # Historical features (per-job, strictly prior runs)
    historical_failure_rate: float = NAN
    historical_mean_duration: float = NAN
    recent_failure_streak: float = NAN
    recent_success_streak: float = NAN

    # Historical features (per-author, strictly prior runs)
    author_failure_rate: float = NAN

    # Cache stability (strictly prior log-derived labels)
    cache_key_stability: float = NAN

    # Temporal features
    hour_of_day: float = NAN
    day_of_week: float = NAN
    is_weekend: float = NAN

    # Labels (training only; NaN = no label)
    label_cache_hit: float = NAN
    label_failed: float = NAN
    label_duration: float = NAN

    # Diagnostics (not model features)
    workflow_path: str | None = None
    timestamp: str | None = None
    event: str | None = None
    conclusion: str | None = None
    cache_outcome: str | None = None
    any_changed_file_in_graph: float = NAN


# ---------------------------------------------------------------------------
# Rolling statistics with exponential decay
# ---------------------------------------------------------------------------

def _exponential_decay_mean(values: list[float], decay: float = 0.95) -> float:
    """Recency-weighted mean: most recent value weight 1.0, older ``decay**i``."""
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
    """Length of the most-recent consecutive streak of ``target``."""
    streak = 0
    for outcome in reversed(outcomes):
        if outcome == target:
            streak += 1
        else:
            break
    return streak


def _failed_label(rec: WorkflowRunRecord) -> int | None:
    if rec.conclusion in FAILURE_CONCLUSIONS:
        return 1
    if rec.conclusion == "success":
        return 0
    return None  # cancelled / skipped / unknown carry no failure label


def _duration_label(rec: WorkflowRunRecord) -> float | None:
    if rec.conclusion in ("success", "failure") and rec.duration_seconds is not None:
        return float(rec.duration_seconds)
    return None


def _cache_label(rec: WorkflowRunRecord) -> int | None:
    return None if rec.cache_hit is None else int(bool(rec.cache_hit))


def _run_sort_key(rec: WorkflowRunRecord) -> tuple[str, int, str, str]:
    # job id last: rows of one run get a fixed order, so order-sensitive (decayed)
    # aggregates never depend on the arbitrary order jobs happened to be loaded in
    return (rec.timestamp or "", len(str(rec.run_id)), str(rec.run_id), rec.job_id)


class HistoryState:
    """Running per-(workflow, job) and per-author outcome history.

    ``*_features`` read the state; ``update_run`` writes it.  Call order in the
    training builder — read for every row of a run, *then* update with the run —
    is what makes the aggregates strictly prior.
    """

    def __init__(self, decay: float = 0.95):
        self.decay = decay
        self._fail: dict[tuple[str, str], list[int]] = defaultdict(list)
        self._dur: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._cache: dict[tuple[str, str], list[int]] = defaultdict(list)
        self._author: dict[str, list[int]] = defaultdict(list)

    @staticmethod
    def _key(workflow_path: str | None, job_id: str) -> tuple[str, str]:
        return (workflow_path or "", job_id)

    def job_features(self, workflow_path: str | None, job_id: str) -> dict[str, float]:
        k = self._key(workflow_path, job_id)
        fl, du, ca = self._fail.get(k, []), self._dur.get(k, []), self._cache.get(k, [])
        return {
            "historical_failure_rate": _exponential_decay_mean([float(x) for x in fl], self.decay) if fl else NAN,
            "historical_mean_duration": _exponential_decay_mean(du, self.decay) if du else NAN,
            "recent_failure_streak": float(_streak_count(fl, 1)) if fl else NAN,
            "recent_success_streak": float(_streak_count(fl, 0)) if fl else NAN,
            "cache_key_stability": _exponential_decay_mean([float(x) for x in ca], self.decay) if ca else NAN,
        }

    def author_failure_rate(self, author: str | None) -> float:
        hist = self._author.get(author) if author else None
        return _exponential_decay_mean([float(x) for x in hist], self.decay) if hist else NAN

    def update_run(self, recs: Iterable[WorkflowRunRecord]) -> None:
        for rec in recs:
            k = self._key(rec.workflow_path, rec.job_id)
            f = _failed_label(rec)
            if f is not None:
                self._fail[k].append(f)
                if rec.author:
                    self._author[rec.author].append(f)
            d = _duration_label(rec)
            if d is not None:
                self._dur[k].append(d)
            c = _cache_label(rec)
            if c is not None:
                self._cache[k].append(c)


def _group_runs(history: list[WorkflowRunRecord]) -> list[list[WorkflowRunRecord]]:
    """Records grouped by run, runs in chronological order."""
    groups: dict[str, list[WorkflowRunRecord]] = {}
    for rec in sorted(history, key=_run_sort_key):
        groups.setdefault(str(rec.run_id), []).append(rec)
    return sorted(groups.values(), key=lambda g: _run_sort_key(g[0]))


def _temporal(timestamp: str | None) -> tuple[float, float, float]:
    if not timestamp:
        return NAN, NAN, NAN
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return NAN, NAN, NAN
    dow = dt.weekday()
    return float(dt.hour), float(dow), float(dow >= 5)


def _none_to_nan(value: Any) -> float:
    return NAN if value is None else float(value)


# ---------------------------------------------------------------------------
# Training frame builder
# ---------------------------------------------------------------------------

def build_training_frame(
    g: nx.DiGraph,
    history: list[WorkflowRunRecord],
    decay: float = 0.95,
    use_static_graph: bool = False,
) -> pd.DataFrame:
    """
    Build the training frame from historical execution records (chronological rows).

    Structural features come from ``rec.graph_features`` — computed on the repository
    tree at that record's own commit.  A record without them gets NaN structural
    features; they are **not** derived from ``g`` (a graph of some other commit) unless
    ``use_static_graph=True`` is passed explicitly (unit tests / ad-hoc exploration).
    Historical features use only strictly earlier runs.
    """
    state = HistoryState(decay)
    static_cache: dict[tuple, dict[str, dict[str, float]]] = {}
    rows: list[dict[str, Any]] = []

    for run_recs in _group_runs(history):
        for rec in run_recs:
            struct = _structural(rec, g, static_cache, use_static_graph)
            jf = state.job_features(rec.workflow_path, rec.job_id)
            hour, dow, weekend = _temporal(rec.timestamp)
            failed, dur, cache = _failed_label(rec), _duration_label(rec), _cache_label(rec)
            row = JobFeatureRow(
                job_id=rec.job_id,
                run_id=str(rec.run_id),
                files_changed_count=float(len(rec.files_changed)),
                loc_added=_none_to_nan(rec.loc_added),
                loc_deleted=_none_to_nan(rec.loc_deleted),
                job_structurally_impacted=struct["job_structurally_impacted"],
                cache_key_touched=struct["cache_key_touched"],
                dep_graph_distance=struct["dep_graph_distance"],
                historical_failure_rate=jf["historical_failure_rate"],
                historical_mean_duration=jf["historical_mean_duration"],
                recent_failure_streak=jf["recent_failure_streak"],
                recent_success_streak=jf["recent_success_streak"],
                author_failure_rate=state.author_failure_rate(rec.author),
                cache_key_stability=jf["cache_key_stability"],
                hour_of_day=hour,
                day_of_week=dow,
                is_weekend=weekend,
                label_cache_hit=NAN if cache is None else float(cache),
                label_failed=NAN if failed is None else float(failed),
                label_duration=NAN if dur is None else dur,
                workflow_path=rec.workflow_path,
                timestamp=rec.timestamp,
                event=rec.event,
                conclusion=rec.conclusion,
                cache_outcome=rec.cache_outcome,
                any_changed_file_in_graph=struct["any_changed_file_in_graph"],
            )
            rows.append(row.__dict__)
        state.update_run(run_recs)  # only now may later runs see this run's outcomes

    return pd.DataFrame(rows)


def _structural(
    rec: WorkflowRunRecord,
    g: nx.DiGraph,
    cache: dict[tuple, dict[str, dict[str, float]]],
    use_static_graph: bool = False,
) -> dict[str, float]:
    gf = rec.graph_features
    if gf is not None:
        return {
            "job_structurally_impacted": _none_to_nan(gf.get("job_structurally_impacted")),
            "cache_key_touched": _none_to_nan(gf.get("cache_key_touched")),
            "dep_graph_distance": _none_to_nan(gf.get("dep_graph_distance")),
            "any_changed_file_in_graph": _none_to_nan(gf.get("any_changed_file_in_graph")),
        }
    if not use_static_graph:  # no per-commit graph features: unknown, not "whatever today's graph says"
        return {k: NAN for k in ("job_structurally_impacted", "cache_key_touched", "dep_graph_distance", "any_changed_file_in_graph")}
    files = tuple(sorted(rec.files_changed))
    if files not in cache:
        job_nodes = [
            n.split("::", 1)[1] for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_JOB.value
        ]
        cache[files] = job_graph_features(g, list(files), job_nodes)
    per_job = cache[files].get(rec.job_id)
    if per_job is None:  # job id is not a node of the static graph: structure is unknown
        return {k: NAN for k in ("job_structurally_impacted", "cache_key_touched", "dep_graph_distance", "any_changed_file_in_graph")}
    return per_job


# ---------------------------------------------------------------------------
# Inference-time statistics (state after ALL history == strictly prior to "now")
# ---------------------------------------------------------------------------

def compute_history_stats(
    history: list[WorkflowRunRecord], decay: float = 0.95, workflow_path: str | None = None
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """``(job_stats, author_stats)`` over the full history, for scoring a *new* run.

    ``job_stats[job_id]`` keys: failure_rate, mean_duration, recent_failure_streak,
    recent_success_streak, cache_key_stability (NaN where there is no prior evidence).
    """
    recs = [r for r in history if workflow_path is None or r.workflow_path in (None, workflow_path)]
    state = HistoryState(decay)
    for run_recs in _group_runs(recs):
        state.update_run(run_recs)
    job_stats: dict[str, dict[str, float]] = {}
    for rec in recs:
        if rec.job_id in job_stats:
            continue
        jf = state.job_features(rec.workflow_path, rec.job_id)
        job_stats[rec.job_id] = {
            "failure_rate": jf["historical_failure_rate"],
            "mean_duration": jf["historical_mean_duration"],
            "recent_failure_streak": jf["recent_failure_streak"],
            "recent_success_streak": jf["recent_success_streak"],
            "cache_key_stability": jf["cache_key_stability"],
        }
    author_stats = {
        a: {"failure_rate": state.author_failure_rate(a)} for a in {r.author for r in recs if r.author}
    }
    return job_stats, author_stats


# ---------------------------------------------------------------------------
# Inference frame builder
# ---------------------------------------------------------------------------

def build_inference_frame(
    g: nx.DiGraph,
    job_ids: list[str],
    changed_files: list[str],
    history_stats: dict[str, dict[str, float]],
    hour_of_day: float | None = None,
    day_of_week: float | None = None,
    author: str | None = None,
    author_stats: dict[str, dict[str, float]] | None = None,
    loc_added: float | None = None,
    loc_deleted: float | None = None,
) -> pd.DataFrame:
    """
    One row per job for the *current* diff.

    ``history_stats``: ``{job_id: {"failure_rate", "mean_duration", "recent_failure_streak",
    "recent_success_streak", "cache_key_stability"}}`` — see :func:`compute_history_stats`.
    A job without an entry (or a missing key) gets NaN, not a default.  Time features
    default to the real current UTC time.
    """
    now = datetime.now(timezone.utc)
    hour = float(now.hour) if hour_of_day is None else float(hour_of_day)
    dow = float(now.weekday()) if day_of_week is None else float(day_of_week)

    structural = job_graph_features(g, changed_files, job_ids)
    author_stats = author_stats or {}
    a_rate = author_stats.get(author, {}).get("failure_rate", NAN) if author else NAN

    rows = []
    for job_id in job_ids:
        stats = history_stats.get(job_id, {})
        sf = structural[job_id]
        rows.append(
            {
                "job_id": job_id,
                "files_changed_count": float(len(changed_files)),
                "loc_added": _none_to_nan(loc_added),
                "loc_deleted": _none_to_nan(loc_deleted),
                "job_structurally_impacted": sf["job_structurally_impacted"],
                "cache_key_touched": sf["cache_key_touched"],
                "dep_graph_distance": sf["dep_graph_distance"],
                "historical_failure_rate": stats.get("failure_rate", NAN),
                "historical_mean_duration": stats.get("mean_duration", NAN),
                "recent_failure_streak": stats.get("recent_failure_streak", NAN),
                "recent_success_streak": stats.get("recent_success_streak", NAN),
                "author_failure_rate": a_rate,
                "cache_key_stability": stats.get("cache_key_stability", NAN),
                "hour_of_day": hour,
                "day_of_week": dow,
                "is_weekend": float(dow >= 5),
                "any_changed_file_in_graph": sf["any_changed_file_in_graph"],
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
