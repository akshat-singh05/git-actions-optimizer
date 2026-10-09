"""
Advisory report: which jobs does the dependency graph say the current diff cannot reach?

This is the *advisory* use of the graph-based skip policy replayed in :mod:`gha_optimizer.replay`
(``docs/IMPACT_REPLAY.md``): the report lists the jobs the readable DAG-safe policy would skip for this diff and
attaches the rates that policy had on the repository's real history.  It **changes nothing** - not the schedule, not the
proposed workflow - because the replay showed the policy is not safe enough to apply automatically (it would make a
share of failing runs look green); whether to act on it is the repository owner's decision.

The classification reuses :func:`gha_optimizer.replay.skipped_jobs` and :func:`gha_optimizer.replay.opaque_job_keys`, so
the advice for a live diff is exactly the policy that was measured.  Jobs whose reachability is unknown (the graph has no
file evidence for them) are never advised.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from gha_optimizer.replay import JobOutcome, descendants_map, opaque_job_keys, skipped_jobs

ADVISE_SKIP = "advise_skip"
KEPT_UNREADABLE = "kept_unreadable"
KEPT_NEEDED = "kept_needed"


@dataclass
class JobAdvice:
    job: str
    verdict: str        # ADVISE_SKIP | KEPT_UNREADABLE | KEPT_NEEDED
    reason: str


def _known(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def advise_unreached_jobs(workflow: Any, impacted: dict[str, Any]) -> tuple[list[JobAdvice], list[str]]:
    """``(advice for every job the graph calls unreached, jobs whose reachability is unknown)``.

    ``impacted`` maps job key -> 1 (reached) / 0 (not reached) / None or NaN (unknown), i.e. the
    ``job_structurally_impacted`` feature of the current diff."""
    opaque = opaque_job_keys(workflow)
    jobs = {k: JobOutcome(executed=True, failed=False, seconds=0.0, impacted=_known(impacted.get(k)), opaque=k in opaque)
            for k in workflow.jobs}
    desc = descendants_map(workflow.job_dag_edges())
    advised = skipped_jobs(jobs, desc, dag_safe=True, require_readable=True)

    advice: list[JobAdvice] = []
    for key, job in jobs.items():
        if job.impacted != 0:
            continue
        if key in advised:
            advice.append(JobAdvice(key, ADVISE_SKIP, "no changed file reaches it and nothing that needs it can run"))
        elif job.opaque:
            advice.append(JobAdvice(key, KEPT_UNREADABLE,
                                    "unreached, but it runs `make`, a remote / composite action or a reusable workflow "
                                    "the graph cannot read"))
        else:
            blockers = sorted(d for d in desc.get(key, ()) if jobs[d].impacted != 0 or jobs[d].opaque)
            advice.append(JobAdvice(key, KEPT_NEEDED,
                                    "unreached, but job(s) that need it are reached, unknown or unreadable: "
                                    + ", ".join(blockers)))
    unknown = sorted(k for k, j in jobs.items() if j.impacted is None)
    return advice, unknown


def advisory_stats(db: Any, repo: str, workflow_path: str | None, n_boot: int = 200) -> dict[str, Any] | None:
    """Replay figures of the policy on ``repo``'s real history (``None`` if there is nothing to replay)."""
    from gha_optimizer.replay import replay_repo

    result = replay_repo(db, repo, workflow_path, n_boot=n_boot)
    return result if "dag_safe_readable" in result else None


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render_advisory(advice: list[JobAdvice], unknown: list[str], stats: dict[str, Any] | None) -> str:
    lines = ["### Advisory: jobs the dependency graph says this diff cannot reach (NOT applied)"]
    advised = [a for a in advice if a.verdict == ADVISE_SKIP]
    if advised:
        lines.append("The proposed workflow still runs all of these. A repository owner could skip them for a change "
                     "like this one:")
    elif advice:
        lines.append("Some jobs look unreached, but none can be skipped safely (see below).")
    else:
        lines.append("The graph reaches, or has no file evidence about, every job for this diff: nothing to advise.")
    by_job = (stats or {}).get("dag_safe_readable", {}).get("by_job", {})
    for a in advice:
        mark = {"advise_skip": "skip candidate", "kept_unreadable": "keep (unreadable steps)",
                "kept_needed": "keep (needed)"}[a.verdict]
        hist = by_job.get(a.job)
        past = ""
        if a.verdict == ADVISE_SKIP and hist and hist["skipped"]:
            past = (f" - on past runs the policy would have skipped it {hist['skipped']} of {hist['executed']} times; "
                    f"it failed in {hist['skipped_failures']} of those")
        lines.append(f"- `{a.job}`: **{mark}** - {a.reason}{past}")
    if unknown:
        lines.append(f"- not advised (no file evidence either way): {', '.join(f'`{u}`' for u in unknown)}")
    if stats:
        b = stats["dag_safe_readable"]
        lines += [
            "",
            f"Measured on this repository's {stats['runs']} ingested runs (counterfactual replay of the same policy; "
            "`gha-optimizer replay-impact`, `docs/IMPACT_REPLAY.md`):",
            f"- billable time it would have skipped: {_pct(b['saved_seconds_share'])}",
            f"- failing runs that would have looked green: {b['masked_runs']} of {b['failing_runs']} "
            f"({_pct(b['masked_run_share'])})",
            f"- failure rate of the jobs it would skip vs the rest: {_pct(b['failure_rate_skipped'])} vs "
            f"{_pct(b['failure_rate_rest'])}",
            "- these are MEASURED on history, not on a run of the policy; it assumes skipping a job changes no other "
            "job's outcome. The rules were written after reading these workflows, so this is not a held-out test.",
        ]
    else:
        lines += ["", "No replay figures are attached (no ingested runs with current graph features), so the "
                      "safety of this advice on past runs is unknown."]
    return "\n".join(lines)
