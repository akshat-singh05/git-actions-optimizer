"""LLM explanation layer: it must never change the result, must be optional, and its output must be validated."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gha_optimizer.llm_summary import (
    SYSTEM_PROMPT,
    GeminiClient,
    LLMError,
    client_from_env,
    validate_summary,
)
from gha_optimizer.pipeline import run_pipeline
from tests.fixtures.synthetic_history import generate_synthetic_test_history

FIXTURE_WORKFLOW = Path(__file__).resolve().parent / "fixtures" / "sample_workflow.yml"
FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"
TRACKED = ["src/app.py", "src/utils.py", "src/api/routes.py", "tests/unit/test_app.py",
           "tests/integration/test_api.py", "requirements.txt"]
CHANGED = ["src/api/routes.py", "requirements.txt"]

FACTS = {
    "plan": {"wall_clock": {"baseline_s": 120.5, "optimized_s": 100.0, "saved_pct": 17.0}, "jobs_total": 5},
    "jobs": [{"job": "lint", "runner": "ubuntu-latest-4-cores", "order": 1}],
    "measured_replay": {"billable_time_skipped_pct": 12.4, "runs_replayed": 682},
}
FACTS_NO_REPLAY = {k: v for k, v in FACTS.items() if k != "measured_replay"}


# ---- validation ---------------------------------------------------------------------------------------

def test_a_grounded_answer_passes():
    text = ("The plan has 5 jobs. The estimated wall-clock saving is 17.0%, from 120.5 s to 100 s. On 682 past runs the "
            "policy would have skipped 12.4% of billable time (measured).")
    assert validate_summary(text, FACTS) is None


@pytest.mark.parametrize("text,reason", [
    ("The estimated saving is 43.9%.", "number 43.9"),                                 # invented figure
    ("The saving is 17.0%.", "plan figures are not called estimates"),                # estimate not labelled
    ("It saves 17.0% (estimated).\n```yaml\njobs: {}\n```", "code"),                  # code block
    ("Set runs-on to a bigger runner. Estimated 17.0%.", "code"),                     # workflow text
    ("x " * 400, "too long"),
    ("", "empty"),
])
def test_ungrounded_or_unsafe_answers_are_rejected(text, reason):
    got = validate_summary(text, FACTS)
    assert got is not None and reason.split()[0] in got


def test_measured_is_only_allowed_when_the_facts_contain_replay_figures():
    assert validate_summary("Estimated 17.0% saving, measured elsewhere.", FACTS_NO_REPLAY) == \
        "claims a measurement but the facts contain none"
    assert validate_summary("Estimated 17.0% saving, measured elsewhere.", FACTS) is None


def test_numbers_inside_strings_and_rounded_forms_are_accepted():
    assert validate_summary("The first job uses a 4 core runner and takes about 120 s (estimated 17%).", FACTS) is None


def test_a_faithful_high_precision_quote_and_the_magnitude_of_a_negative_fact_are_accepted_but_an_invented_number_is_not():
    """Found by the first live call (2026-10-26): the model quoted a cost stored as 0.0293398 as 0.02934 and a fact of -43.1 as "-43.1%"; both were rejected as
    'not in the facts' because only up to 3 decimals and signed values were allowed.  A rounding / magnitude of a fact is the same figure; an invented one is not."""
    facts = {"plan": {"cost_usd": 0.0293398, "larger_runners": {"cost_saved_pct": -43.1, "wall_clock_saved_pct": 29.5}}}
    assert validate_summary("Estimated cost is 0.02934 USD.", facts) is None                      # 5 decimals of the stored value
    assert validate_summary("Larger runners are estimated to cost -43.1% (more) and save 29.5% time.", facts) is None
    assert validate_summary("Estimated cost is 0.02935 USD.", facts) is not None                   # one digit off: an invented figure
    assert validate_summary("Larger runners are estimated to cost 43.2% more.", facts) is not None


# ---- Gemini client ------------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.exc:
            raise self.exc
        return self.response


OK = {"candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "world"}]}}]}


def test_gemini_client_builds_the_documented_request_and_keeps_the_key_out_of_the_url():
    s = FakeSession(FakeResponse(200, OK))
    out = GeminiClient("SECRET-KEY", "some-model", session=s).complete("sys", "prompt")
    assert out == "Hello world"
    call = s.calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/models/some-model:generateContent"
    assert "SECRET-KEY" not in call["url"] and call["headers"] == {"x-goog-api-key": "SECRET-KEY"}
    assert call["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert call["json"]["contents"][0]["parts"][0]["text"] == "prompt"
    assert call["json"]["generationConfig"]["maxOutputTokens"] > 0


@pytest.mark.parametrize("session", [
    FakeSession(FakeResponse(500)),
    FakeSession(FakeResponse(200, bad_json=True)),
    FakeSession(FakeResponse(200, {"candidates": []})),                                  # e.g. blocked prompt
    FakeSession(FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "  "}]}}]})),
    FakeSession(exc=ConnectionError("dns failure for key=SECRET-KEY")),
])
def test_gemini_failures_become_llm_errors_without_leaking_the_key(session):
    with pytest.raises(LLMError) as ei:
        GeminiClient("SECRET-KEY", "m", session=session).complete("s", "p")
    assert "SECRET-KEY" not in str(ei.value)


def test_client_from_env_requires_both_settings():
    assert client_from_env({}) is None
    assert client_from_env({"GEMINI_API_KEY": "k"}) is None            # no model: no stale default is assumed
    assert client_from_env({"GHA_LLM_MODEL": "m"}) is None
    c = client_from_env({"GEMINI_API_KEY": "k", "GHA_LLM_MODEL": "m"})
    assert c is not None and c.model == "m"


# ---- pipeline: the layer is wording only -------------------------------------------------------------

class EchoClient:
    """Answers with a sentence built from the facts it was sent (so it is valid), and records the request."""
    model = "fake-model"

    def __init__(self):
        self.requests = []

    def complete(self, system, prompt):
        self.requests.append((system, prompt))
        facts = json.loads(prompt.split("Facts (JSON):\n", 1)[1])
        wc = facts["plan"]["wall_clock"]
        return (f"The estimated wall-clock change is {wc['saved_pct']}% for {facts['plan']['jobs_total']} jobs, "
                f"from {wc['baseline_s']} s to {wc['optimized_s']} s.")


class LyingClient:
    model = "fake-model"

    def complete(self, system, prompt):
        return "CI will be 99.9% faster."


class ExplodingClient:
    model = "fake-model"

    def complete(self, system, prompt):
        raise RuntimeError("boom with key=SECRET")


def _run(tmp_path, name, history, client=None):
    out = tmp_path / f"{name}.yml"
    plan, explanation = run_pipeline(
        workflow_path=FIXTURE_WORKFLOW, repo_root=FIXTURE_REPO, tracked_files=TRACKED, history=history,
        changed_files=CHANGED, out_path=out, model_dir=tmp_path / "models", llm_client=client)
    return plan, explanation, out.read_text(encoding="utf-8")


def test_summary_never_changes_the_plan_or_the_workflow_and_is_appended_when_valid(tmp_path):
    history = generate_synthetic_test_history(n_runs=50)
    plan0, text0, yaml0 = _run(tmp_path, "plain", history)
    assert "Plain-language summary" not in text0                                    # off by default

    echo = EchoClient()
    plan1, text1, yaml1 = _run(tmp_path, "echo", history, echo)
    assert plan1.summary_dict() == plan0.summary_dict() and yaml1 == yaml0
    assert text1.startswith(text0.split("### Workflow diff")[0].rstrip()[:200])
    assert "### Plain-language summary (LLM-written; not part of the decision)" in text1
    assert "The estimated wall-clock change is" in text1 and "Model: fake-model" in text1

    # what was sent: the system prompt and facts only - no file paths, no workflow text, no repo location
    system, prompt = echo.requests[0]
    assert system == SYSTEM_PROMPT
    for forbidden in ("routes.py", "requirements.txt", "src/", "sample_repo", "runs-on", str(tmp_path)):
        assert forbidden not in prompt
    assert re.search(r'"job": "', prompt)                                             # job ids are sent

    plan2, text2, yaml2 = _run(tmp_path, "lying", history, LyingClient())
    assert plan2.summary_dict() == plan0.summary_dict() and yaml2 == yaml0
    assert "discarded because it failed validation" in text2 and "99.9" not in text2

    plan3, text3, yaml3 = _run(tmp_path, "boom", history, ExplodingClient())
    assert plan3.summary_dict() == plan0.summary_dict() and yaml3 == yaml0
    assert "Unavailable: the model client failed (RuntimeError)" in text3 and "SECRET" not in text3


# ---- `gha-optimizer llm-check` ------------------------------------------------------------------------------

class _CheckClient:
    model = "m-1"

    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def complete(self, system, prompt):
        assert "ping" == prompt and "OK" in system                       # a tiny fixed request, no repository data
        if self.error:
            raise LLMError(self.error)
        return self.reply


def _run_check(monkeypatch, client):
    import argparse

    from gha_optimizer import cli
    import gha_optimizer.llm_summary as llm_module

    monkeypatch.setattr(llm_module, "client_from_env", lambda env=None: client)
    cli.cmd_llm_check(argparse.Namespace())


def test_llm_check_reports_success(monkeypatch, capsys):
    _run_check(monkeypatch, _CheckClient(reply="OK"))
    out = capsys.readouterr().out
    assert "OK: model 'm-1' answered" in out and "demo.py" in out


def test_llm_check_explains_failures_and_missing_configuration(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_check(monkeypatch, _CheckClient(error="HTTP 404"))
    assert ei.value.code == 1 and "FAILED: HTTP 404" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ei:
        _run_check(monkeypatch, None)
    assert ei.value.code == 2 and "GEMINI_API_KEY" in capsys.readouterr().out
