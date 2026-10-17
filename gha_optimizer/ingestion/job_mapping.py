"""
Map GitHub API job names to workflow-YAML job keys, and decide whether a job
can plausibly emit cache lines.

The jobs API returns display names: ``lint``, ``test (ubuntu-latest, 3.12)``,
``Build / compile`` (reusable workflows).  The scheduler and the dependency
graph use the YAML *key*.  The mapping is derived from the workflow YAML that was
checked in at the run's head sha, never guessed from the current default branch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_EXPR = re.compile(r"(\$\{\{.*?\}\})")

# Actions that restore a cache themselves (or wrap one).  Local composite actions
# (``./...``) are treated as capable because their internals are not in the YAML.
_CACHE_CAPABLE_USES = re.compile(
    r"^(actions/cache(/restore)?@"
    r"|actions/setup-(python|node|go|java|dotnet|ruby)@"
    r"|astral-sh/setup-uv@"
    r"|Swatinem/rust-cache@"
    r"|gradle/(actions/setup-gradle|gradle-build-action)@"
    r"|pnpm/action-setup@"
    r"|ruby/setup-ruby@"
    r"|hynek/build-and-inspect-python-package@"
    r"|\./)"
)
_CACHE_STEP_NAME = re.compile(r"cache|actions/setup-|Run \./", re.IGNORECASE)


@dataclass(frozen=True)
class JobMatcher:
    key: str
    regex: re.Pattern[str]
    specificity: int  # number of literal characters in the name template
    matrix: bool = False  # the job has a ``strategy.matrix``


def _template_to_regex(template: str, has_matrix: bool, is_reusable_call: bool) -> tuple[re.Pattern[str], int]:
    parts = _EXPR.split(template)
    pattern = ""
    literal = 0
    for part in parts:
        if part.startswith("${{"):
            pattern += ".*?"  # an expression may expand to an empty string (e.g. an unset matrix value)
        else:
            pattern += re.escape(part)
            literal += len(part)
    if has_matrix:
        pattern += r"(?: \(.*\))?"
    if is_reusable_call:
        pattern += r"(?: / .*)?"
    return re.compile(f"^{pattern}$"), literal


def build_matchers(jobs: dict[str, Any]) -> list[JobMatcher]:
    """Build one matcher per YAML job from the raw ``jobs:`` mapping."""
    matchers: list[JobMatcher] = []
    for key, body in (jobs or {}).items():
        body = body if isinstance(body, dict) else {}
        name = body.get("name")
        template = name if isinstance(name, str) and name else str(key)
        has_matrix = bool((body.get("strategy") or {}).get("matrix")) if isinstance(body.get("strategy"), dict) else False
        is_call = "uses" in body
        rx, literal = _template_to_regex(template, has_matrix, is_call)
        matchers.append(JobMatcher(str(key), rx, literal, has_matrix))
        # A job whose name template is customised can still appear under its key.
        if template != str(key):
            rx2, lit2 = _template_to_regex(str(key), has_matrix, is_call)
            matchers.append(JobMatcher(str(key), rx2, lit2, has_matrix))
    return matchers


def match_job_key(api_job_name: str, matchers: list[JobMatcher]) -> str | None:
    """Return the YAML key for an API job name, or ``None`` if unmapped/ambiguous."""
    hits = [m for m in matchers if m.regex.match(api_job_name)]
    if not hits:
        return None
    best = max(m.specificity for m in hits)
    top = [m for m in hits if m.specificity == best]
    keys = {m.key for m in top}
    if len(keys) > 1:
        # Two jobs share one display name (prometheus: a matrix job and the aggregator job that gives branch protection
        # a stable name).  GitHub appends " (<matrix values>)" to every matrix cell, so a name without that suffix cannot
        # be a matrix cell and one with it cannot be a plain job.  Only used to break a tie, never to override a match.
        suffixed = re.search(r" \(.*\)$", api_job_name) is not None
        keys = {m.key for m in top if m.matrix == suffixed}
    return next(iter(keys)) if len(keys) == 1 else None


def job_is_cache_capable(job_body: dict[str, Any] | None, api_steps: list[dict[str, Any]] | None) -> bool:
    """Could this job emit cache lines?

    Uses the YAML steps when the job was mapped; otherwise falls back to the step
    names reported by the API.  Reusable-workflow calls are treated as capable.
    """
    if isinstance(job_body, dict):
        if "uses" in job_body:
            return True
        for step in job_body.get("steps") or []:
            if isinstance(step, dict) and _CACHE_CAPABLE_USES.match(str(step.get("uses", ""))):
                return True
        return False
    for step in api_steps or []:
        name = str(step.get("name", ""))
        if name.lower() not in ("set up job", "complete job", "initialize containers") and _CACHE_STEP_NAME.search(name):
            return True
    return False
