"""
Layer 5 — Workflow Generation.

Takes a ``SchedulePlan`` and the original ``WorkflowSpec`` / raw YAML dict,
and produces a rewritten workflow: jobs reordered, runner labels updated per
the cost-aware allocation, skip-recommended jobs annotated with ``if:``
guards (non-destructive), and header comments explaining each change.

Also produces a unified diff and a human-readable PR explanation.
"""
from __future__ import annotations

import copy
import difflib
import re
from pathlib import Path

import yaml

from gha_optimizer.gates import apply_relaxation, header_lines
from gha_optimizer.install_pruning import apply_pruning, header_lines as pruning_header_lines
from gha_optimizer.pip_cache import apply_pip_cache, header_lines as pip_cache_header_lines
from gha_optimizer.ingestion.workflow_parser import WorkflowSpec
from gha_optimizer.scheduler.optimizer import SchedulePlan, base_runner_label


def generate_optimized_workflow(workflow: WorkflowSpec, plan: SchedulePlan) -> dict:
    """
    Rewrite the workflow YAML from an optimized ``SchedulePlan``.

    Build matrices are never modified: dropping a cell removes a tested configuration, which a history of past
    failures cannot justify (see CLAUDE.md, "matrix reduction was evaluated and removed").

    Parameters
    ----------
    workflow : Original parsed workflow.
    plan : Optimized schedule from the scheduler.
    """
    optimized = copy.deepcopy(workflow.raw)
    ordered_job_ids = [jp.job_id for jp in plan.jobs]
    plan_by_id = {jp.job_id: jp for jp in plan.jobs}

    original_jobs = optimized.get("jobs", {})
    reordered_jobs = {}

    for job_id in ordered_job_ids:
        if job_id not in original_jobs:
            continue
        job_body = copy.deepcopy(original_jobs[job_id])
        jp = plan_by_id[job_id]

        # --- Runner upgrade ---
        # Only a job that is on the planner's base tier may move to a larger tier of the same OS.  Anything else
        # (windows-latest, macos-*, a matrix expression, a list of labels, a self-hosted runner) is left untouched:
        # rewriting it would change the job's operating system or break its runner selection.  The larger-tier labels
        # must exist in the organisation, so this only happens when the plan was built with upsizing enabled.
        original_runner = job_body.get("runs-on", "ubuntu-latest")
        if plan.runner_upsizing and original_runner == base_runner_label() and jp.runner != original_runner:
            job_body["runs-on"] = jp.runner

        # --- Skip guard ---
        if jp.skip_recommended:
            # Non-destructive: guard with `if:` instead of deleting, so the change is reversible and visible in the
            # PR diff.  The expression must be exactly `${{ ... }}`: text after it would make GitHub evaluate the
            # whole value as a non-empty (truthy) string and the job would NOT be skipped.  The original condition
            # is kept inside the expression, and the reason goes into the file header comment (_plan_header).
            original_if = job_body.get("if")
            if original_if is None:
                job_body["if"] = "${{ false }}"
            else:
                inner = re.sub(r"^\s*\$\{\{\s*|\s*\}\}\s*$", "", str(original_if))
                job_body["if"] = "${{ false && (" + inner + ") }}"

        reordered_jobs[job_id] = job_body

    optimized["jobs"] = reordered_jobs

    # --- Relaxed gates (opt-in, validated by the pipeline against gates.classify_gates) ---
    if plan.gate_relaxation is not None and plan.gate_relaxation.relaxed:
        optimized = apply_relaxation(optimized, plan.gate_relaxation.relaxed)

    # --- Pip caching (opt-in; edits only a setup-python step's `with`, so it is applied before pruning, whose step indices it does not move) ---
    if plan.pip_cache is not None and plan.pip_cache.actions:
        optimized = apply_pip_cache(optimized, plan.pip_cache.actions)

    # --- Pruned dependency installs (opt-in, validated by the pipeline against install_pruning.plan_install_pruning) ---
    if plan.install_pruning is not None and plan.install_pruning.actions:
        optimized = apply_pruning(optimized, plan.install_pruning.actions)

    # No metadata is added as a YAML key: GitHub rejects unknown top-level keys, so provenance and the estimated
    # figures live in the header comment written by write_workflow / generate_diff (see _plan_header).
    return optimized


def _plan_header(plan: SchedulePlan | None) -> str:
    """Comment lines describing the plan: ESTIMATED figures and the reason for every skip guard."""
    if plan is None:
        return ""
    lines = [
        f"# Estimated (from predicted durations, NOT measured): wall-clock {plan.time_saved_pct:+.1f}% saved, "
        f"cost {plan.cost_saved_pct:+.1f}% saved.",
        "# Runner upsizing: " + ("ON (assumed speed-up law, see docs/SCHEDULER_ASSUMPTIONS.md)"
                                 if plan.runner_upsizing else "off (no runner labels were changed)"),
    ]
    for jp in plan.jobs:
        if jp.skip_recommended:
            lines.append(f"# Skip guard on '{jp.job_id}': {jp.skip_reason}")
    lines += pip_cache_header_lines(plan.pip_cache)
    lines += pruning_header_lines(plan.install_pruning)
    lines += header_lines(plan.gate_relaxation)
    candidates = [jp.job_id for jp in plan.jobs if jp.skip_candidate and not jp.skip_recommended]
    if candidates:
        lines.append(f"# Skip recommendations: off - {len(candidates)} job(s) would be guarded: {', '.join(candidates)}")
    return "\n".join(lines) + "\n"


def write_workflow(optimized: dict, out_path: str | Path, plan: SchedulePlan | None = None) -> Path:
    """Write the optimized workflow YAML to disk (``plan`` adds the estimate / skip-reason header comments)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    content = _yaml_dump_with_header(optimized, _plan_header(plan))
    out_path.write_text(content, encoding="utf-8")
    return out_path


def _yaml_dump_with_header(data: dict, extra_header: str = "") -> str:
    """Dump YAML with a header comment explaining the provenance."""
    header = (
        "# ──────────────────────────────────────────────────────────────\n"
        "# This workflow was auto-generated by gha-optimizer.\n"
        "# Review carefully before merging — all changes are reversible.\n"
        "# ──────────────────────────────────────────────────────────────\n"
        f"{extra_header}\n"
    )
    # PyYAML (YAML 1.1) loads the workflow key `on:` as the boolean True; dumping it back would write
    # `true:` and silently break the triggers of the proposed workflow.
    data = {("on" if key is True else key): value for key, value in data.items()}
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    # PyYAML also quotes the string key 'on'; GitHub workflow files write it bare
    body = re.sub(r"(?m)^'on':", "on:", body, count=1)
    return header + body


def generate_diff(
    original_path: str | Path,
    optimized: dict,
    context_lines: int = 3,
    plan: SchedulePlan | None = None,
) -> str:
    """
    Produce a unified diff between the original workflow file and the
    optimized version.  Useful for PR bodies and code review.
    """
    original_path = Path(original_path)
    if original_path.exists():
        original_lines = original_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        original_lines = []

    optimized_text = _yaml_dump_with_header(optimized, _plan_header(plan))
    optimized_lines = optimized_text.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        optimized_lines,
        fromfile=f"a/{original_path.name}",
        tofile=f"b/{original_path.name} (optimized)",
        n=context_lines,
    )
    return "".join(diff)


def explain_changes(plan: SchedulePlan, sensitivity: list[dict[str, float]] | None = None) -> str:
    """Human-readable explanation to attach to the generated pull request.

    ``sensitivity`` (from ``scheduler.optimizer.speedup_sensitivity``) adds the table showing how the larger-runner
    what-if moves with the ASSUMED speed-up law."""
    time_word = "faster" if plan.time_saved_pct >= 0 else "slower"
    cost_word = "cheaper" if plan.cost_saved_pct >= 0 else "more expensive"
    energy_word = "less energy" if plan.energy_saved_pct >= 0 else "more energy"

    lines = [
        "## 🚀 gha-optimizer: proposed workflow changes",
        "",
        "### Summary",
        "",
        "_All figures are ESTIMATED from predicted durations - none is a measured execution._",
        "",
        "| Metric | Baseline | Optimized | Estimated savings |",
        "|--------|----------|-----------|---------|",
        f"| ⏱ Wall-clock | {plan.baseline_wall_clock_seconds:.0f}s "
        f"| {plan.estimated_wall_clock_seconds:.0f}s "
        f"| {abs(plan.time_saved_pct):.1f}% {time_word} |",
        f"| 💰 Cost | ${plan.baseline_cost_usd:.4f} "
        f"| ${plan.estimated_cost_usd:.4f} "
        f"| {abs(plan.cost_saved_pct):.1f}% {cost_word} |",
        f"| ⚡ Energy | {plan.baseline_energy_wh:.2f} Wh "
        f"| {plan.estimated_energy_wh:.2f} Wh "
        f"| {abs(plan.energy_saved_pct):.1f}% {energy_word} |",
        f"| 🌿 CO₂ | {plan.baseline_co2_grams:.2f}g "
        f"| {plan.estimated_co2_grams:.2f}g "
        f"| {abs(plan.co2_saved_pct):.1f}% |",
        "",
        "### Where the estimated saving comes from",
        "",
        f"- Skip recommendations: **{'ON' if plan.skip_applied else 'off - no skip guard is written'}**; "
        f"{sum(1 for j in plan.jobs if j.skip_candidate)} candidate job(s) downstream of a predicted failure. "
        f"They would be credited with {plan.time_saved_by_skipping_pct:.1f}% wall-clock, but only against a baseline in "
        "which every job runs: GitHub already skips a job whose upstream (`needs`) fails, so a correct prediction "
        "saves nothing and a wrong one removes tests that would have passed.",
        f"- Runner upsizing in this plan: **{'ON' if plan.runner_upsizing else 'off - no runner label is changed'}**.",
        f"- What-if, larger runners (ASSUMED speed-up exponent {plan.speedup_exponent}; the ingested history has one "
        f"runner size per OS, so this cannot be measured): {plan.time_saved_with_upsizing_whatif_pct:.1f}% wall-clock, "
        f"{plan.cost_saved_with_upsizing_whatif_pct:+.1f}% cost saved (negative = more expensive).",
        "",
    ]
    if sensitivity:
        lines += [
            "| Assumed speed-up exponent | Wall-clock saved | Cost saved | Energy saved |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {r['speedup_exponent']} | {r['time_saved_pct']:.1f}% | {r['cost_saved_pct']:.1f}% | "
            f"{r['energy_saved_pct']:.1f}% |" for r in sensitivity
        ]
        lines += ["", "_Exponent 0: a larger runner is not faster at all; 1: speed-up proportional to price._", ""]
    lines += [
        "### Per-job plan",
        "",
    ]

    # Group by DAG level
    by_level: dict[int, list] = {}
    for jp in plan.jobs:
        by_level.setdefault(jp.dag_level, []).append(jp)

    for level in sorted(by_level.keys()):
        lines.append(f"**Level {level}** (parallel group):")
        for jp in by_level[level]:
            status = ("⛔ SKIP" if jp.skip_recommended
                      else "⚠ would skip (not applied)" if jp.skip_candidate else "✅ run")
            lines.append(
                f"- `{jp.job_id}` → runner=`{jp.runner}`, "
                f"failure score={jp.predicted_failure_prob:.2f} (not a calibrated probability), "
                f"P(cache miss)={'n/a (no step can restore a cache)' if jp.predicted_cache_miss_prob != jp.predicted_cache_miss_prob else format(jp.predicted_cache_miss_prob, '.2f')} (informational, not used by the scheduler), "
                f"est. {jp.estimated_duration_seconds:.0f}s "
                f"(${jp.estimated_cost_usd:.5f}), "
                f"CO₂={jp.estimated_co2_grams:.3f}g, "
                f"status={status}"
            )
            if jp.skip_reason:
                lines.append(f"  - _Reason: {jp.skip_reason}_")
        lines.append("")

    skipped = [jp for jp in plan.jobs if jp.skip_recommended]
    if skipped:
        lines.append(
            f"ℹ️ {len(skipped)} job(s) guarded with `if: ${{{{ false }}}}` — "
            "remove the guard to re-enable."
        )

    return "\n".join(lines)
