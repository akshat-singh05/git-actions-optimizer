"""
Opt-in pip dependency caching for jobs that install requirements without a cache (``analyze --add-pip-cache``; docs/SPEEDUP.md).

``actions/setup-python`` can cache pip's download cache with one input (``cache: pip``).  A job that installs ``pip install -r requirements.txt`` on every run without it downloads every
package every time.  This module proposes the input - and nothing else - where it can show that the input is safe and meaningful:

* the step is ``actions/setup-python`` with **no** ``cache`` input, in a job whose ``runs-on`` is a plain GitHub-hosted label (``ubuntu-*``, ``windows-*``, ``macos-*``; not a self-hosted or
  expression label);
* the same job runs ``pip install ... -r FILE`` (simple command lines only: no pipe / ``&&`` / ``;`` / redirect), **every** ``FILE`` exists in the checkout (``setup-python`` *fails* when its
  dependency file is missing, so a guess would turn a passing job into a failing one), and no pip command of the job uses ``--no-cache-dir`` (the cache would never be read);
* the job has no ``actions/cache`` step and no other caching of its own (nothing to duplicate or collide with), and the job's install step is not one install pruning removes.

The proposal sets ``cache: pip`` and ``cache-dependency-path`` to exactly the requirement files the job installs.  What it does and does not do:

* pip's cache holds *downloaded* files: the saving is download time, wheels are still installed (and sdists still built), so the saving is small for a job whose install is mostly compiling and
  large for one that mostly downloads.  **No saving is estimated** (the history says nothing about it); the effect is established only by a real before/after run, which is what
  ``scripts/measure_workflows.py`` is for.
* the first run after a change to a requirement file is a cache miss and pays the cost of saving the cache (a few seconds); cache entries of a repository are subject to GitHub's cache size limit and eviction.
* it changes what a job does (it adds a cache restore and save step inside ``setup-python``), so it is opt-in, listed in the file header and the explanation, and validated.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gha_optimizer.ingestion.workflow_parser import WorkflowSpec
from gha_optimizer.install_pruning import _COMPLEX, _logical_lines, _tokens

_HOSTED = re.compile(r"^(ubuntu|windows|macos)-[\w.\-]+$")


@dataclass
class CacheAction:
    job: str
    step_index: int
    files: list[str]                 # the requirement files the job installs (and the cache key is a hash of)


@dataclass
class PipCache:
    actions: list[CacheAction] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)       # job -> why it was left alone


def _read_exists(root: Path, rel: str, read_file: Any) -> bool:
    if read_file is not None:
        return read_file(rel) is not None
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return False
    return p.is_file()


def _pip_requirement_files(job: dict[str, Any]) -> tuple[list[str], str | None]:
    """(requirement files installed with ``pip install -r``, reason this job cannot be judged or None)."""
    files: list[str] = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict) or step.get("run") is None:
            continue
        for line in _logical_lines(step["run"]):
            toks = _tokens(line) if not _COMPLEX.search(line) else None
            if toks is None:
                if re.search(r"\bpip3?\s+install\b", line):
                    return [], "has a pip install in a command line this module does not parse (pipe / && / ; / redirect)"
                continue
            if toks and toks[0] in ("pip", "pip3") and len(toks) > 1 and toks[1] == "install":
                if "--no-cache-dir" in toks:
                    return [], "uses --no-cache-dir, so a pip cache would never be read"
                i = 2
                while i < len(toks):
                    if toks[i] in ("-r", "--requirement") and i + 1 < len(toks):
                        files.append(toks[i + 1])
                        i += 2
                        continue
                    if toks[i].startswith("-r") and len(toks[i]) > 2 and not toks[i].startswith("--"):
                        files.append(toks[i][2:])
                    i += 1
    return list(dict.fromkeys(files)), None


def plan_pip_cache(workflow: WorkflowSpec, repo_root: str | Path, exclude_jobs: set[str] | None = None, read_file: Any = None) -> PipCache:
    """Decide where ``cache: pip`` can be added (see the module docstring).  ``exclude_jobs``: jobs whose pip install is being removed by install pruning."""
    root = Path(repo_root)
    result = PipCache()
    for job_id, job in (workflow.raw.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        sp = [i for i, s in enumerate(steps) if isinstance(s, dict) and str(s.get("uses", "")).lower().startswith("actions/setup-python@")]
        if not sp:
            continue
        if job_id in (exclude_jobs or set()):
            result.skipped[job_id] = "its install is removed by install pruning"
            continue
        if "uses" in job:
            result.skipped[job_id] = "calls a reusable workflow"
            continue
        runs_on = job.get("runs-on")
        if not isinstance(runs_on, str) or not _HOSTED.match(runs_on):
            result.skipped[job_id] = f"runs-on is not a plain GitHub-hosted label ({runs_on!r})"
            continue
        if any("cache" in (s.get("with") or {}) for i, s in enumerate(steps) if i in sp and isinstance(s.get("with"), dict)):
            result.skipped[job_id] = "setup-python already has a cache input"
            continue
        if any(isinstance(s, dict) and "cache" in str(s.get("uses", "")).lower() for s in steps):
            result.skipped[job_id] = "the job has its own caching step"
            continue
        if len(sp) > 1:
            result.skipped[job_id] = "more than one setup-python step"
            continue
        files, why = _pip_requirement_files(job)
        if why:
            result.skipped[job_id] = why
            continue
        if not files:
            result.skipped[job_id] = "installs no requirement file with `pip install -r` (nothing to key the cache on)"
            continue
        missing = [f for f in files if not _read_exists(root, f, read_file)]
        if missing:
            result.skipped[job_id] = f"requirement file not in the checkout: {', '.join(missing)} (setup-python would fail without it)"
            continue
        result.actions.append(CacheAction(job_id, sp[0], files))
    return result


def apply_pip_cache(optimized: dict, actions: list[CacheAction]) -> dict:
    """Copy of the workflow dict with ``cache: pip`` (and ``cache-dependency-path``) added to each action's setup-python step."""
    out = copy.deepcopy(optimized)
    for a in actions:
        steps = ((out.get("jobs") or {}).get(a.job) or {}).get("steps")
        if not isinstance(steps, list) or a.step_index >= len(steps):
            continue
        w = steps[a.step_index].get("with")
        if not isinstance(w, dict):
            w = {}
            steps[a.step_index]["with"] = w
        w["cache"] = "pip"
        w["cache-dependency-path"] = a.files[0] if len(a.files) == 1 else "\n".join(a.files)
    return out


def validate_step_edits(original: WorkflowSpec, optimized: dict, repo_root: str | Path, read_file: Any = None, *, prune: bool = False, cache: bool = False) -> list[str]:
    """Problems with the ``steps`` of an optimized dict relative to the original when install pruning and / or pip caching were applied; empty = valid.

    The expected steps are recomputed from the original by the same rules, so a step edited any other way is reported."""
    from gha_optimizer.install_pruning import apply_pruning, plan_install_pruning

    expected = copy.deepcopy(original.raw)
    pruned = plan_install_pruning(original, repo_root, {}, {}, read_file) if prune else None
    if cache:
        expected = apply_pip_cache(expected, plan_pip_cache(original, repo_root, {a.job for a in pruned.actions} if pruned else set(), read_file).actions)
    if pruned is not None:
        expected = apply_pruning(expected, pruned.actions)
    problems = []
    for job_id, body in (optimized.get("jobs") or {}).items():
        if ((expected.get("jobs") or {}).get(job_id) or {}).get("steps") != (body or {}).get("steps"):
            problems.append(f"steps of job '{job_id}' differ from the original beyond the provably safe edits")
    return problems


def header_lines(r: "PipCache | None") -> list[str]:
    if r is None or not r.actions:
        return []
    out = [f"# Pip caching (opt-in --add-pip-cache): `cache: pip` added to setup-python in {len(r.actions)} job(s); effect NOT estimated (it saves download time only), establish it with a real before/after run."]
    out += [f"#   job '{a.job}': cache key from {', '.join(a.files)}" for a in r.actions]
    return out


def render_pip_cache(r: "PipCache | None") -> str:
    if r is None:
        return ""
    lines = ["### Pip caching (opt-in `--add-pip-cache`)"]
    if not r.actions:
        lines.append("No job qualified: none has a `setup-python` step without a cache input that installs requirement files present in the checkout.")
    else:
        for a in r.actions:
            lines.append(f"- job `{a.job}`: `cache: pip` added to its `setup-python` step, keyed on `{', '.join(a.files)}`.")
        lines.append("**What it does not tell you:** the saving is NOT estimated. pip's cache holds downloads only (wheels are still installed, source packages still built), so it helps a job whose install is mostly "
                     "downloading and does little for one that mostly compiles. The first run after a change to a requirement file is a cache miss and also pays the cost of saving the cache. Only a real before/after run settles it.")
    if r.skipped:
        lines.append(f"{len(r.skipped)} job(s) with a `setup-python` step were left alone; examples:")
        lines += [f"- `{j}`: {w}" for j, w in list(r.skipped.items())[:5]]
    return "\n".join(lines)
