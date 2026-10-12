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

import heapq
import re
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

    # Protocol-v2 features (docs/IMPROVEMENT_PLAN.md): run context, lineage, change content, job history.  All are known
    # at queue time and NaN when unknown.
    event_pull_request: float = NAN
    actor_is_bot: float = NAN
    run_attempt: float = NAN
    lineage_runs_so_far: float = NAN
    lineage_prev_job_failed: float = NAN
    lineage_job_fail_count: float = NAN
    lineage_prev_run_any_failure: float = NAN
    touches_tests: float = NAN
    touches_dependencies: float = NAN
    touches_ci_config: float = NAN
    docs_only: float = NAN
    n_top_level_dirs: float = NAN
    hours_since_last_failure: float = NAN
    failure_rate_last5: float = NAN
    job_prior_runs: float = NAN

    # Labels (training only; NaN = no label)
    label_cache_hit: float = NAN          # 1 = restored (exact OR restore-keys partial hit), 0 = miss
    label_cache_exact_miss: float = NAN   # 1 = exact key NOT restored (miss or partial hit), 0 = exact hit
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


def _cache_exact_miss_label(rec: WorkflowRunRecord) -> int | None:
    """1 when the job's exact cache key was not restored (a miss, or only a ``restore-keys`` prefix hit);
    0 for an exact hit; None when the outcome is unknown or unresolved (``hit_unresolved``)."""
    if rec.cache_outcome in ("miss", "partial_hit"):
        return 1
    if rec.cache_outcome == "exact_hit":
        return 0
    return None


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
        self._last_fail_epoch: dict[tuple[str, str], float] = {}   # when the latest visible failure became known

    @staticmethod
    def _key(workflow_path: str | None, job_id: str) -> tuple[str, str]:
        return (workflow_path or "", job_id)

    def job_features(self, workflow_path: str | None, job_id: str, now_epoch: float | None = None) -> dict[str, float]:
        k = self._key(workflow_path, job_id)
        fl, du, ca = self._fail.get(k, []), self._dur.get(k, []), self._cache.get(k, [])
        last_fail = self._last_fail_epoch.get(k)
        return {
            "historical_failure_rate": _exponential_decay_mean([float(x) for x in fl], self.decay) if fl else NAN,
            "historical_mean_duration": _exponential_decay_mean(du, self.decay) if du else NAN,
            "recent_failure_streak": float(_streak_count(fl, 1)) if fl else NAN,
            "recent_success_streak": float(_streak_count(fl, 0)) if fl else NAN,
            "cache_key_stability": _exponential_decay_mean([float(x) for x in ca], self.decay) if ca else NAN,
            "failure_rate_last5": float(sum(fl[-5:])) / len(fl[-5:]) if fl else NAN,
            "job_prior_runs": float(len(fl)) if fl else NAN,
            "hours_since_last_failure": (max(now_epoch - last_fail, 0.0) / 3600.0
                                         if last_fail is not None and now_epoch is not None else NAN),
        }

    def author_failure_rate(self, author: str | None) -> float:
        hist = self._author.get(author) if author else None
        return _exponential_decay_mean([float(x) for x in hist], self.decay) if hist else NAN

    def update_run(self, recs: Iterable[WorkflowRunRecord], visible_at: float | None = None) -> None:
        """``visible_at``: epoch seconds at which this run's outcome became known (its finish time)."""
        for rec in recs:
            k = self._key(rec.workflow_path, rec.job_id)
            f = _failed_label(rec)
            if f is not None:
                self._fail[k].append(f)
                if f == 1 and visible_at is not None:
                    self._last_fail_epoch[k] = visible_at
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


def _epoch(timestamp: str | None) -> float | None:
    """Seconds since the epoch of an ISO timestamp (UTC), or None."""
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


# ---------------------------------------------------------------------------
# Protocol-v2 features (docs/IMPROVEMENT_PLAN.md): run context, lineage, change content
# ---------------------------------------------------------------------------

_BOT_NAMES = {"dependabot", "renovate", "github-actions", "pre-commit-ci", "copilot"}
_TEST_PATH = re.compile(r"(^|/)(tests?|spec|specs|__tests__|testing)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")
_DEP_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "pipfile", "pipfile.lock", "uv.lock", "package.json",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.mod", "go.sum", "pom.xml", "cargo.toml", "cargo.lock",
    "gemfile", "gemfile.lock", "build.gradle", "build.gradle.kts", "composer.json", "composer.lock",
}
_CI_PREFIXES = (".github/", ".circleci/", ".gitlab-ci", "dockerfile", "makefile", "tox.ini", "noxfile.py", ".pre-commit")
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")


def _is_dep_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in _DEP_FILES or (name.startswith("requirements") and name.endswith(".txt"))


def change_content_features(files: list[str]) -> dict[str, float]:
    """What kind of change this is, from file names only (known the moment the commit exists)."""
    low = [f.lower() for f in files]
    return {
        "touches_tests": float(any(_TEST_PATH.search(f) for f in low)),
        "touches_dependencies": float(any(_is_dep_file(f) for f in low)),
        "touches_ci_config": float(any(f.startswith(_CI_PREFIXES) or f.rsplit("/", 1)[-1].startswith(_CI_PREFIXES)
                                       for f in low)),
        "docs_only": float(bool(low) and all(f.startswith(("docs/", "doc/")) or f.endswith(_DOC_SUFFIXES) for f in low)),
        "n_top_level_dirs": float(len({f.split("/", 1)[0] if "/" in f else "." for f in low})),
    }


def run_context_features(rec: WorkflowRunRecord) -> dict[str, float]:
    author = (rec.author or "").lower()
    return {
        "event_pull_request": NAN if rec.event is None else float(rec.event in ("pull_request", "pull_request_target")),
        "actor_is_bot": NAN if not author else float(author.endswith("[bot]") or author.endswith("-bot")
                                                       or author in _BOT_NAMES),
        "run_attempt": _none_to_nan(rec.run_attempt),
    }


LINEAGE_WINDOW = 5


class LineageState:
    """Outcomes of the runs of one *lineage* - a pull request (else branch + author; pushes: the branch) - that are
    already visible.  A run is added with :meth:`update_run` only once its outcome is known."""

    def __init__(self) -> None:
        self._runs: dict[str, int] = defaultdict(int)
        self._job_recent: dict[tuple[str, str], list[int]] = defaultdict(list)   # last LINEAGE_WINDOW outcomes
        self._run_any_failure: dict[str, int] = {}

    @staticmethod
    def key(rec: WorkflowRunRecord) -> str:
        if rec.pr_number:
            return f"pr:{rec.pr_number}"
        if rec.event == "push":
            return f"push:{rec.branch}"
        return f"b:{rec.event}:{rec.branch}:{rec.author}"

    def features(self, rec: WorkflowRunRecord) -> dict[str, float]:
        lk = self.key(rec)
        jk = (lk, rec.job_id)
        seen = self._runs.get(lk, 0)
        recent = self._job_recent.get(jk, [])
        # bounded on purpose: an unbounded count along a long-lived lineage (all pushes to main) is a clock
        return {
            "lineage_runs_so_far": float(min(seen, LINEAGE_WINDOW * 4)),
            "lineage_prev_job_failed": float(recent[-1]) if recent else NAN,
            "lineage_job_fail_count": float(sum(recent)) if seen else NAN,
            "lineage_prev_run_any_failure": float(self._run_any_failure[lk]) if lk in self._run_any_failure else NAN,
        }

    def update_run(self, recs: list[WorkflowRunRecord]) -> None:
        if not recs:
            return
        lk = self.key(recs[0])
        self._runs[lk] += 1
        any_failure, labelled = 0, False
        for rec in recs:
            f = _failed_label(rec)
            if f is None:
                continue
            labelled = True
            hist = self._job_recent[(lk, rec.job_id)]
            hist.append(f)
            del hist[:-LINEAGE_WINDOW]
            if f:
                any_failure = 1
        if labelled:
            self._run_any_failure[lk] = any_failure


# ---------------------------------------------------------------------------
# Training frame builder
# ---------------------------------------------------------------------------

def build_training_frame(
    g: nx.DiGraph,
    history: list[WorkflowRunRecord],
    decay: float = 0.95,
    use_static_graph: bool = False,
    completion_aware: bool = True,
) -> pd.DataFrame:
    """
    Build the training frame from historical execution records (chronological rows).

    ``completion_aware`` (default, "protocol v2"): a run's outcome enters the history only once that run has *finished*
    before the target run started (``finished_at``; unknown finish = known immediately, the old behaviour).  With
    ``False`` every earlier-started run is visible even if it was still running - the "protocol v1" the first results
    were produced with.

    Structural features come from ``rec.graph_features`` — computed on the repository
    tree at that record's own commit.  A record without them gets NaN structural
    features; they are **not** derived from ``g`` (a graph of some other commit) unless
    ``use_static_graph=True`` is passed explicitly (unit tests / ad-hoc exploration).
    Historical features use only strictly earlier runs.
    """
    state = HistoryState(decay)
    lineage = LineageState()
    static_cache: dict[tuple, dict[str, dict[str, float]]] = {}
    rows: list[dict[str, Any]] = []
    pending: list[tuple[float, int, float, list[WorkflowRunRecord]]] = []   # (visible at, order, finish, run)

    def reveal(upto: float | None) -> None:
        """Move every pending run that has finished by ``upto`` into the visible history (all of them if None)."""
        while pending and (upto is None or pending[0][0] <= upto):
            _, _, finish, recs = heapq.heappop(pending)
            state.update_run(recs, finish)
            lineage.update_run(recs)

    for order, run_recs in enumerate(_group_runs(history)):
        start = _epoch(run_recs[0].timestamp)
        if completion_aware:
            reveal(start)
        for rec in run_recs:
            struct = _structural(rec, g, static_cache, use_static_graph)
            jf = state.job_features(rec.workflow_path, rec.job_id, start)
            lf = lineage.features(rec)
            ctx = run_context_features(rec)
            chg = change_content_features(rec.files_changed)
            hour, dow, weekend = _temporal(rec.timestamp)
            failed, dur, cache = _failed_label(rec), _duration_label(rec), _cache_label(rec)
            exact = _cache_exact_miss_label(rec)
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
                **ctx,
                **lf,
                **chg,
                hours_since_last_failure=jf["hours_since_last_failure"],
                failure_rate_last5=jf["failure_rate_last5"],
                job_prior_runs=jf["job_prior_runs"],
                label_cache_hit=NAN if cache is None else float(cache),
                label_cache_exact_miss=NAN if exact is None else float(exact),
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
        # only now may later runs see this run's outcomes - and (completion-aware) only once it has finished
        finish = _epoch(run_recs[0].finished_at) if completion_aware else None
        visible_at = finish if finish is not None and start is not None and finish > start else (start or 0.0)
        heapq.heappush(pending, (visible_at, order, visible_at, run_recs))
        if not completion_aware:
            reveal(None)

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
        state.update_run(run_recs, _epoch(run_recs[0].finished_at) or _epoch(run_recs[0].timestamp))
    now_epoch = datetime.now(timezone.utc).timestamp()
    job_stats: dict[str, dict[str, float]] = {}
    for rec in recs:
        if rec.job_id in job_stats:
            continue
        jf = state.job_features(rec.workflow_path, rec.job_id, now_epoch)
        job_stats[rec.job_id] = {
            "failure_rate": jf["historical_failure_rate"],
            "mean_duration": jf["historical_mean_duration"],
            "recent_failure_streak": jf["recent_failure_streak"],
            "recent_success_streak": jf["recent_success_streak"],
            "cache_key_stability": jf["cache_key_stability"],
            "failure_rate_last5": jf["failure_rate_last5"],
            "job_prior_runs": jf["job_prior_runs"],
            "hours_since_last_failure": jf["hours_since_last_failure"],
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
    context: dict[str, float] | None = None,
    lineage_stats: dict[str, dict[str, float]] | None = None,
) -> pd.DataFrame:
    """
    One row per job for the *current* diff.

    ``context``: run-level values known for a real pushed run (``event_pull_request``, ``actor_is_bot``, ``run_attempt``,
    ``lineage_runs_so_far``, ``lineage_prev_run_any_failure``); ``lineage_stats[job_id]``: ``lineage_prev_job_failed``,
    ``lineage_job_fail_count``.  For a hypothetical diff (``analyze``) none of these is known and they stay NaN.

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

    ctx = context or {}
    lstats = lineage_stats or {}
    change = change_content_features(changed_files)
    rows = []
    for job_id in job_ids:
        stats = history_stats.get(job_id, {})
        sf = structural[job_id]
        ls = lstats.get(job_id, {})
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
                "event_pull_request": ctx.get("event_pull_request", NAN),
                "actor_is_bot": ctx.get("actor_is_bot", NAN),
                "run_attempt": ctx.get("run_attempt", NAN),
                "lineage_runs_so_far": ctx.get("lineage_runs_so_far", NAN),
                "lineage_prev_run_any_failure": ctx.get("lineage_prev_run_any_failure", NAN),
                "lineage_prev_job_failed": ls.get("lineage_prev_job_failed", NAN),
                "lineage_job_fail_count": ls.get("lineage_job_fail_count", NAN),
                **change,
                "hours_since_last_failure": stats.get("hours_since_last_failure", NAN),
                "failure_rate_last5": stats.get("failure_rate_last5", NAN),
                "job_prior_runs": stats.get("job_prior_runs", NAN),
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

# Protocol-v2 additions (docs/IMPROVEMENT_PLAN.md); used only by the failure model's v2 candidates
V2_FAILURE_COLUMNS = [
    "event_pull_request",
    "actor_is_bot",
    "run_attempt",
    "lineage_runs_so_far",
    "lineage_prev_job_failed",
    "lineage_job_fail_count",
    "lineage_prev_run_any_failure",
    "touches_tests",
    "touches_dependencies",
    "touches_ci_config",
    "docs_only",
    "n_top_level_dirs",
    "hours_since_last_failure",
    "failure_rate_last5",
    "job_prior_runs",
]
FAILURE_V2_COLUMNS = FEATURE_COLUMNS + V2_FAILURE_COLUMNS

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
