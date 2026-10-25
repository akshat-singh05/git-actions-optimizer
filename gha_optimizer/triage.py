"""
Failure triage on real history: what kind of failures does this workflow have, and what should be done about them?

For every failed job (a failed matrix cell makes the job failed) the module finds

* the failing **step** (from the recorded step conclusions) and a coarse **bucket** of it;
* a **class**, decided only from recorded outcomes - never guessed:

  ``derived_aggregator``  the failing step is that of a status / aggregator job ("Run false", "Decide whether the needed jobs
                          succeeded or failed", ...): it fails only because an upstream job failed, so it is not an independent
                          failure and is left out of the run-level "no code fix needed" figure
  ``flaky_same_commit``   the same job passed in another run of the *same commit* (a re-trigger): strongest flakiness evidence
  ``pre_existing``        the same job also failed in the push run on the default branch at the change's base commit
  ``fixed_by_later_commit`` the next run of the same lineage (a PR, or a branch) passed with a *different* commit: a real defect,
                          later fixed
  ``persistent``          the same job failed in two or more consecutive runs of the lineage and was not fixed within the data
  ``unrelated_to_change`` the dependency graph has file evidence for the job and the change reaches none of its files (weakest:
                          the graph is a static approximation)
  ``unclassified``        none of the above (a first failure with no rerun or lineage evidence)

  A failure gets the first class that applies, in the order above; the evidence flags are also counted separately.

The database keeps only the latest attempt of a re-run, so "passed on a later attempt of the same run" cannot be seen; the
same-commit evidence comes from separate runs of that commit.  Everything is a heuristic over recorded outcomes, not a root
cause: the report prints the counts so the weight of each class is visible.  No LLM and no network are involved.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.server.db import models as m

FAILED = ("failure", "timed_out")
EXECUTED = ("success", "failure", "timed_out")
CLASSES = ("derived_aggregator", "flaky_same_commit", "pre_existing", "fixed_by_later_commit", "persistent", "unrelated_to_change", "unclassified")

ACTIONS = {
    "derived_aggregator": "no action of its own: it fails because another job failed (look at that job)",
    "flaky_same_commit": "re-run: it passed on the same commit",
    "pre_existing": "not caused by this change: fix or quarantine on the base branch",
    "fixed_by_later_commit": "real defect that a later commit fixed",
    "persistent": "needs a fix: it keeps failing",
    "unrelated_to_change": "the change reaches none of its files: check flakiness / infrastructure",
    "unclassified": "investigate: first occurrence, no rerun or lineage evidence",
}
NON_ACTIONABLE = ("flaky_same_commit", "pre_existing", "unrelated_to_change")

# steps of status / aggregator jobs: they fail only because an upstream job failed, so they are not independent failures
_AGGREGATOR_STEP = re.compile(r"^(run false|decide whether the needed jobs|check required jobs|merge test result|"
                              r"check (all )?(required )?(jobs|checks))", re.I)

_BUCKETS = (
    ("runner_infra", re.compile(r"^(set up job|complete job|initialize containers|post )", re.I)),
    ("setup_install", re.compile(r"checkout|setup[ -]|set up |install|cache|restore|download|upload|artifact|fetch|clone|"
                                 r"login|docker pull|npm ci|pip |poetry|uv sync|dependenc", re.I)),
    ("lint_typecheck", re.compile(r"lint|ruff|flake8|black|isort|mypy|pyright|eslint|prettier|format|fmt|clippy|"
                                  r"type ?check|vet|shellcheck|pre-commit|spell", re.I)),
    ("test", re.compile(r"test|pytest|unittest|spec|coverage|nox|tox|junit|integration|e2e|benchmark|bench", re.I)),
    ("build", re.compile(r"build|compile|package|wheel|sdist|docker|make|cargo|gradle|mvn|bazel|generate|docs?", re.I)),
)


def step_bucket(name: str | None) -> str:
    """Coarse bucket of a failing step by its name (first matching rule; ``other`` if none)."""
    if not name:
        return "unknown"
    for bucket, pattern in _BUCKETS:
        if pattern.search(name):
            return bucket
    return "other"


@dataclass
class _Job:
    failed: bool
    executed: bool
    seconds: float
    cells: list[int]                # github_job_id of the failed cells
    impacted: float | None = None   # graph feature (None = unknown)


@dataclass
class _Run:
    run_id: str
    commit: str | None
    base: str | None
    event: str | None
    branch: str | None
    author: str | None
    pr: int | None
    created: str
    jobs: dict[str, _Job]


def lineage_key(r: _Run) -> str:
    if r.pr:
        return f"pr:{r.pr}"
    if r.event == "push":
        return f"push:{r.branch}"
    return f"b:{r.event}:{r.branch}:{r.author}"


def _load(db: Session, repo: str, workflow_path: str | None) -> tuple[list[_Run], str | None]:
    q = db.query(m.WorkflowRun).filter(m.WorkflowRun.repo == repo, m.WorkflowRun.status == "completed",
                                       m.WorkflowRun.jobs_fetched_at.isnot(None))
    if workflow_path:
        q = q.filter(m.WorkflowRun.workflow_path == workflow_path)
    rows = q.all()
    ids = [r.run_id for r in rows]
    cells: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        for c in db.query(m.WorkflowJob).filter(m.WorkflowJob.run_id.in_(ids[i:i + 500]), m.WorkflowJob.job_key.isnot(None)).all():
            cells[(c.run_id, c.job_key)].append(c)
    impacted: dict[tuple[str, str], float | None] = {}
    for i in range(0, len(ids), 500):
        for f in db.query(m.RunGraphFeatures).filter(m.RunGraphFeatures.run_id.in_(ids[i:i + 500]),
                                                     m.RunGraphFeatures.feature_version == GRAPH_FEATURE_VERSION).all():
            impacted[(f.run_id, f.job_key)] = None if f.job_structurally_impacted is None else float(f.job_structurally_impacted)
    by_run: dict[str, dict[str, _Job]] = defaultdict(dict)
    for (run_id, key), cs in cells.items():
        ran = [c for c in cs if c.conclusion in EXECUTED]
        by_run[run_id][key] = _Job(
            failed=any(c.conclusion in FAILED for c in ran), executed=bool(ran),
            seconds=float(sum(c.duration_seconds or 0.0 for c in ran)),
            cells=[c.github_job_id for c in ran if c.conclusion in FAILED and c.github_job_id is not None],
            impacted=impacted.get((run_id, key)),
        )
    default_branch = db.query(m.Repository.default_branch).filter(m.Repository.full_name == repo).scalar()
    runs = [_Run(r.run_id, r.commit_sha, r.diff_base_sha, r.event, r.head_branch, r.actor_login, r.pr_number,
                 r.timestamp or "", by_run.get(r.run_id, {})) for r in rows if r.run_id in by_run]
    runs.sort(key=lambda r: (r.created, len(r.run_id), r.run_id))
    return runs, default_branch


def _failing_steps(db: Session, cell_ids: list[int]) -> list[str]:
    if not cell_ids:
        return []
    out: list[str] = []
    for i in range(0, len(cell_ids), 500):
        out += [n for (n,) in db.query(m.WorkflowStep.name).filter(
            m.WorkflowStep.github_job_id.in_(cell_ids[i:i + 500]), m.WorkflowStep.conclusion == "failure").all() if n]
    return out


def triage_repo(db: Session, repo: str, workflow_path: str | None = None) -> dict[str, Any]:
    runs, default_branch = _load(db, repo, workflow_path)
    result: dict[str, Any] = {"repo": repo, "runs": len(runs), "default_branch": default_branch}
    if not runs:
        result["note"] = "no ingested runs with jobs"
        return result

    by_commit: dict[str, list[_Run]] = defaultdict(list)
    for r in runs:
        if r.commit:
            by_commit[r.commit].append(r)
    base_index: dict[str, _Run] = {}
    for r in runs:                                    # newest push run per commit on the default branch
        if r.event == "push" and r.branch == default_branch and r.commit:
            base_index[r.commit] = r
    lineages: dict[str, list[_Run]] = defaultdict(list)
    for r in runs:
        lineages[lineage_key(r)].append(r)
    position = {id(r): (lk, i) for lk, rs in lineages.items() for i, r in enumerate(rs)}

    classes: Counter[str] = Counter()
    evidence: Counter[str] = Counter()
    buckets: Counter[str] = Counter()
    steps: Counter[str] = Counter()
    wasted_s: Counter[str] = Counter()  # seconds (float)
    per_job: dict[str, Counter[str]] = defaultdict(Counter)
    job_failures: Counter[str] = Counter()
    job_runs: Counter[str] = Counter()
    failing_runs = non_actionable_runs = 0

    for r in runs:
        run_classes: list[str] = []
        for key, job in r.jobs.items():
            if job.executed:
                job_runs[key] += 1
            if not job.failed:
                continue
            lk, i = position[id(r)]
            line = lineages[lk]
            prev = line[i - 1] if i > 0 else None
            nxt = line[i + 1] if i + 1 < len(line) else None
            same_commit_pass = any(o is not r and o.created > r.created and key in o.jobs and o.jobs[key].executed
                                   and not o.jobs[key].failed for o in by_commit.get(r.commit or "", []))
            base = base_index.get(r.base or "")
            pre_existing = bool(base and key in base.jobs and base.jobs[key].failed)
            unrelated = job.impacted == 0.0
            fixed = bool(nxt and key in nxt.jobs and nxt.jobs[key].executed and not nxt.jobs[key].failed
                         and nxt.commit != r.commit)
            persistent = bool((prev and key in prev.jobs and prev.jobs[key].failed)
                              or (nxt and key in nxt.jobs and nxt.jobs[key].failed))
            for flag, on in (("same_commit_pass", same_commit_pass), ("pre_existing", pre_existing),
                             ("unrelated", unrelated), ("fixed_by_later_commit", fixed), ("persistent", persistent)):
                evidence[flag] += on
            names = _failing_steps(db, job.cells)
            step = Counter(names).most_common(1)[0][0] if names else None
            derived = bool(step and _AGGREGATOR_STEP.search(step))
            cls = ("derived_aggregator" if derived else "flaky_same_commit" if same_commit_pass else "pre_existing" if pre_existing
                   else "fixed_by_later_commit" if fixed else "persistent" if persistent
                   else "unrelated_to_change" if unrelated else "unclassified")
            classes[cls] += 1
            per_job[key][cls] += 1
            job_failures[key] += 1
            wasted_s[cls] += job.seconds
            buckets[step_bucket(step)] += 1
            if step:
                steps[(key, step)] += 1          # type: ignore[index]
            if cls != "derived_aggregator":
                run_classes.append(cls)
        if run_classes:
            failing_runs += 1
            non_actionable_runs += all(c in NON_ACTIONABLE for c in run_classes)

    total = sum(classes.values())
    result.update({
        "failed_jobs": total, "independent_failed_jobs": total - classes.get("derived_aggregator", 0),
        "failing_runs": failing_runs,
        "classes": {c: classes.get(c, 0) for c in CLASSES},
        "evidence_flags": dict(evidence),
        "buckets": dict(buckets.most_common()),
        "non_actionable_failures": sum(classes.get(c, 0) for c in NON_ACTIONABLE),
        "non_actionable_runs": non_actionable_runs,
        "billable_seconds_by_class": {c: round(wasted_s.get(c, 0.0), 1) for c in CLASSES},
        "top_failing_steps": [{"job": k[0], "step": k[1], "count": n} for k, n in steps.most_common(8)],       # type: ignore[index]
        "jobs": {k: {"runs": job_runs[k], "failures": job_failures[k],
                     "classes": {c: per_job[k].get(c, 0) for c in CLASSES if per_job[k].get(c, 0)}}
                 for k, _ in job_failures.most_common(8)},
    })
    return result


# ---------------------------------------------------------------------------
# Markdown and the per-job profile shown by `analyze`
# ---------------------------------------------------------------------------

def _pct(n: int, d: int) -> str:
    return "n/a" if not d else f"{100 * n / d:.0f}%"


def render_triage(results: list[dict[str, Any]]) -> str:
    out = ["# Failure triage on real history\n",
           "Generated by `gha-optimizer triage` from the database. Classes are decided from recorded outcomes only (definitions "
           "and limits: `gha_optimizer/triage.py`); they are heuristics, not root causes. A failed job = a job with a failed "
           "matrix cell. The database keeps only the latest attempt of a re-run, so re-run evidence is limited to separate "
           "runs of the same commit.\n"]
    rows = []
    for r in results:
        if "classes" not in r:
            rows.append([r["repo"], r["runs"], r.get("note", "-")] + [""] * 10)
            continue
        c, t = r["classes"], r["failed_jobs"]
        rows.append([r["repo"], r["runs"], f"{t:,}", f"{r['independent_failed_jobs']:,}", r["failing_runs"]] +
                    [f"{c[k]} ({_pct(c[k], t)})" for k in CLASSES] +
                    [f"{r['non_actionable_runs']} of {r['failing_runs']} ({_pct(r['non_actionable_runs'], r['failing_runs'])})"])
    out.append("## Failed jobs by class\n")
    out.append(_table(["repo", "runs", "failed jobs", "of which independent", "red runs", *CLASSES,
                       "red runs with no code fix needed"], rows))
    out.append("*Red runs with no code fix needed*: every independent failed job in the run is `flaky_same_commit`, "
               "`pre_existing` or `unrelated_to_change`. **Flakiness is under-detected here**: a re-run overwrites the earlier "
               "attempt in the database, so only a separate run of the same commit can show it; read `flaky_same_commit` as a "
               "lower bound, not as the flaky rate.\n")
    out.append("## Where they fail (failing step bucket)\n")
    bk = ("runner_infra", "setup_install", "lint_typecheck", "test", "build", "other", "unknown")
    rows = []
    for r in results:
        if "buckets" in r:
            t = r["failed_jobs"]
            rows.append([r["repo"]] + [f"{r['buckets'].get(b, 0)} ({_pct(r['buckets'].get(b, 0), t)})" for b in bk])
    out.append(_table(["repo", *bk], rows))
    out.append("## Recurring failing steps and recommended actions\n")
    for r in results:
        if "top_failing_steps" not in r or not r["top_failing_steps"]:
            continue
        out.append(f"**{r['repo']}**\n")
        out.append(_table(["job", "failing step", "failures"], [[s["job"], s["step"], s["count"]] for s in r["top_failing_steps"]]))
    out.append("## What to do, by class\n")
    out.append(_table(["class", "recommended action"], [[k, v] for k, v in ACTIONS.items()]))
    out.append("## Billable time of failed jobs by class (hours)\n")
    rows = [[r["repo"]] + [f"{r['billable_seconds_by_class'][k] / 3600:.1f}" for k in CLASSES]
            for r in results if "billable_seconds_by_class" in r]
    out.append(_table(["repo", *CLASSES], rows))
    return "\n".join(out)


def job_failure_profile(result: dict[str, Any], min_failures: int = 3) -> dict[str, dict[str, Any]]:
    """``{job: {failures, runs, classes, share_non_actionable}}`` for the jobs with at least ``min_failures`` past failures."""
    out = {}
    for job, d in result.get("jobs", {}).items():
        if d["failures"] >= min_failures:
            na = sum(d["classes"].get(c, 0) for c in NON_ACTIONABLE)
            out[job] = {**d, "share_non_actionable": na / d["failures"]}
    return out


def render_profile(profile: dict[str, dict[str, Any]]) -> str:
    lines = ["### Failure triage of this workflow's past failures (from recorded outcomes; heuristic)"]
    if not profile:
        lines.append("No job has enough past failures to profile.")
        return "\n".join(lines)
    for job, d in profile.items():
        parts = ", ".join(f"{n} {c.replace('_', ' ')}" for c, n in d["classes"].items())
        lines.append(f"- `{job}`: {d['failures']} failures in {d['runs']} runs ({parts}); "
                     f"{100 * d['share_non_actionable']:.0f}% needed no code fix by the evidence above")
    return "\n".join(lines)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"
