"""
Opt-in removal of project-dependency installs from tool-only jobs (``analyze --prune-installs``; docs/SPEEDUP.md).

Many pipelines have a ``lint`` job that runs ``pip install -r requirements.txt`` and then ``ruff check``.  The linter never
imports the project, so installing every dependency is pure wall-clock (the project's own CI: 26 s of a 35 s job, ruff itself
takes 0.1 s).  This module finds those installs and removes **only** them, and only when it can show the job does not need them:

A ``pip install -r FILE`` line is removed from a job only if all of these hold:

* the job is not a matrix job and does not call a reusable workflow;
* every ``uses:`` step is one of checkout / setup-python / cache (nothing that could run project code), and every ``run:`` line is
  either a stand-alone tool (``ruff``, ``black``, ``codespell``, ``yamllint``, ``shellcheck``, ``actionlint``; they read files but do
  not import the project) or a ``pip install`` - a line with ``&&``, ``|``, ``;``, ``$(`` or a redirect, any other command, or a step
  with ``if:`` / ``working-directory:`` makes the whole job ineligible;
* the removed line is exactly ``pip install`` + harmless flags + ``-r FILE`` (no extra packages, no ``-e`` / ``.`` / ``-c``);
* ``FILE`` is readable in the checkout, contains no nested ``-r`` / ``-c`` / ``-e`` / ``git+`` / index options, and does not mention
  any tool the job runs (a pin of the linter inside ``FILE`` would change the linter's version once the install is gone);
* the job still installs the tool itself with a plain ``pip install <tool>`` line (kept untouched).

Anything else is kept.  The trade-off is stated wherever the result is shown: if a later change makes the job need the project's
dependencies (a new tool that imports it) it fails loudly instead of passing.  The saving is ESTIMATED from the recorded duration
of the step in the repository's own history, never measured by this module; ``scripts/measure_workflows.py`` measures it.
"""
from __future__ import annotations

import copy
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gha_optimizer.ingestion.workflow_parser import WorkflowSpec

STANDALONE_TOOLS = ("ruff", "black", "codespell", "yamllint", "shellcheck", "actionlint")
_HARMLESS_USES = ("actions/checkout@", "actions/setup-python@", "actions/cache@", "actions/cache/restore@")
_HARMLESS_PIP_FLAGS = {"-q", "--quiet", "--no-cache-dir", "--upgrade", "-U", "--disable-pip-version-check", "--no-input"}
_COMPLEX = re.compile(r"&&|\|\||\||;|\$\(|`|>|<")
_NESTED = re.compile(r"^\s*(-r|-c|-e|--requirement|--constraint|--editable|--index-url|--extra-index-url|-i|-f|--find-links|git\+|https?://)",
                     re.I | re.M)


@dataclass
class PruneAction:
    job: str
    step_index: int
    step_name: str | None
    removed: list[str]                 # logical lines removed (exactly the project-dependency installs)
    kept: list[str]                    # lines of the same step that stay
    files: list[str]                   # the requirement files the removed lines installed
    est_seconds: float | None          # ESTIMATED saving (recorded median of the step x share of its pip lines), None = unknown


@dataclass
class InstallPruning:
    actions: list[PruneAction] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)       # job -> why it was left alone
    baseline_seconds: float = 0.0                               # estimated critical path before
    pruned_seconds: float = 0.0                                 # ... after (jobs shortened by est_seconds)

    @property
    def saved_pct(self) -> float:
        return 0.0 if self.baseline_seconds == 0 else 100 * (self.baseline_seconds - self.pruned_seconds) / self.baseline_seconds

    def seconds_by_job(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for a in self.actions:
            out[a.job] = out.get(a.job, 0.0) + (a.est_seconds or 0.0)
        return out


def _logical_lines(run: str) -> list[str]:
    """Physical lines joined across trailing backslashes; blank lines and comments dropped."""
    out: list[str] = []
    buf = ""
    for raw in str(run).splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        full = (buf + line).strip()
        buf = ""
        if full and not full.startswith("#"):
            out.append(full)
    if buf.strip():
        out.append(buf.strip())
    return out


def _tokens(line: str) -> list[str] | None:
    try:
        toks = shlex.split(line, posix=True)
    except ValueError:
        return None
    if len(toks) >= 3 and toks[0] in ("python", "python3") and toks[1] == "-m":
        toks = toks[2:]
    return toks


def _classify(line: str) -> tuple[str, Any]:
    """('tool', name) | ('project_install', [files]) | ('plain_install', None) | ('blocked', reason)."""
    if _COMPLEX.search(line):
        return "blocked", "a command line with a pipe / && / || / ; / redirect / substitution"
    toks = _tokens(line)
    if not toks:
        return "blocked", "an unparsable command line"
    if toks[0] in STANDALONE_TOOLS:
        return "tool", toks[0]
    if toks[0] in ("pip", "pip3") and len(toks) >= 2 and toks[1] == "install":
        files: list[str] = []
        other_args = 0
        i = 2
        while i < len(toks):
            t = toks[i]
            if t in ("-r", "--requirement") and i + 1 < len(toks):
                files.append(toks[i + 1])
                i += 2
                continue
            if t.startswith("-r") and len(t) > 2 and not t.startswith("--"):
                files.append(t[2:])
            elif t in ("-e", "--editable", "-c", "--constraint", ".") or t.startswith((".", "/", "git+", "http")) or t.endswith((".txt", ".toml")):
                return "blocked", "installs the project itself or uses a constraint / path argument"
            elif t in _HARMLESS_PIP_FLAGS:
                pass
            elif t.startswith("-"):
                return "blocked", f"an unrecognised pip option ({t})"
            else:
                other_args += 1
            i += 1
        if files and other_args:
            return "blocked", "a pip install that mixes a requirements file with explicit packages"
        if files:
            return "project_install", files
        return "plain_install", None
    return "blocked", f"runs `{toks[0]}`, which is not a stand-alone tool"


def _requirement_file_ok(root: Path, rel: str, tools: set[str]) -> str | None:
    """None if the file can be dropped from the job, else the reason it cannot."""
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return f"{rel} is outside the checkout"
    if not p.is_file():
        return f"{rel} is not in the checkout"
    text = p.read_text(encoding="utf-8", errors="replace")
    if _NESTED.search(text):
        return f"{rel} contains nested requirement / constraint / index options"
    for t in sorted(tools):
        if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text, re.I):
            return f"{rel} mentions {t}, so removing it could change {t}'s version"
    return None


def _analyse_job(job_id: str, job: dict[str, Any], root: Path, step_seconds: dict[tuple[str, str], float]) -> tuple[list[PruneAction], str | None]:
    if job.get("uses") is not None:
        return [], "calls a reusable workflow"
    if isinstance(job.get("strategy"), dict) and job["strategy"].get("matrix"):
        return [], "is a matrix job"
    steps = [s for s in (job.get("steps") or [])]
    if not steps or not all(isinstance(s, dict) for s in steps):
        return [], "has no steps"
    tools: set[str] = set()
    planned: list[tuple[int, list[str], list[str], list[str], str | None]] = []
    n_project = 0
    for idx, step in enumerate(steps):
        if step.get("uses") is not None:
            if not str(step["uses"]).lower().startswith(_HARMLESS_USES):
                return [], f"uses {step['uses']}, which may run project code"
            continue
        run = step.get("run")
        if run is None:
            return [], "has a step that is neither `uses` nor `run`"
        if step.get("if") is not None or step.get("working-directory") is not None or step.get("shell") is not None or step.get("env"):
            return [], f"step {idx + 1} has if / working-directory / shell / env"
        removed: list[str] = []
        kept: list[str] = []
        files: list[str] = []
        for line in _logical_lines(run):
            kind, info = _classify(line)
            if kind == "blocked":
                return [], f"step {idx + 1}: {info}"
            if kind == "tool":
                tools.add(info)
                kept.append(line)
            elif kind == "project_install":
                removed.append(line)
                files += info
                n_project += 1
            else:
                kept.append(line)
        if removed:
            planned.append((idx, removed, kept, files, step.get("name")))
    if not tools:
        return [], "runs no stand-alone tool"
    if not n_project:
        return [], "installs no project dependencies"
    # a tool must be installed by the job itself, otherwise the removed line was what provided it
    for t in tools:
        if not any(re.search(rf"pip3?\s+install\b[^\n]*(?<![\w-]){re.escape(t)}(?![\w-])", str(s.get("run", "")))
                   for s in steps if isinstance(s, dict) and s.get("run")):
            return [], f"{t} is not installed by the job itself (the removed install may provide it)"
    actions: list[PruneAction] = []
    for idx, removed, kept, files, name in planned:
        for f in files:
            why = _requirement_file_ok(root, f, tools)
            if why:
                return [], why
        est: float | None = None
        if name is not None and (job_id, name) in step_seconds:
            pip_lines = [ln for ln in kept if _classify(ln)[0] == "plain_install"] + removed
            only_pip = all(_classify(ln)[0] in ("project_install", "plain_install") for ln in kept + removed)
            if only_pip and pip_lines:
                est = step_seconds[(job_id, name)] * len(removed) / len(pip_lines)
        actions.append(PruneAction(job_id, idx, name, removed, kept, files, est))
    return actions, None


def plan_install_pruning(workflow: WorkflowSpec, repo_root: str | Path, durations: dict[str, float],
                         step_seconds: dict[tuple[str, str], float] | None = None) -> InstallPruning:
    """Decide which project-dependency installs can be removed.  ``durations``: predicted seconds per job (for the
    critical-path estimate); ``step_seconds``: recorded median seconds per (job key, step name)."""
    from gha_optimizer.gates import critical_path_seconds

    root = Path(repo_root)
    result = InstallPruning()
    for job_id, job in (workflow.raw.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        actions, why = _analyse_job(job_id, job, root, step_seconds or {})
        if why is not None:
            result.skipped[job_id] = why
        else:
            result.actions += actions
    edges = list(workflow.job_dag_edges())
    result.baseline_seconds = critical_path_seconds(edges, durations)
    shortened = {j: max(d - result.seconds_by_job().get(j, 0.0), 0.0) for j, d in durations.items()}
    result.pruned_seconds = critical_path_seconds(edges, shortened)
    return result


def apply_pruning(optimized: dict, actions: list[PruneAction]) -> dict:
    """Copy of the workflow dict with each action's removed lines taken out of its step (the step is dropped if nothing is left)."""
    out = copy.deepcopy(optimized)
    for a in sorted(actions, key=lambda x: (x.job, -x.step_index)):
        steps = ((out.get("jobs") or {}).get(a.job) or {}).get("steps")
        if not isinstance(steps, list) or a.step_index >= len(steps):
            continue
        if a.kept:
            steps[a.step_index]["run"] = "\n".join(a.kept)
        else:
            del steps[a.step_index]
    return out


def validate_pruning(original: WorkflowSpec, optimized: dict, repo_root: str | Path) -> list[str]:
    """Problems with the optimized dict's ``steps`` relative to the original; empty = valid.  The expected steps are recomputed
    from the original by the same rules, so a step edited any other way is reported."""
    problems: list[str] = []
    expected = apply_pruning(original.raw, plan_install_pruning(original, repo_root, {}, {}).actions)
    for job_id, body in (optimized.get("jobs") or {}).items():
        want = ((expected.get("jobs") or {}).get(job_id) or {}).get("steps")
        got = (body or {}).get("steps")
        if want != got:
            problems.append(f"steps of job '{job_id}' differ from the original beyond the provably unnecessary installs")
    return problems


def header_lines(r: "InstallPruning | None") -> list[str]:
    if r is None or not r.actions:
        return []
    out = [f"# Pruned installs (opt-in --prune-installs): {len(r.actions)} step(s); estimated critical path "
           f"{r.baseline_seconds:.0f}s -> {r.pruned_seconds:.0f}s ({r.saved_pct:+.1f}%, ESTIMATED from recorded step durations)."]
    for a in r.actions:
        est = "" if a.est_seconds is None else f" (about {a.est_seconds:.0f}s)"
        out.append(f"#   job '{a.job}', step '{a.step_name or a.step_index + 1}': removed `{'; '.join(a.removed)}`{est}")
    return out


def render_pruning(r: "InstallPruning | None") -> str:
    if r is None:
        return ""
    lines = ["### Pruned dependency installs (opt-in `--prune-installs`)"]
    if not r.actions:
        lines.append("No job qualified: none runs only stand-alone tools after installing project dependencies it does not need.")
    else:
        lines.append(f"Estimated critical path (recorded step durations - ESTIMATED, not measured): "
                     f"{r.baseline_seconds:.0f}s -> {r.pruned_seconds:.0f}s ({r.saved_pct:+.1f}%).")
        for a in r.actions:
            est = "no recorded duration for this step" if a.est_seconds is None else f"about {a.est_seconds:.0f}s of the step's recorded median"
            lines.append(f"- job `{a.job}`, step `{a.step_name or a.step_index + 1}`: removed `{'; '.join(a.removed)}` ({est}); "
                         f"the job only runs {', '.join(sorted({t for t in STANDALONE_TOOLS if any(t in k for k in a.kept)})) or 'stand-alone tools'}, "
                         "which read the files and do not import the project.")
        lines.append("**Trade-off:** if the job later gains a tool that needs the project's dependencies, it will fail (missing module) instead of passing, "
                     "and the requirement file no longer warms that job's pip cache.")
    if r.skipped:
        lines.append(f"{len(r.skipped)} other job(s) were left alone; examples:")
        lines += [f"- `{j}`: {w}" for j, w in list(r.skipped.items())[:5]]
    return "\n".join(lines)
