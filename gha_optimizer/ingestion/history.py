"""
Load real ingested history as :class:`WorkflowRunRecord` rows — one per
``(run, workflow job key)`` — and refuse to predict when there is not enough.

Matrix cells (several API jobs sharing one YAML key) are aggregated to the node
the scheduler actually schedules:

* conclusion : failure if any cell failed/timed out; else cancelled if any was
  cancelled; else skipped if all were skipped; else success
* duration   : the **slowest** executed cell (cells run in parallel), only for
  success/failure rows, otherwise ``None``
* cache_hit  : over the cells that have a log-derived label — any miss -> False,
  all hits -> True, no labelled cell -> ``None``

Cell-level detail stays in ``workflow_jobs`` untouched.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.ingestion.github_client import WorkflowRunRecord
from gha_optimizer.server.db import models as m

FAILURE_CONCLUSIONS = ("failure", "timed_out")
_OUTCOME_RANK = {"miss": 3, "partial_hit": 2, "hit_unresolved": 1, "exact_hit": 0}


class InsufficientHistoryError(RuntimeError):
    """Raised instead of predicting from nothing (no synthetic fallback, no defaults)."""

    def __init__(self, reason: str, **details: Any):
        self.reason = reason
        self.details = details
        extra = ", ".join(f"{k}={v}" for k, v in details.items())
        super().__init__(f"insufficient ingested history: {reason}" + (f" ({extra})" if extra else ""))


@dataclass
class _Agg:
    conclusion: str | None
    duration: float | None
    cache_hit: bool | None
    cache_outcome: str | None


def aggregate_cells(cells: Iterable[m.WorkflowJob]) -> _Agg:
    cells = list(cells)
    conclusions = {c.conclusion for c in cells if c.conclusion}
    executed = conclusions - {"skipped"}
    if any(x in conclusions for x in FAILURE_CONCLUSIONS):
        conclusion: str | None = "failure"
    elif "cancelled" in conclusions:
        conclusion = "cancelled"
    elif conclusions and not executed:
        conclusion = "skipped"
    elif executed == {"success"}:
        conclusion = "success"
    elif executed:
        conclusion = sorted(executed)[0]
    else:
        conclusion = None

    duration: float | None = None
    if conclusion in ("success", "failure"):
        durations = [c.duration_seconds for c in cells if c.duration_seconds is not None and c.conclusion != "skipped"]
        duration = max(durations) if durations else None

    labelled = [c for c in cells if c.cache_hit is not None]
    cache_hit: bool | None
    outcome: str | None
    if not labelled:
        cache_hit, outcome = None, None
    else:
        cache_hit = all(c.cache_hit for c in labelled)
        outcomes = [c.cache_outcome for c in labelled if c.cache_outcome]
        outcome = max(outcomes, key=lambda o: _OUTCOME_RANK.get(o, -1)) if outcomes else None
    return _Agg(conclusion, duration, cache_hit, outcome)


def load_history(
    db: Session,
    repo: str,
    workflow_path: str | None = None,
    since: datetime | None = None,
    events: Iterable[str] | None = None,
) -> list[WorkflowRunRecord]:
    """Real ``(run, job key)`` records for ``repo`` (optionally one workflow), oldest first.

    Raises nothing for an empty result; callers decide (see :func:`require_history`).
    """
    runs_q = db.query(m.WorkflowRun).filter(m.WorkflowRun.repo == repo, m.WorkflowRun.status == "completed",
                                            m.WorkflowRun.jobs_fetched_at.isnot(None))
    if workflow_path:
        runs_q = runs_q.filter(m.WorkflowRun.workflow_path == workflow_path)
    if since is not None:
        runs_q = runs_q.filter(m.WorkflowRun.gh_created_at >= since)
    if events:
        runs_q = runs_q.filter(m.WorkflowRun.event.in_(list(events)))
    runs = {r.run_id: r for r in runs_q.all()}
    if not runs:
        return []

    cells_by_key: dict[tuple[str, str], list[m.WorkflowJob]] = defaultdict(list)
    run_ids = list(runs)
    for i in range(0, len(run_ids), 500):  # keep the IN clause modest
        chunk = run_ids[i:i + 500]
        for job in db.query(m.WorkflowJob).filter(m.WorkflowJob.run_id.in_(chunk), m.WorkflowJob.job_key.isnot(None)).all():
            cells_by_key[(job.run_id, job.job_key)].append(job)

    files: dict[str, list[str]] = defaultdict(list)
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i:i + 500]
        for rid, fname in db.query(m.RunChangedFile.run_id, m.RunChangedFile.filename).filter(
            m.RunChangedFile.run_id.in_(chunk)
        ).all():
            files[rid].append(fname)

    gf: dict[tuple[str, str], m.RunGraphFeatures] = {}
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i:i + 500]
        for row in db.query(m.RunGraphFeatures).filter(
            m.RunGraphFeatures.run_id.in_(chunk), m.RunGraphFeatures.feature_version == GRAPH_FEATURE_VERSION
        ).all():  # rows of an older feature definition are unknown, not "current but zero"
            gf[(row.run_id, row.job_key)] = row

    records: list[WorkflowRunRecord] = []
    for (run_id, job_key), cells in cells_by_key.items():
        run = runs[run_id]
        agg = aggregate_cells(cells)
        feat = gf.get((run_id, job_key))
        records.append(
            WorkflowRunRecord(
                run_id=run_id,
                commit_sha=run.commit_sha or "",
                files_changed=sorted(files.get(run_id, [])),
                job_id=job_key,
                duration_seconds=agg.duration,
                conclusion=agg.conclusion,
                cache_hit=agg.cache_hit,
                author=run.actor_login,
                timestamp=(run.gh_created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if run.gh_created_at else run.timestamp or ""),
                branch=run.head_branch,
                runner_label=cells[0].runner_label,
                loc_added=run.loc_added,
                loc_deleted=run.loc_deleted,
                workflow_path=run.workflow_path,
                event=run.event,
                cache_outcome=agg.cache_outcome,
                graph_features=(
                    {
                        "job_structurally_impacted": feat.job_structurally_impacted,
                        "cache_key_touched": feat.cache_key_touched,
                        "dep_graph_distance": feat.dep_graph_distance,
                        "any_changed_file_in_graph": feat.any_changed_file_in_graph,
                    }
                    if feat is not None
                    else None
                ),
            )
        )
    records.sort(key=lambda r: (r.timestamp, len(r.run_id), r.run_id, r.job_id))
    return records


def require_history(
    history: list[WorkflowRunRecord],
    job_ids: Iterable[str],
    min_runs: int,
    need_cache_labels: int = 0,
) -> None:
    """Decline to predict unless the history can support it.  Raises
    :class:`InsufficientHistoryError` with the precise reason."""
    n_runs = len({r.run_id for r in history})
    if not history:
        raise InsufficientHistoryError("no ingested history for this repository/workflow", runs=0)
    if n_runs < min_runs:
        raise InsufficientHistoryError("too few ingested runs", runs=n_runs, required=min_runs)
    present = {r.job_id for r in history}
    missing = sorted(set(job_ids) - present)
    if missing:
        raise InsufficientHistoryError("workflow jobs with no ingested history", jobs=missing)
    if need_cache_labels:
        labelled = [r for r in history if r.cache_hit is not None]
        classes = {r.cache_hit for r in labelled}
        if len(labelled) < need_cache_labels or len(classes) < 2:
            raise InsufficientHistoryError(
                "cache-miss model cannot be trained from the available log-derived labels",
                labelled_rows=len(labelled), classes_present=sorted(map(str, classes)), required_rows=need_cache_labels,
            )
