"""
Counterfactual replay of the graph-based "skip jobs the diff cannot reach" policy on real run history.

The policy (no tuned parameters - it is the static graph of ``graph/fusion.py``):

    skip a job when the graph at the run's commit says the diff does not reach it (``job_structurally_impacted == 0``).

* **naive** - skip every such job that actually ran.
* **dag_safe** - skip it only if every job downstream of it (``needs``) either did not run anyway or is itself
  unreached.  Skipping a job makes GitHub skip its dependents, so a job whose dependents ARE reached must keep running.
* **dag_safe_readable** - additionally never skip a job (or let one be skipped through ``needs``) whose steps the graph
  cannot read: ``make`` targets, remote / local composite actions, reusable-workflow calls.  Motivated by the replay
  itself (prometheus's Go test job runs through ``make``, which also builds the UI, so UI-only changes break it while the
  graph calls it "unreached"); the guard is general, not tuned per job, but it was added after seeing that result.

Unknown jobs (``impacted`` NaN: the graph has no file evidence for them) are never skipped.

For every real (run, job) the replay reads what happened, so the figures are **MEASURED on history** - but the
policy was not run: this is a counterfactual that assumes skipping a job changes no other job's outcome.  Per
repository it reports

* **billable seconds saved** - summed cell durations of the skipped jobs / summed cell durations of everything that ran;
* **failures missed** - jobs that actually failed but would have been skipped, as a share of all job failures
  (an aggregator job that fails because an upstream job failed counts again, so this overstates the damage);
* **failing runs that would look green** - runs in which every job that failed is among the skipped ones: the
  developer-facing safety measure;
* a **random-skipping comparison**: skipping the same *number* of jobs at random would miss
  ``skipped rows / executed rows`` of the failures; a ``lift`` below 1 means the graph picks safer jobs than chance;
* cluster-bootstrap intervals over runs (jobs of one run are not independent).

Jobs the workflow already skips itself (e.g. through a ``paths-filter`` gate) did not run, so they are not counted:
what is measured is the *additional* saving over the workflow as written.  The rule set was written after reading
these repositories' workflows, so the figures are not a held-out test of the rules.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from gha_optimizer.graph.ci_semantics import command_segments
from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text
from gha_optimizer.server.db import models as m

EXECUTED = ("success", "failure", "timed_out")
FAILED = ("failure", "timed_out")
SEED = 13


@dataclass
class JobOutcome:
    executed: bool            # at least one matrix cell ran to success / failure
    failed: bool              # at least one cell failed or timed out
    seconds: float            # summed duration of the executed cells (billable-time proxy)
    impacted: float | None    # graph feature: 1 reached, 0 not reached, None unknown
    opaque: bool = False      # some step is something the graph cannot read (make, remote / composite action, ...)


# actions whose behaviour is known not to read the repository's files in a way that matters to the skip decision
SAFE_ACTION_PREFIXES = (
    "actions/checkout", "actions/setup-", "actions/cache", "actions/upload-artifact", "actions/download-artifact",
    "astral-sh/setup-uv", "pnpm/action-setup", "docker/setup-",
    # status aggregators: they only read the `needs` results of other jobs, never repository files (added after the
    # first replay showed fastapi's `alls-green` job blocking every skip; a second adjustment made after seeing results)
    "re-actors/alls-green",
)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = {"sudo", "time", "env", "nice", "nohup", "exec"}


def _runs_make(run: str) -> bool:
    """True if some simple command in the script has ``make`` / ``gmake`` as its command word (``echo make`` does not)."""
    for segment in command_segments(run):
        for word in segment.split():
            if _ENV_ASSIGN.match(word) or word in _WRAPPERS:
                continue
            if word.split("/")[-1] in ("make", "gmake"):
                return True
            break
    return False


def opaque_job_keys(workflow: Any) -> set[str]:
    """Job keys with at least one step the graph cannot read: a ``make`` command (the Makefile is not parsed), any
    ``uses:`` outside :data:`SAFE_ACTION_PREFIXES` (remote or composite action) or a reusable-workflow call."""
    out: set[str] = set()
    for key, job in workflow.jobs.items():
        if job.uses:
            out.add(key)
            continue
        for step in job.steps:
            uses = str(step.get("uses") or "")
            if _runs_make(str(step.get("run") or "")) or (uses and not uses.startswith(SAFE_ACTION_PREFIXES)):
                out.add(key)
                break
    return out


def descendants_map(edges: list[tuple[str, str]]) -> dict[str, set[str]]:
    """``{job: every job downstream of it}`` from ``(upstream, downstream)`` ``needs`` edges."""
    children: dict[str, set[str]] = defaultdict(set)
    for up, down in edges:
        children[up].add(down)
    out: dict[str, set[str]] = {}
    for start in list(children):
        seen: set[str] = set()
        stack = list(children[start])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(children.get(node, ()))
        out[start] = seen
    return out


def skipped_jobs(
    jobs: dict[str, JobOutcome], descendants: dict[str, set[str]] | None, dag_safe: bool,
    require_readable: bool = False,
) -> set[str]:
    """The jobs the policy would have skipped in one run (see the module docstring)."""
    candidates = {k for k, j in jobs.items()
                  if j.executed and j.impacted == 0 and not (require_readable and j.opaque)}
    if not dag_safe:
        return candidates
    if descendants is None:          # the workflow's DAG is unknown: nothing can be proven safe
        return set()
    safe = set()
    for k in candidates:
        for d in descendants.get(k, ()):
            jd = jobs.get(d)
            # a job with no record, a reached job, or (readable policy) an opaque job downstream still needs this one
            if jd is None or (jd.executed and (jd.impacted != 0 or (require_readable and jd.opaque))):
                break
        else:
            safe.add(k)
    return safe


def load_runs(db: Session, repo: str, workflow_path: str | None = None) -> dict[str, dict[str, Any]]:
    """``{run_id: {"jobs": {job_key: JobOutcome}, "descendants": {...} | None}}`` for every ingested run that has
    graph features of the current definition.  Jobs without a feature row are left out (counted by the caller)."""
    q = db.query(m.WorkflowRun).filter(
        m.WorkflowRun.repo == repo, m.WorkflowRun.status == "completed",
        m.WorkflowRun.jobs_fetched_at.isnot(None), m.WorkflowRun.diff_source.isnot(None),
    )
    if workflow_path:
        q = q.filter(m.WorkflowRun.workflow_path == workflow_path)
    runs = {r.run_id: r for r in q.all()}
    if not runs:
        return {}
    ids = list(runs)

    impacted: dict[tuple[str, str], float | None] = {}
    cells: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for f in db.query(m.RunGraphFeatures).filter(
            m.RunGraphFeatures.run_id.in_(chunk), m.RunGraphFeatures.feature_version == GRAPH_FEATURE_VERSION
        ).all():
            impacted[(f.run_id, f.job_key)] = None if f.job_structurally_impacted is None else float(f.job_structurally_impacted)
        for c in db.query(m.WorkflowJob).filter(m.WorkflowJob.run_id.in_(chunk), m.WorkflowJob.job_key.isnot(None)).all():
            cells[(c.run_id, c.job_key)].append(c)

    dag_cache: dict[str, tuple[dict[str, set[str]] | None, set[str] | None]] = {}

    def workflow_facts(run: Any) -> tuple[dict[str, set[str]] | None, set[str] | None]:
        """``(descendants, opaque job keys)`` of the workflow at the run's commit; ``(None, None)`` if unknown."""
        sha = run.workflow_blob_sha
        if not sha or sha == "unavailable":
            return None, None
        if sha not in dag_cache:
            blob = db.query(m.WorkflowBlob).filter_by(repo=repo, blob_sha=sha).first()
            try:
                wf = parse_workflow_text(blob.content) if blob and blob.content else None
                dag_cache[sha] = (descendants_map(wf.job_dag_edges()), opaque_job_keys(wf)) if wf else (None, None)
            except Exception:
                dag_cache[sha] = (None, None)
        return dag_cache[sha]

    out: dict[str, dict[str, Any]] = {}
    for (run_id, key), imp in impacted.items():
        cs = cells.get((run_id, key), [])
        ran = [c for c in cs if c.conclusion in EXECUTED]
        job = JobOutcome(
            executed=bool(ran),
            failed=any(c.conclusion in FAILED for c in ran),
            seconds=float(sum(c.duration_seconds for c in ran if c.duration_seconds is not None)),
            impacted=imp,
        )
        if run_id not in out:
            desc, opaque = workflow_facts(runs[run_id])
            r = runs[run_id]
            out[run_id] = {"jobs": {}, "descendants": desc, "opaque": opaque, "event": r.event, "commit": r.commit_sha,
                           "base": r.diff_base_sha, "branch": r.head_branch, "created": r.gh_created_at}
        entry = out[run_id]
        # a workflow we could not read has no provably readable job
        job.opaque = True if entry["opaque"] is None else key in entry["opaque"]
        entry["jobs"][key] = job
    return out


def _tally(runs: dict[str, dict[str, Any]], dag_safe: bool, readable: bool = False
           ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Per-run vector ``[exec_rows, exec_fail, exec_secs, skip_rows, skip_fail, skip_secs, failing_run, masked_run]``
    and per-job totals.  A run is *masked* when it has failing jobs and every one of them is skipped."""
    rows = []
    per_job: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(5))
    for entry in runs.values():
        jobs = entry["jobs"]
        skip = skipped_jobs(jobs, entry["descendants"], dag_safe, readable)
        v = np.zeros(8)
        failed_jobs = {k for k, j in jobs.items() if j.executed and j.failed}
        if failed_jobs:
            v[6] = 1
            v[7] = float(failed_jobs <= skip)
        for k, j in jobs.items():
            if not j.executed:
                continue
            v[0] += 1
            v[1] += j.failed
            v[2] += j.seconds
            pj = per_job[k]
            pj[0] += 1
            if k in skip:
                v[3] += 1
                v[4] += j.failed
                v[5] += j.seconds
                pj[1] += 1
                pj[2] += j.seconds
                pj[3] += j.failed
            pj[4] += j.failed
        rows.append(v)
    return np.array(rows) if rows else np.zeros((0, 8)), per_job


def _shares(v: np.ndarray) -> dict[str, float | None]:
    er, ef, es, sr, sf, ss, fr, mr = v.sum(axis=0)
    out: dict[str, float | None] = {
        "masked_run_share": float(mr / fr) if fr else None,
        "saved_seconds_share": float(ss / es) if es else None,
        "missed_failure_share": float(sf / ef) if ef else None,
        "skipped_row_share": float(sr / er) if er else None,
    }
    rest_rows, rest_fail = er - sr, ef - sf
    out["failure_rate_skipped"] = float(sf / sr) if sr else None
    out["failure_rate_rest"] = float(rest_fail / rest_rows) if rest_rows else None
    out["lift_vs_random"] = (out["missed_failure_share"] / out["skipped_row_share"]
                             if out["missed_failure_share"] is not None and out["skipped_row_share"] else None)
    return out


def replay_repo(db: Session, repo: str, workflow_path: str | None = None, n_boot: int = 1000) -> dict[str, Any]:
    runs = load_runs(db, repo, workflow_path)
    result: dict[str, Any] = {"repo": repo, "runs": len(runs)}
    if not runs:
        result["note"] = "no ingested runs with current graph features"
        return result
    for name, dag_safe, readable in (("naive", False, False), ("dag_safe", True, False), ("dag_safe_readable", True, True)):
        per_run, per_job = _tally(runs, dag_safe, readable)
        totals = per_run.sum(axis=0)
        block: dict[str, Any] = {
            "executed_rows": int(totals[0]), "executed_failures": int(totals[1]), "executed_seconds": float(totals[2]),
            "skipped_rows": int(totals[3]), "skipped_failures": int(totals[4]), "skipped_seconds": float(totals[5]),
            "failing_runs": int(totals[6]), "masked_runs": int(totals[7]),
            **_shares(per_run),
        }
        rng = np.random.default_rng(SEED)
        saved, missed, masked = [], [], []
        for _ in range(n_boot):
            s = _shares(per_run[rng.integers(0, len(per_run), len(per_run))])
            if s["saved_seconds_share"] is not None:
                saved.append(s["saved_seconds_share"])
            if s["missed_failure_share"] is not None:
                missed.append(s["missed_failure_share"])
            if s["masked_run_share"] is not None:
                masked.append(s["masked_run_share"])

        def ci(xs: list[float]) -> list[float] | None:
            return [float(np.percentile(xs, 2.5)), float(np.percentile(xs, 97.5))] if xs else None

        block["saved_seconds_ci95"] = ci(saved)
        block["missed_failure_ci95"] = ci(missed)
        block["masked_run_ci95"] = ci(masked)
        top = sorted(per_job.items(), key=lambda kv: -kv[1][2])[:6]
        block["by_job"] = {k: {"executed": int(v[0]), "skipped": int(v[1]), "skipped_failures": int(v[3]),
                               "job_failures": int(v[4])} for k, v in per_job.items()}
        block["top_jobs"] = [
            {"job": k, "executed": int(v[0]), "skipped": int(v[1]), "skipped_seconds": float(v[2]),
             "skipped_failures": int(v[3]), "job_failures": int(v[4])} for k, v in top if v[1] > 0
        ]
        result[name] = block
    return result


# ---------------------------------------------------------------------------
# Deferral: do not run the unreached jobs on pull_request runs (docs/DEFERRAL_PLAN.md, pre-registered)
# ---------------------------------------------------------------------------

PR_EVENTS = ("pull_request", "pull_request_target")
MIN_FAILING_PR_RUNS = 20         # fewer failing PR runs: inconclusive
MIN_SAVING_SHARE = 0.05          # criterion A: share of the repository's total billable time
MAX_HIDDEN_SHARE = 0.05          # criterion B: worst-case masked PR runs / failing PR runs


def _base_run_index(runs: dict[str, dict[str, Any]], default_branch: str | None) -> dict[str, dict[str, JobOutcome]]:
    """``{commit sha: jobs of the newest push run on the default branch at that commit}``."""
    newest: dict[str, tuple[Any, dict[str, JobOutcome]]] = {}
    for e in runs.values():
        if e.get("event") != "push" or not default_branch or e.get("branch") != default_branch or not e.get("commit"):
            continue
        stamp = e.get("created")
        if e["commit"] not in newest or (stamp is not None and (newest[e["commit"]][0] is None or stamp > newest[e["commit"]][0])):
            newest[e["commit"]] = (stamp, e["jobs"])
    return {sha: jobs for sha, (_, jobs) in newest.items()}


def _failure_vs_base(failed: set[str], base_jobs: dict[str, JobOutcome] | None) -> dict[str, int]:
    """Classify each failed (deferred) job against the push run at the PR's base commit."""
    c = {"new": 0, "pre_existing": 0, "unknown": 0}
    for k in failed:
        bj = None if base_jobs is None else base_jobs.get(k)
        if bj is None or not bj.executed:
            c["unknown"] += 1
        elif bj.failed:
            c["pre_existing"] += 1
        else:
            c["new"] += 1
    return c


def _deferral_vectors(runs: dict[str, dict[str, Any]], default_branch: str | None, readable: bool
                      ) -> tuple[np.ndarray, float]:
    """Per PR run ``[exec_s, deferred_s, failing_run, masked, masked_new, masked_unknown_only, masked_pre_existing,
    base_found, deferred_failed_jobs, dj_new, dj_unknown, dj_pre_existing]`` and the total executed seconds of ALL runs."""
    base_index = _base_run_index(runs, default_branch)
    total = sum(j.seconds for e in runs.values() for j in e["jobs"].values() if j.executed)
    rows = []
    for e in runs.values():
        if e.get("event") not in PR_EVENTS:
            continue
        jobs = e["jobs"]
        defer = skipped_jobs(jobs, e["descendants"], True, readable)
        failed = {k for k, j in jobs.items() if j.executed and j.failed}
        base_jobs = base_index.get(e.get("base") or "")
        v = np.zeros(12)
        v[0] = sum(j.seconds for j in jobs.values() if j.executed)
        v[1] = sum(jobs[k].seconds for k in defer)
        v[2] = 1.0 if failed else 0.0
        v[7] = 1.0 if base_jobs is not None else 0.0
        if failed and failed <= defer:
            cls = _failure_vs_base(failed, base_jobs)
            v[3] = 1.0
            if cls["new"]:
                v[4] = 1.0
            elif cls["unknown"]:
                v[5] = 1.0
            else:
                v[6] = 1.0
        dfail = failed & defer
        if dfail:
            cls = _failure_vs_base(dfail, base_jobs)
            v[8], v[9], v[10], v[11] = len(dfail), cls["new"], cls["unknown"], cls["pre_existing"]
        rows.append(v)
    return (np.array(rows) if rows else np.zeros((0, 12))), float(total)


def _deferral_shares(v: np.ndarray, total: float) -> dict[str, float | None]:
    s = v.sum(axis=0) if len(v) else np.zeros(12)
    fail = s[2]
    return {
        "saving_of_pr_time": float(s[1] / s[0]) if s[0] else None,
        "saving_of_total_time": float(s[1] / total) if total else None,
        "hidden_share_worst_case": float((s[4] + s[5]) / fail) if fail else None,
        "hidden_share_new_only": float(s[4] / fail) if fail else None,
    }


def deferral_repo(db: Session, repo: str, workflow_path: str | None = None, n_boot: int = 1000) -> dict[str, Any]:
    """Measured consequences of deferring the graph-unreached jobs on pull_request runs (see docs/DEFERRAL_PLAN.md)."""
    runs = load_runs(db, repo, workflow_path)
    result: dict[str, Any] = {"repo": repo, "runs": len(runs)}
    if not runs:
        result["note"] = "no ingested runs with current graph features"
        return result
    default_branch = db.query(m.Repository.default_branch).filter(m.Repository.full_name == repo).scalar()
    result["default_branch"] = default_branch
    for name, readable in (("readable", True), ("dag_safe", False)):
        per_run, total = _deferral_vectors(runs, default_branch, readable)
        s = per_run.sum(axis=0) if len(per_run) else np.zeros(12)
        block: dict[str, Any] = {
            "pr_runs": int(len(per_run)), "pr_seconds": float(s[0]), "total_seconds": total, "deferred_seconds": float(s[1]),
            "failing_pr_runs": int(s[2]), "masked_runs": int(s[3]), "masked_new": int(s[4]),
            "masked_unknown_only": int(s[5]), "masked_pre_existing": int(s[6]), "base_found_runs": int(s[7]),
            "deferred_failed_jobs": int(s[8]), "deferred_failed_jobs_new": int(s[9]),
            "deferred_failed_jobs_unknown": int(s[10]), "deferred_failed_jobs_pre_existing": int(s[11]),
            **_deferral_shares(per_run, total),
        }
        if len(per_run):
            rng = np.random.default_rng(SEED)
            boots = [_deferral_shares(per_run[rng.integers(0, len(per_run), len(per_run))], total) for _ in range(n_boot)]
            for key in ("saving_of_total_time", "hidden_share_worst_case"):
                xs = [b[key] for b in boots if b[key] is not None]
                block[key + "_ci95"] = [float(np.percentile(xs, 2.5)), float(np.percentile(xs, 97.5))] if xs else None
        block["verdict"] = deferral_verdict(block)
        result[name] = block
    return result


def deferral_verdict(b: dict[str, Any]) -> str:
    """The pre-stated reading of docs/DEFERRAL_PLAN.md (applied to the readable policy)."""
    if b["failing_pr_runs"] < MIN_FAILING_PR_RUNS:
        return "inconclusive (fewer than %d failing PR runs)" % MIN_FAILING_PR_RUNS
    if (b["saving_of_total_time"] or 0.0) < MIN_SAVING_SHARE:
        return "not supported: saves under %.0f%% of total billable time" % (100 * MIN_SAVING_SHARE)
    if (b["hidden_share_worst_case"] or 0.0) >= MAX_HIDDEN_SHARE:
        return "not supported: at least %.0f%% of failing PR runs would first show after merge (worst case)" % (100 * MAX_HIDDEN_SHARE)
    return "supported by the pre-stated criteria (recurrence at merge is NOT measured)"


# ---------------------------------------------------------------------------
# Markdown (every figure comes from the dicts above)
# ---------------------------------------------------------------------------

def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _ci(ci: list[float] | None) -> str:
    return "" if not ci else f" [{100 * ci[0]:.1f}%, {100 * ci[1]:.1f}%]"


def render_replay(results: list[dict[str, Any]], n_boot: int) -> str:
    out = ["# Counterfactual replay: skip jobs the graph says the diff cannot reach\n",
           "Generated by `gha-optimizer replay-impact` from the database; every figure is computed from real job "
           "records (**MEASURED on history**), but the policy was not run: it assumes skipping a job changes no other "
           f"job's outcome. Cluster bootstrap over runs ({n_boot} resamples, seed {SEED}). Method and limits: "
           "`gha_optimizer/replay.py` docstring.\n",
           "Policy: skip a job when the graph at the run's commit says the diff does not reach it. *naive* skips every such "
           "job that ran; *dag_safe* only if every downstream (`needs`) job either did not run anyway or is also unreached; "
           "the *readable* variant also never skips a job (or lets one be skipped through `needs`) whose steps the graph "
           "cannot read - `make`, remote / composite actions, reusable-workflow calls - a guard added after the first "
           "replay showed prometheus's `make`-driven Go test job was the main source of misses. Unknown jobs are never skipped. Jobs the workflow already skipped by itself are not counted: this is the "
           "**additional** saving over the workflow as written. The rules were written after reading these workflows, "
           "so this is not a held-out test of the rules.\n"]
    titles = (("dag_safe_readable", "DAG-safe policy that never skips what the graph cannot read (the one that could be shipped)"),
              ("dag_safe", "DAG-safe policy"), ("naive", "Naive policy (upper bound)"))
    for policy, title in titles:
        out.append(f"## {title}\n")
        rows = []
        for r in results:
            if policy not in r:
                rows.append([r["repo"], r["runs"], r.get("note", "no result")] + [""] * 8)
                continue
            b = r[policy]
            rows.append([
                r["repo"], r["runs"], f"{b['executed_rows']:,}", f"{b['skipped_rows']:,} ({_pct(b['skipped_row_share'])})",
                f"{b['skipped_seconds'] / 3600:.1f} h of {b['executed_seconds'] / 3600:.1f} h",
                _pct(b["saved_seconds_share"]) + _ci(b["saved_seconds_ci95"]),
                f"{b['skipped_failures']} of {b['executed_failures']}",
                _pct(b["missed_failure_share"]) + _ci(b["missed_failure_ci95"]),
                f"{b['masked_runs']} of {b['failing_runs']}: " + _pct(b["masked_run_share"]) + _ci(b["masked_run_ci95"]),
                f"{_pct(b['failure_rate_skipped'])} vs {_pct(b['failure_rate_rest'])}",
                "n/a" if b["lift_vs_random"] is None else f"{b['lift_vs_random']:.2f}",
            ])
        out.append(_table(["repo", "runs", "jobs that ran", "would skip", "billable time skipped", "time saved",
                           "failures in skipped jobs", "job failures missed", "failing runs that would look green",
                           "failure rate: skipped vs rest", "lift vs random"], rows))
    out.append("`lift vs random` = failures-missed share / skipped-jobs share: below 1 the graph skips jobs that fail less "
               "often than chance, above 1 more often.\n")
    out.append("## Jobs contributing most to the readable DAG-safe saving\n")
    for r in results:
        b = r.get("dag_safe_readable")
        if not b or not b["top_jobs"]:
            continue
        out.append(f"**{r['repo']}**\n")
        out.append(_table(["job", "ran", "would skip", "skipped hours", "failures in skipped", "job failures overall"],
                          [[j["job"], j["executed"], j["skipped"], f"{j['skipped_seconds'] / 3600:.2f}",
                            j["skipped_failures"], j["job_failures"]] for j in b["top_jobs"]]))
    return "\n".join(out)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def render_deferral(results: list[dict[str, Any]], n_boot: int) -> str:
    out = ["# Counterfactual replay: defer the graph-unreached jobs on pull_request runs\n",
           "Generated by `gha-optimizer replay-impact --deferral` from the database (**MEASURED on history**; the policy was "
           f"not run). Cluster bootstrap over PR runs ({n_boot} resamples, seed {SEED}). Definitions, criteria and what is "
           "NOT measured were fixed before any figure was computed: `docs/DEFERRAL_PLAN.md`.\n",
           "Policy: on `pull_request` runs, do not run the jobs the readable DAG-safe policy would skip; they still run on "
           "`push` runs. Savings can only come from PR runs. A failure of a deferred job is **pre-existing** if the same job "
           "also failed in the push run on the default branch at the PR's base commit, **new** if it ran and passed there, "
           "**unknown** if no such run / job exists. **Whether a hidden failure recurs after merge is not measured** (the "
           "database cannot link a PR to its merge commit).\n"]
    for key, title in (("readable", "Readable DAG-safe policy (the one the verdict applies to)"), ("dag_safe", "DAG-safe policy (context)")):
        out.append(f"## {title}\n")
        rows = []
        for r in results:
            b = r.get(key)
            if not b:
                rows.append([r["repo"], r["runs"], r.get("note", "no result")] + [""] * 9)
                continue
            rows.append([
                r["repo"], b["pr_runs"],
                f"{_pct(b['saving_of_pr_time'])}", f"{_pct(b['saving_of_total_time'])}{_ci(b.get('saving_of_total_time_ci95'))}",
                b["failing_pr_runs"], b["masked_runs"],
                f"{b['masked_new']} / {b['masked_unknown_only']} / {b['masked_pre_existing']}",
                f"{_pct(b['hidden_share_worst_case'])}{_ci(b.get('hidden_share_worst_case_ci95'))}",
                _pct(b["hidden_share_new_only"]),
                f"{b['base_found_runs']} of {b['pr_runs']}",
                f"{b['deferred_failed_jobs']} ({b['deferred_failed_jobs_new']} / {b['deferred_failed_jobs_unknown']} / {b['deferred_failed_jobs_pre_existing']})",
                b["verdict"] if key == "readable" else "",
            ])
        out.append(_table(["repo", "PR runs", "saved of PR time", "saved of total time", "failing PR runs",
                           "would look green", "of those: new / unknown / pre-existing", "hidden share (worst case)",
                           "hidden share (new only)", "PR runs with a base push run", "deferred job failures (new / unknown / pre-existing)",
                           "pre-stated verdict"], rows))
    out.append("`hidden share (worst case)` counts masked PR runs that are *new* or *unknown* against the base; "
               "`new only` is the optimistic count. Criteria: saving >= 5% of total billable time and worst-case hidden "
               "share < 5%, judged only with >= 20 failing PR runs.\n")
    return "\n".join(out)
