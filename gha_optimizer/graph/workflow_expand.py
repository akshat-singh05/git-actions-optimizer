"""
Inline *local* reusable workflows and composite actions into a ``WorkflowSpec``.

A job that is only ``uses: ./.github/workflows/x.yml`` has no steps of its own, and a step
``uses: ./.github/actions/y`` hides its commands in ``y/action.yml``.  Without inlining, the graph
sees neither, so no file can be linked to those jobs.  Only repository-local references are
resolved (read through ``read_file``, e.g. from the git tree at the run's commit); remote
``owner/repo@ref`` actions are opaque and stay that way.

``if:`` expressions of an inlined reusable job are carried to the calling job with
``inputs.<name>`` replaced by the caller's ``with:`` expression, because that is where e.g.
``needs.changes.outputs.tests`` reaches the job.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from typing import Any, Callable

import yaml

from gha_optimizer.ingestion.workflow_parser import JobSpec, WorkflowSpec

logger = logging.getLogger(__name__)

ReadFile = Callable[[str], "str | None"]

# poetry's workflow files spell a repository-local reference ``$/.github/...`` at the ingested commits
_LOCAL_PREFIXES = ("./", "$/")
MAX_COMPOSITE_DEPTH = 3
ORIGIN_KEY = "x-origin"  # added to inlined step dicts: where the step really came from


def local_path(uses: str) -> str | None:
    """Repo-relative path of a local ``uses:`` reference, else None."""
    uses = uses.strip()
    for prefix in _LOCAL_PREFIXES:
        if uses.startswith(prefix):
            return uses[len(prefix):].rstrip("/")
    return None


def _load_yaml(read_file: ReadFile, path: str) -> dict[str, Any] | None:
    text = read_file(path)
    if not text:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.debug("cannot parse %s: %s", path, exc)
        return None
    return doc if isinstance(doc, dict) else None


def _bind_inputs(cond: str, with_inputs: dict[str, Any]) -> str:
    def sub(m: re.Match[str]) -> str:
        val = with_inputs.get(m.group(1))
        if val is None:
            return m.group(0)
        return "(" + re.sub(r"^\$\{\{\s*|\s*\}\}$", "", str(val)) + ")"
    return re.sub(r"\binputs\.([\w-]+)", sub, cond)


def _inline_composites(steps: list[dict[str, Any]], read_file: ReadFile, depth: int,
                       seen: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in steps:
        out.append(step)
        uses = step.get("uses")
        path = local_path(uses) if isinstance(uses, str) else None
        if not path or path.startswith(".github/workflows/") or depth >= MAX_COMPOSITE_DEPTH or path in seen:
            continue
        doc = _load_yaml(read_file, f"{path}/action.yml") or _load_yaml(read_file, f"{path}/action.yaml")
        runs = (doc or {}).get("runs")
        if not isinstance(runs, dict) or runs.get("using") != "composite":
            continue
        inner = []
        for s in runs.get("steps") or []:
            if isinstance(s, dict):
                s = dict(s)
                s.setdefault(ORIGIN_KEY, path)
                if step.get("working-directory") and "working-directory" not in s:
                    s["working-directory"] = step["working-directory"]
                inner.append(s)
        out.extend(_inline_composites(inner, read_file, depth + 1, seen | {path}))
    return out


def expand_workflow(spec: WorkflowSpec, read_file: ReadFile | None) -> WorkflowSpec:
    """A new spec whose jobs carry the steps / conditions of their local reusable workflow and
    composite actions.  ``spec`` itself is not modified."""
    if read_file is None:
        return spec
    jobs: dict[str, JobSpec] = {}
    for key, job in spec.jobs.items():
        steps = list(job.steps)
        extra = list(job.extra_conditions)
        called = local_path(job.uses) if job.uses else None
        if called and called.startswith(".github/workflows/"):
            doc = _load_yaml(read_file, called)
            for ckey, cjob in ((doc or {}).get("jobs") or {}).items():
                cjob = cjob or {}
                wd = ((cjob.get("defaults") or {}).get("run") or {}).get("working-directory")
                for s in cjob.get("steps") or []:
                    if isinstance(s, dict):
                        s = dict(s)
                        s.setdefault(ORIGIN_KEY, f"{called}#{ckey}")
                        if wd and "working-directory" not in s:
                            s["working-directory"] = wd
                        steps.append(s)
                if cjob.get("if") is not None:
                    extra.append(_bind_inputs(str(cjob["if"]), job.with_inputs))
        jobs[key] = dataclasses.replace(job, steps=_inline_composites(steps, read_file, 0), extra_conditions=extra)
    return dataclasses.replace(spec, jobs=jobs)
