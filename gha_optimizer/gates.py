"""
Safe relaxation of ``needs`` gates (docs/IMPROVEMENT_PLAN.md C; CLAUDE.md rule 7 as amended).

A ``needs`` edge ``up -> down`` often only *gates*: ``down`` runs after ``up`` succeeded, but uses nothing ``up`` produced.
On GitHub-hosted runners the wall-clock of such a pair is ``up + down``; without the edge it is ``max(up, down)``.  This module
decides, edge by edge, whether dropping the gate is safe, and nothing else: anything it cannot prove safe is kept.

An edge is **relaxable** only if all of these hold for the downstream job ``down``:

* it has no ``environment`` (a deployment gate), no ``concurrency`` group and no job-level ``uses`` (a reusable workflow
  whose content is unknown);
* it references no secret other than ``GITHUB_TOKEN``;
* it has no deploy / publish step: known publish actions, or commands such as ``docker push``, ``twine upload``,
  ``npm publish``, ``gh release``, ``git push``, ``terraform apply``, ``kubectl``, ``helm install``, cloud upload commands
  (``cargo publish --dry-run`` is allowed);
* it never reads ``needs.<up>`` (outputs / result), nor ``needs`` as a whole (``toJSON(needs)``), anywhere in the job;
* it has no ``if`` that uses a status function (``always()``, ``failure()``, ``cancelled()``, ``success()``) - so aggregator /
  status jobs never qualify - and its name does not suggest a deploy / publish / release step (a heuristic safety net:
  deploy logic can hide in scripts);
* no artifact flows from ``up`` to ``down`` (``down`` downloads artifacts while ``up`` uploads any), and they do not share a
  cache key (the upstream job may be what populates it).

The trade-off is real and stated everywhere the result is shown: after relaxation ``down`` also runs when ``up`` fails, so a
failing run can bill more minutes.  Wall-clock estimates use the longest path through the DAG with predicted durations and are
ESTIMATED; the history replay in ``gha_optimizer/gate_replay.py`` uses recorded durations.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from gha_optimizer.ingestion.workflow_parser import WorkflowSpec

PUBLISH_ACTIONS = (
    "pypa/gh-action-pypi-publish", "docker/build-push-action", "softprops/action-gh-release", "actions/deploy-pages",
    "peaceiris/actions-gh-pages", "jamesives/github-pages-deploy-action", "aws-actions/", "azure/", "google-github-actions/",
    "ncipollo/release-action", "cloudflare/", "vercel/", "netlify/", "actions/upload-pages-artifact", "docker/login-action",
    "goreleaser/goreleaser-action", "changesets/action", "release-drafter/", "peter-evans/create-pull-request",
    "stefanzweifel/git-auto-commit-action", "ad-m/github-push-action", "mikepenz/release-changelog-builder-action",
)
_PUBLISH_COMMANDS = re.compile(
    r"docker\s+(push|login)|twine\s+upload|npm\s+publish|yarn\s+publish|pnpm\s+publish|gh\s+release|gh\s+pr\s+(create|merge)|"
    r"git\s+push|git\s+tag|terraform\s+(apply|destroy)|kubectl\s|helm\s+(install|upgrade|uninstall)|aws\s+(s3|ecr|cloudformation)|"
    r"gcloud\s|az\s+(webapp|acr|deployment)|poetry\s+publish|uv\s+publish|mvn\s+(.*\s)?deploy|gradle\w*\s+.*publish|"
    r"curl\s+.*-X\s*(POST|PUT|DELETE|PATCH)|cargo\s+publish(?!.*--dry-run)|vsce\s+publish|ansible-playbook|rsync\s|scp\s",
    re.I)
_SECRET = re.compile(r"secrets\.(?!GITHUB_TOKEN\b)|secrets\[|secrets:\s*inherit", re.I)
_STATUS_FUNCTIONS = re.compile(r"\b(always|failure|cancelled|success)\s*\(\s*\)", re.I)
_DYNAMIC_NEEDS = re.compile(r"(toJSON|fromJSON)\s*\(\s*needs\s*\)|needs\s*\[\s*[^'\"\s\]]", re.I)


@dataclass
class GateDecision:
    upstream: str
    downstream: str
    relaxable: bool
    reasons: list[str] = field(default_factory=list)   # why not (empty when relaxable)


def _text(obj: Any) -> str:
    return yaml.safe_dump(obj, default_flow_style=True, width=10 ** 6)


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _uses(job: dict[str, Any]) -> list[str]:
    return [str(s.get("uses", "")).lower() for s in _steps(job)]


def _cache_keys(job: dict[str, Any]) -> set[str]:
    return {str((s.get("with") or {}).get("key", "")).strip() for s in _steps(job)
            if "actions/cache" in str(s.get("uses", "")) and (s.get("with") or {}).get("key")}


_RISKY_NAME = re.compile(r"deploy|publish|release|upload|push|promote|prod|rollout|ship|tag|registry|pages|"
                         r"announce|notify|sync|mirror", re.I)


def _unsafe_reasons(down: dict[str, Any], down_id: str = "") -> list[str]:
    """Reasons the downstream job is not a harmless check (independent of which upstream gate is considered)."""
    reasons = []
    if _RISKY_NAME.search(f"{down_id} {down.get('name', '')}"):
        reasons.append("its name suggests a deploy / publish / release step (deploy logic can hide in scripts)")
    text = _text({k: v for k, v in down.items() if k != "needs"})
    if down.get("environment") is not None:
        reasons.append("has an environment (deployment gate)")
    if down.get("concurrency") is not None:
        reasons.append("uses a concurrency group")
    if down.get("uses") is not None:
        reasons.append("calls a reusable workflow (content unknown)")
    if _SECRET.search(text):
        reasons.append("references a secret other than GITHUB_TOKEN")
    uses = _uses(down)
    if any(u.startswith(PUBLISH_ACTIONS) or u.split("@")[0] in PUBLISH_ACTIONS for u in uses):
        reasons.append("uses a deploy / publish action")
    if _PUBLISH_COMMANDS.search("\n".join(str(s.get("run", "")) for s in _steps(down))):
        reasons.append("runs a deploy / publish command")
    if _STATUS_FUNCTIONS.search(str(down.get("if", ""))):
        reasons.append("its `if` depends on job status")
    if _DYNAMIC_NEEDS.search(text):
        reasons.append("reads the needs context dynamically")
    return reasons


def classify_gates(workflow: WorkflowSpec) -> list[GateDecision]:
    """One decision per ``needs`` edge of the workflow, from its raw YAML."""
    jobs: dict[str, dict[str, Any]] = {k: v for k, v in (workflow.raw.get("jobs") or {}).items() if isinstance(v, dict)}
    out: list[GateDecision] = []
    for down_id, down in jobs.items():
        needs = down.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        unsafe = _unsafe_reasons(down, down_id)
        down_text = _text({k: v for k, v in down.items() if k != "needs"})
        for up_id in needs:
            reasons = list(unsafe)
            up = jobs.get(up_id)
            if up is None:
                reasons.append("upstream job not found")
            else:
                if re.search(rf"needs\.{re.escape(up_id)}\b|needs\s*\[\s*['\"]{re.escape(up_id)}['\"]\s*\]", down_text):
                    reasons.append(f"reads needs.{up_id} (output / result)")
                if any("download-artifact" in u for u in _uses(down)) and any("upload-artifact" in u for u in _uses(up)):
                    reasons.append("downloads artifacts the upstream job uploads")
                if _cache_keys(up) & _cache_keys(down):
                    reasons.append("shares a cache key with the upstream job (it may populate it)")
            out.append(GateDecision(up_id, down_id, not reasons, reasons))
    return out


def relaxable_edges(workflow: WorkflowSpec) -> set[tuple[str, str]]:
    return {(g.upstream, g.downstream) for g in classify_gates(workflow) if g.relaxable}


# ---------------------------------------------------------------------------
# Timing model: longest path through the DAG (GitHub starts a job as soon as its needs are done)
# ---------------------------------------------------------------------------

def critical_path_seconds(edges: list[tuple[str, str]], durations: dict[str, float]) -> float:
    """Longest path length through the job DAG; ``edges`` are ``(upstream, downstream)``; jobs without a duration count 0."""
    preds: dict[str, list[str]] = {j: [] for j in durations}
    for up, down in edges:
        if up in durations and down in durations:
            preds[down].append(up)
    memo: dict[str, float] = {}

    def finish(job: str, stack: tuple[str, ...] = ()) -> float:
        if job in memo:
            return memo[job]
        if job in stack:                      # a cycle cannot be scheduled; treat the back edge as absent
            return 0.0
        start = max((finish(p, stack + (job,)) for p in preds[job]), default=0.0)
        memo[job] = start + durations[job]
        return memo[job]

    return max((finish(j) for j in durations), default=0.0)


@dataclass
class GateRelaxation:
    relaxed: list[tuple[str, str]]                     # edges removed
    kept: list[GateDecision]                           # edges kept, with reasons (relaxable but not useful: reason "no gain")
    baseline_seconds: float
    relaxed_seconds: float
    expected_extra_seconds: float                      # sum over removed edges of P(up fails) * duration(down)

    @property
    def saved_pct(self) -> float:
        return 0.0 if self.baseline_seconds == 0 else 100 * (self.baseline_seconds - self.relaxed_seconds) / self.baseline_seconds


def plan_relaxation(workflow: WorkflowSpec, durations: dict[str, float],
                    failure_prob: dict[str, float] | None = None) -> GateRelaxation:
    """Greedy: remove a relaxable edge only if it shortens the estimated critical path; repeat until none does.

    ``durations``: predicted seconds per job.  ``failure_prob``: P(job fails) (the pipeline passes each job's recent failure rate), used only to estimate the extra billable
    time the relaxation costs when an upstream job fails (a job that would have been skipped now runs)."""
    decisions = classify_gates(workflow)
    edges = [(g.upstream, g.downstream) for g in decisions]
    relaxable = {(g.upstream, g.downstream) for g in decisions if g.relaxable}
    baseline = critical_path_seconds(edges, durations)
    # Take every safe gate off, then put back each one that is not needed for the saving.  (Removing one gate at a time
    # misses the case of two parallel jobs gated by the same upstream: either removal alone saves nothing.)  The result is
    # a minimal set of removed gates that achieves the full estimated saving; nothing is removed when there is no saving.
    removed = sorted(relaxable)
    best = critical_path_seconds([e for e in edges if e not in set(removed)], durations)
    if best >= baseline - 1e-9:
        removed = []
    else:
        for e in list(removed):
            trial = [x for x in removed if x != e]
            if critical_path_seconds([x for x in edges if x not in set(trial)], durations) <= best + 1e-9:
                removed = trial
    current = [e for e in edges if e not in set(removed)]
    fp = failure_prob or {}
    extra = sum(fp.get(up, 0.0) * durations.get(down, 0.0) for up, down in removed)
    kept = [g for g in decisions if (g.upstream, g.downstream) not in removed]
    return GateRelaxation(removed, kept, baseline, critical_path_seconds(current, durations), extra)


# ---------------------------------------------------------------------------
# Applying and validating
# ---------------------------------------------------------------------------

def apply_relaxation(optimized: dict, relaxed: list[tuple[str, str]]) -> dict:
    """Return a copy of the workflow dict with the given ``up -> down`` gates removed from ``down``'s ``needs``."""
    out = copy.deepcopy(optimized)
    for up, down in relaxed:
        job = (out.get("jobs") or {}).get(down)
        if not isinstance(job, dict):
            continue
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        needs = [n for n in needs if n != up]
        if needs:
            job["needs"] = needs
        else:
            job.pop("needs", None)
    return out


def validate_relaxation(original: WorkflowSpec, optimized: dict) -> list[str]:
    """Problems with an optimized workflow dict relative to its original; empty = valid.

    Every job is kept, no ``needs`` edge is added, and every removed edge is one :func:`classify_gates` calls relaxable."""
    problems: list[str] = []
    jobs = optimized.get("jobs") or {}
    if set(jobs) != set(original.jobs):
        problems.append("the set of jobs changed")
    before = {(u, d) for u, d in original.job_dag_edges()}
    after = set()
    for down, body in jobs.items():
        needs = (body or {}).get("needs") or []
        for up in ([needs] if isinstance(needs, str) else needs):
            after.add((up, down))
    for e in sorted(after - before):
        problems.append(f"needs edge added: {e[0]} -> {e[1]}")
    safe = relaxable_edges(original)
    for e in sorted((before - after) - safe):
        problems.append(f"needs edge removed that is not provably safe: {e[0]} -> {e[1]}")
    return problems


# ---------------------------------------------------------------------------
# Text for the proposed file and the explanation
# ---------------------------------------------------------------------------

def header_lines(r: "GateRelaxation | None") -> list[str]:
    """Comment lines for the proposed workflow file (``[]`` when no gate was relaxed)."""
    if r is None or not r.relaxed:
        return []
    lines = [f"# Relaxed needs gates (opt-in --relax-gates): {len(r.relaxed)} edge(s); estimated critical path "
             f"{r.baseline_seconds:.0f}s -> {r.relaxed_seconds:.0f}s ({r.saved_pct:+.1f}% saved, ESTIMATED from predicted durations)."]
    lines += [f"#   removed: '{down}' no longer waits for '{up}' (it now also runs when '{up}' fails)" for up, down in r.relaxed]
    return lines


def render_relaxation(r: "GateRelaxation | None") -> str:
    """Markdown section for the explanation; says plainly what was and was not relaxed and the trade-off."""
    lines = ["### Relaxed `needs` gates (opt-in `--relax-gates`; changes behaviour when an upstream job fails)"]
    if r is None:
        return ""
    if not r.relaxed:
        lines.append("No `needs` edge was both provably safe to relax and able to shorten the estimated critical path; the "
                     "workflow keeps every gate.")
    else:
        lines.append(f"Estimated critical path (longest chain, predicted durations - ESTIMATED, not measured): "
                     f"{r.baseline_seconds:.0f}s -> {r.relaxed_seconds:.0f}s ({r.saved_pct:+.1f}%).")
        lines += [f"- `{down}` no longer waits for `{up}`" for up, down in r.relaxed]
        lines.append(f"**Trade-off:** each relaxed job now also runs when its former upstream fails, so a failing run can bill "
                     f"more minutes (expected extra about {r.expected_extra_seconds:.0f}s per run, from each upstream job's recent "
                     "failure rate in the history; an estimate).")
    blocked = [g for g in r.kept if g.reasons]
    if blocked:
        lines.append(f"{len(blocked)} other gate(s) were kept because they could not be proven safe; examples:")
        lines += [f"- `{g.upstream}` -> `{g.downstream}`: {g.reasons[0]}" for g in blocked[:5]]
    return "\n".join(lines)


def critical_path_jobs(edges: list[tuple[str, str]], durations: dict[str, float]) -> list[str]:
    """The jobs on one longest chain through the DAG (ties: the lexicographically first), upstream first."""
    preds: dict[str, list[str]] = {j: [] for j in durations}
    for up, down in edges:
        if up in durations and down in durations:
            preds[down].append(up)
    finish: dict[str, float] = {}
    via: dict[str, str | None] = {}

    def f(job: str, stack: tuple[str, ...] = ()) -> float:
        if job in finish:
            return finish[job]
        if job in stack:
            return 0.0
        best, arg = 0.0, None
        for p in sorted(preds[job]):
            v = f(p, stack + (job,))
            if v > best + 1e-12:
                best, arg = v, p
        finish[job], via[job] = best + durations[job], arg
        return finish[job]

    if not durations:
        return []
    end = max(sorted(durations), key=f)
    chain = [end]
    while via.get(chain[-1]):
        chain.append(via[chain[-1]])           # type: ignore[arg-type]
    return chain[::-1]
