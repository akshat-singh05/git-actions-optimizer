"""
Counterfactual replay of safe gate relaxation on real run history (MEASURED on history; the relaxed workflow was not run).

For every ingested run the replay knows the recorded duration of each job (the slowest matrix cell: cells run in parallel) and
which jobs executed.  The critical path of a run is the longest chain of executed jobs through the ``needs`` DAG; a job starts
when its executed needs have finished.  Relaxation removes edges (``gha_optimizer/gates.py``) and the critical path is recomputed
from the SAME recorded durations - no estimate is involved on all-success runs, where every job ran in both worlds.

Selection is leak-free in time: the edges a workflow version relaxes are chosen with median durations of the first
``SELECTION_FRACTION`` of the repository's runs and scored on the remaining runs only.

Assumptions, stated rather than hidden: unlimited concurrent runners (GitHub's concurrency limit could delay a job that no
longer waits); durations are unchanged by running concurrently; and in a run where an upstream job failed, the job that had been
skipped behind it would have run for its typical (median) duration - an ESTIMATE of the extra billable time the relaxation
costs, reported separately from the measured wall-clock saving.
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from gha_optimizer.gates import critical_path_jobs, critical_path_seconds, plan_relaxation
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text
from gha_optimizer.server.db import models as m

EXECUTED = ("success", "failure", "timed_out")
FAILED = ("failure", "timed_out")
SELECTION_FRACTION = 0.4


def _load(db: Session, repo: str, workflow_path: str | None):
    q = db.query(m.WorkflowRun).filter(m.WorkflowRun.repo == repo, m.WorkflowRun.status == "completed",
                                       m.WorkflowRun.jobs_fetched_at.isnot(None))
    if workflow_path:
        q = q.filter(m.WorkflowRun.workflow_path == workflow_path)
    runs = sorted(q.all(), key=lambda r: (r.timestamp or "", len(r.run_id), r.run_id))
    ids = [r.run_id for r in runs]
    cells: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        for c in db.query(m.WorkflowJob).filter(m.WorkflowJob.run_id.in_(ids[i:i + 500]), m.WorkflowJob.job_key.isnot(None)).all():
            cells[(c.run_id, c.job_key)].append(c)
    per_run: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (rid, key), cs in cells.items():
        ran = [c for c in cs if c.conclusion in EXECUTED and c.duration_seconds is not None]
        if not ran:
            continue
        per_run[rid][key] = {"seconds": max(c.duration_seconds for c in ran),
                             "billable": sum(c.duration_seconds for c in ran),
                             "failed": any(c.conclusion in FAILED for c in ran)}
    return runs, per_run


def gate_replay_repo(db: Session, repo: str, workflow_path: str | None = None) -> dict[str, Any]:
    runs, per_run = _load(db, repo, workflow_path)
    result: dict[str, Any] = {"repo": repo, "runs": len(runs)}
    runs = [r for r in runs if per_run.get(r.run_id) and r.workflow_blob_sha and r.workflow_blob_sha != "unavailable"]
    if len(runs) < 20:
        result["note"] = "fewer than 20 usable runs"
        return result
    cut = int(len(runs) * SELECTION_FRACTION)
    selection, scored = runs[:cut], runs[cut:]

    medians: dict[str, float] = {}
    by_job: dict[str, list[float]] = defaultdict(list)
    for r in selection:
        for key, j in per_run[r.run_id].items():
            by_job[key].append(j["seconds"])
    medians = {k: st.median(v) for k, v in by_job.items()}
    fail_rate: dict[str, float] = {}
    for key in by_job:
        outcomes = [per_run[r.run_id][key]["failed"] for r in selection if key in per_run[r.run_id]]
        fail_rate[key] = sum(outcomes) / len(outcomes)

    blobs: dict[str, Any] = {}

    def spec_for(sha: str):
        if sha not in blobs:
            blob = db.query(m.WorkflowBlob).filter_by(repo=repo, blob_sha=sha).first()
            try:
                wf = parse_workflow_text(blob.content) if blob and blob.content else None
            except Exception:
                wf = None
            if wf is None:
                blobs[sha] = None
            else:
                durations = {k: medians.get(k, 0.0) for k in wf.jobs}
                plan = plan_relaxation(wf, durations, fail_rate)
                blobs[sha] = {"edges": wf.job_dag_edges(), "relaxed": plan.relaxed,
                              "n_edges": len(wf.job_dag_edges()), "n_kept_relaxable": len(plan.kept)}
        return blobs[sha]

    savings, pct, success_runs, saved_runs = [], [], 0, 0
    on_path: dict[str, int] = defaultdict(int)
    path_len: list[float] = []
    extra_s = total_s = 0.0
    failing_runs_with_extra = failing_runs = 0
    edges_total = edges_relaxed = 0
    for r in scored:
        info = spec_for(r.workflow_blob_sha)
        jobs = per_run[r.run_id]
        total_s += sum(j["billable"] for j in jobs.values())
        if info is None:
            continue
        executed = set(jobs)
        edges = [(u, d) for u, d in info["edges"] if u in executed and d in executed]
        relaxed_edges = [e for e in edges if e not in set(info["relaxed"])]
        durations = {k: j["seconds"] for k, j in jobs.items()}
        if not any(j["failed"] for j in jobs.values()):
            success_runs += 1
            before = critical_path_seconds(edges, durations)
            path_len.append(before)
            for j in critical_path_jobs(edges, durations):
                on_path[j] += 1
            after = critical_path_seconds(relaxed_edges, durations)
            savings.append(before - after)
            pct.append(100 * (before - after) / before if before else 0.0)
            saved_runs += (before - after) > 1e-9
        else:
            failing_runs += 1
            # an upstream job failed and its (relaxed) dependant was skipped: it would now have run
            skipped_now_running = {d for u, d in info["relaxed"] if u in jobs and jobs[u]["failed"] and d not in jobs}
            extra = sum(medians.get(d, 0.0) for d in skipped_now_running)
            extra_s += extra
            failing_runs_with_extra += extra > 0
    if scored and (spec := spec_for(scored[-1].workflow_blob_sha)):
        edges_total, edges_relaxed = spec["n_edges"], len(spec["relaxed"])

    def pctile(xs: list[float], q: float) -> float | None:
        return None if not xs else sorted(xs)[min(int(q * len(xs)), len(xs) - 1)]

    result.update({
        "selection_runs": len(selection), "scored_runs": len(scored),
        "needs_edges_in_latest_workflow": edges_total, "edges_relaxed_in_latest_workflow": edges_relaxed,
        "all_success_runs": success_runs, "runs_with_saving": saved_runs,
        "median_saving_s": None if not savings else st.median(savings),
        "mean_saving_s": None if not savings else st.mean(savings),
        "p90_saving_s": pctile(savings, 0.9),
        "median_saving_pct": None if not pct else st.median(pct),
        "mean_saving_pct": None if not pct else st.mean(pct),
        "median_critical_path_s": None if not path_len else st.median(path_len),
        "critical_path_job_share": {j: n / success_runs for j, n in sorted(on_path.items(), key=lambda kv: -kv[1])[:4]} if success_runs else {},
        "failing_runs": failing_runs, "failing_runs_with_extra_work": failing_runs_with_extra,
        "extra_billable_hours_estimated": extra_s / 3600.0,
        "total_billable_hours_scored": total_s / 3600.0,
        "extra_billable_pct_estimated": 100 * extra_s / total_s if total_s else None,
    })
    return result


def _f(x: float | None, nd: int = 1, suffix: str = "") -> str:
    return "n/a" if x is None else f"{x:.{nd}f}{suffix}"


def render_gate_replay(results: list[dict[str, Any]]) -> str:
    out = ["# Counterfactual replay: relaxing safe `needs` gates on real history\n",
           "Generated by `gha-optimizer replay-gates` (method and assumptions: `gha_optimizer/gate_replay.py`, safety rules: "
           "`gha_optimizer/gates.py`). **MEASURED on history** for the wall-clock saving (recorded job durations, recomputed "
           "critical path); the extra billable time on failing runs is an **ESTIMATE** (the skipped job is assumed to run for its "
           "median duration). The relaxed workflow was not executed here; edges are chosen on the first 40% of each repository's "
           "runs and scored on the rest. Unlimited concurrent runners are assumed.\n"]
    rows = []
    for r in results:
        if "scored_runs" not in r:
            rows.append([r["repo"], r["runs"], r.get("note", "-")] + [""] * 9)
            continue
        rows.append([
            r["repo"], r["scored_runs"], f"{r['edges_relaxed_in_latest_workflow']} of {r['needs_edges_in_latest_workflow']}",
            r["all_success_runs"], f"{r['runs_with_saving']}",
            _f(r["median_saving_s"], 1, " s"), _f(r["mean_saving_s"], 1, " s"), _f(r["p90_saving_s"], 1, " s"),
            _f(r["median_saving_pct"], 1, "%"), _f(r["mean_saving_pct"], 1, "%"),
            f"{r['failing_runs_with_extra_work']} of {r['failing_runs']}",
            f"{_f(r['extra_billable_hours_estimated'], 1)} h ({_f(r['extra_billable_pct_estimated'], 1, '%')} of "
            f"{_f(r['total_billable_hours_scored'], 0)} h)"])
    out.append(_table(["repo", "scored runs", "edges relaxed (latest workflow)", "all-success runs", "runs with saving",
                       "median saved", "mean saved", "p90 saved", "median saved %", "mean saved %",
                       "failing runs that would do extra work", "extra billable time (ESTIMATED)"], rows))
    out.append("## What limits speed: the jobs on the critical path of all-success runs\n")
    rows = [[r["repo"], _f(r.get("median_critical_path_s"), 0, " s"),
             ", ".join(f"`{j}` {100 * v:.0f}%" for j, v in r.get("critical_path_job_share", {}).items()) or "n/a"]
            for r in results if "scored_runs" in r]
    out.append(_table(["repo", "median critical path", "jobs on the critical path (share of runs)"], rows))
    out.append("A job that is on the critical path in most runs is what to speed up (shard its tests, cache its set-up); "
               "removing gates cannot shorten a path whose slowest job is already first or last in its chain.\n")
    out.append("*Saved* is the reduction of the run's critical path on runs where every job succeeded (so every job ran in both "
               "worlds); *extra billable time* is what the relaxed gates would add in failing runs, as a share of all billable "
               "time in the scored runs.\n")
    return "\n".join(out)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"
