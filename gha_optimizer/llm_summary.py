"""
Optional plain-language summary of an optimization result, written by an LLM.  EXPLANATION LAYER ONLY.

Contract (CLAUDE.md rule 9):

* The schedule, the predictions, the advisory classification and the proposed workflow are computed before this module
  runs and never read anything it produces.  The summary is appended to the explanation text, nothing else.
* It is opt-in (``analyze --llm-summary`` / ``llm_summary`` in the API request) and needs ``GEMINI_API_KEY`` and
  ``GHA_LLM_MODEL``.  No model name is hard-coded: provider model names change, so the layer stays off until the
  operator names one.  Without them, offline, on any HTTP / parsing error, or if the answer fails validation, the
  pipeline output is exactly what it would have been without the layer plus a one-line note.
* The model sees only a JSON object of facts built from the pipeline's own results: job ids, runner labels, computed
  numbers and the advisory verdicts.  No file paths, commit ids, repository names or workflow text are sent.
* The answer is validated application-side before it is shown: every number in it must appear in the facts, it must not
  contain workflow YAML or code, must stay short, must call plan figures estimates, and may use the word "measured" only
  when the facts carry replay figures.  A failing answer is discarded (never repaired or partly shown).

Status: the request / response shape follows Google's published ``models.generateContent`` REST reference; it has NOT
been exercised against the live service in this repository (no API key was available) - the tests use a fake client and
a mocked HTTP session.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping, Protocol

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
MAX_WORDS = 160
MAX_CHARS = 1400

SYSTEM_PROMPT = (
    "You explain the output of a CI/CD optimizer to a developer in plain English. Use ONLY the JSON facts you are given. "
    "Do not write any number that is not in the facts. Figures under 'plan' are ESTIMATES from predicted durations: call "
    "them estimates, never measurements. Only the figures under 'measured_replay' (if present) are measured, on past runs. "
    "Do not tell the reader to change the schedule, do not promise faster builds, do not output code or YAML. "
    f"Write at most {MAX_WORDS} words."
)


class LLMError(RuntimeError):
    """Any failure to obtain an answer from the model (not configured, transport, HTTP status, malformed body)."""


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, prompt: str) -> str: ...


class GeminiClient:
    """Minimal ``generateContent`` client over ``requests`` (a core dependency)."""

    def __init__(self, api_key: str, model: str, timeout: float = 30.0, session: Any = None):
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self._session = session

    def complete(self, system: str, prompt: str) -> str:
        if self._session is None:
            import requests
            self._session = requests
        url = f"{API_ROOT}/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
        }
        try:
            # the key travels in a header, never in the URL, so it cannot end up in an exception message or a log line
            resp = self._session.post(url, json=body, headers={"x-goog-api-key": self.api_key}, timeout=self.timeout)
        except Exception as exc:  # transport errors: DNS, timeout, TLS ...
            raise LLMError(f"request failed ({type(exc).__name__})") from None
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}")
        try:
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("unexpected response shape (no candidate text; possibly blocked)") from None
        if not text:
            raise LLMError("empty answer")
        return text


def client_from_env(env: dict[str, str] | None = None) -> GeminiClient | None:
    """A client if ``GEMINI_API_KEY`` and ``GHA_LLM_MODEL`` are both set, else None (layer disabled)."""
    source: Mapping[str, str] = os.environ if env is None else env
    key, model = source.get("GEMINI_API_KEY", "").strip(), source.get("GHA_LLM_MODEL", "").strip()
    return GeminiClient(key, model) if key and model else None


# ---------------------------------------------------------------------------
# Facts: the only thing the model sees
# ---------------------------------------------------------------------------

def build_facts(plan: Any, advice: list[Any], unknown: list[str], replay_stats: dict[str, Any] | None) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "plan": plan.summary_dict(),
        "jobs": [
            {"job": j.job_id, "order": j.order, "runner": j.runner,
             "predicted_failure_probability": round(j.predicted_failure_prob, 2),
             "predicted_duration_seconds": round(j.predicted_duration_seconds),
             "skip_candidate": bool(j.skip_candidate), "skip_applied": bool(j.skip_recommended)}
            for j in plan.jobs
        ],
        "advisory_not_applied": {
            "could_be_skipped": [a.job for a in advice if a.verdict == "advise_skip"],
            "unreached_but_kept_unreadable_steps": [a.job for a in advice if a.verdict == "kept_unreadable"],
            "unreached_but_kept_needed_by_other_jobs": [a.job for a in advice if a.verdict == "kept_needed"],
            "reachability_unknown": list(unknown),
        },
    }
    stats = replay_stats or {}
    b = stats.get("dag_safe_readable")
    if b:
        pct = lambda x: None if x is None else round(100 * x, 1)  # noqa: E731
        facts["measured_replay"] = {
            "basis": "MEASURED on past runs of this repository; a counterfactual, the policy was not run",
            "runs_replayed": stats["runs"],
            "billable_time_skipped_pct": pct(b["saved_seconds_share"]),
            "failing_runs_that_would_look_green": b["masked_runs"],
            "failing_runs_total": b["failing_runs"],
            "failure_rate_of_skipped_jobs_pct": pct(b["failure_rate_skipped"]),
            "failure_rate_of_other_jobs_pct": pct(b["failure_rate_rest"]),
        }
    return facts


# ---------------------------------------------------------------------------
# Validation of the model's answer
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _leaves(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _leaves(v)
    else:
        yield obj


def _numbers_in(text: str) -> list[float]:
    return [float(t.replace(",", "")) for t in _NUMBER.findall(text)]


def _allowed_numbers(obj: Any) -> set[float]:
    out: set[float] = set()
    for leaf in _leaves(obj):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, (int, float)):
            for d in range(0, 4):
                out.add(round(float(leaf), d))
        elif isinstance(leaf, str):
            out.update(_numbers_in(leaf))        # "ubuntu-latest-4-cores" legitimately contains a 4
    return out


def _pct_numbers(plan_part: Any) -> set[float]:
    out: set[float] = set()
    if isinstance(plan_part, dict):
        for k, v in plan_part.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and "pct" in k:
                out.update(round(float(v), d) for d in range(0, 4))
            else:
                out |= _pct_numbers(v)
    return out


def validate_summary(text: str, facts: dict[str, Any]) -> str | None:
    """None if the answer is acceptable, else the reason it is rejected."""
    if not text.strip():
        return "empty"
    if len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS * 1.5:
        return "too long"
    low = text.lower()
    if "```" in text or "runs-on" in low or re.search(r"\bneeds\s*:", low) or re.search(r"^\s*(name|on|jobs|if)\s*:", low, re.M):
        return "contains code or workflow text"
    allowed = _allowed_numbers(facts)
    for n in _numbers_in(text):
        if all(abs(n - a) > 1e-9 for a in allowed):
            return "it states a number that is not in the facts"
    plan_pct = _pct_numbers(facts.get("plan"))
    replay_pct = _pct_numbers(facts.get("measured_replay"))
    uses_plan_pct = any(any(abs(n - a) <= 1e-9 for a in plan_pct - replay_pct) for n in _numbers_in(text))
    if uses_plan_pct and "estimat" not in low:
        return "plan figures are not called estimates"
    if "measured" in low and "measured_replay" not in facts:
        return "claims a measurement but the facts contain none"
    return None


# ---------------------------------------------------------------------------
# Entry point used by the pipeline
# ---------------------------------------------------------------------------

def summarize(client: LLMClient, plan: Any, advice: list[Any], unknown: list[str],
              replay_stats: dict[str, Any] | None) -> str:
    """Markdown section for the explanation.  Never raises and never alters the plan."""
    import json

    title = "### Plain-language summary (LLM-written; not part of the decision)"
    facts = build_facts(plan, advice, unknown, replay_stats)
    try:
        text = client.complete(SYSTEM_PROMPT, "Facts (JSON):\n" + json.dumps(facts, indent=2))
    except LLMError as exc:
        logger.warning("LLM summary unavailable: %s", exc)
        return f"{title}\nUnavailable: {exc}. The analysis above is complete without it."
    except Exception as exc:  # a custom client must not be able to break the pipeline either
        logger.warning("LLM summary failed: %s", type(exc).__name__)
        return f"{title}\nUnavailable: the model client failed ({type(exc).__name__}). The analysis above is complete without it."
    reason = validate_summary(text, facts)
    if reason:
        logger.warning("LLM summary rejected: %s", reason)
        return (f"{title}\nThe model's answer was discarded because it failed validation ({reason}). "
                "The analysis above is complete without it.")
    return (f"{title}\n{text.strip()}\n\n_Model: {client.model}. Checked by the application: every number above appears in the "
            "analysis facts; the model did not influence the schedule, the predictions or the proposed workflow._")
