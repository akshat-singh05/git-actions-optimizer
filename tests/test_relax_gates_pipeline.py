"""--relax-gates through the real pipeline: opt-in, validated, listed, and absent by default.

The history is the explicitly SYNTHETIC test fixture: these tests check wiring and honesty, never a result.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from gha_optimizer.gates import validate_relaxation
from gha_optimizer.ingestion.workflow_parser import parse_workflow
from gha_optimizer.pipeline import run_pipeline
from tests.fixtures.synthetic_history import generate_synthetic_test_history

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRACKED = ["src/app.py", "src/utils.py", "src/api/routes.py", "tests/unit/test_app.py",
           "tests/integration/test_api.py", "requirements.txt"]


@pytest.fixture(scope="module")
def workflow_file(tmp_path_factory):
    """The sample workflow with distinct cache keys, so the lint -> test gates are not blocked by a shared key."""
    text = (FIXTURES / "sample_workflow.yml").read_text(encoding="utf-8")
    first, rest = text.split("unit_tests:", 1)
    unit, rest2 = rest.split("integration_tests:", 1)
    text = first + "unit_tests:" + unit.replace("pip-${{", "pip-unit-${{") + "integration_tests:" + rest2.replace(
        "pip-${{", "pip-int-${{", 1)
    path = tmp_path_factory.mktemp("wf") / "ci.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(workflow_file, **kw):
    out = Path(tempfile.mkdtemp()) / "out.yml"
    plan, explanation = run_pipeline(
        workflow_path=workflow_file, repo_root=FIXTURES / "sample_repo", tracked_files=TRACKED,
        history=generate_synthetic_test_history(n_runs=50), changed_files=["src/api/routes.py"], out_path=out, **kw)
    return plan, explanation, out


def test_default_keeps_every_gate_and_has_no_relaxation_section(workflow_file):
    plan, explanation, out = _run(workflow_file)
    assert plan.gate_relaxation is None and "Relaxed `needs` gates" not in explanation
    assert "Relaxed needs gates" not in out.read_text(encoding="utf-8")
    assert validate_relaxation(parse_workflow(workflow_file), yaml.safe_load(out.read_text(encoding="utf-8"))) == []


def test_relax_gates_removes_only_safe_gates_lists_them_and_validates(workflow_file):
    plan, explanation, out = _run(workflow_file, relax_gates=True)
    original = parse_workflow(workflow_file)
    relaxation = plan.gate_relaxation
    assert relaxation is not None and relaxation.relaxed, "some gates are safe and shorten the estimated path"
    assert all(down != "deploy" for _, down in relaxation.relaxed)               # the deploy gate is never relaxed
    # minimal set: the lint -> unit_tests gate is safe but not needed for the saving, so it is kept
    assert any((g.upstream, g.downstream) == ("lint", "unit_tests") and g.relaxable for g in relaxation.kept)
    assert relaxation.relaxed_seconds < relaxation.baseline_seconds and relaxation.saved_pct > 0

    text = out.read_text(encoding="utf-8")
    optimized = yaml.safe_load(text)
    assert validate_relaxation(original, optimized) == []
    removed = set(relaxation.relaxed)
    for up, down in original.job_dag_edges():
        still_gated = up in (optimized["jobs"][down].get("needs") or [])
        still_gated = still_gated or optimized["jobs"][down].get("needs") == up
        assert still_gated == ((up, down) not in removed), (up, down)
    assert optimized["jobs"]["deploy"]["needs"] == "build"                       # deploy still waits for build

    assert "Relaxed needs gates (opt-in --relax-gates)" in text and "ESTIMATED" in text
    assert "Relaxed `needs` gates" in explanation and "Trade-off" in explanation and "ESTIMATED" in explanation
    for up, down in removed:
        assert f"`{down}` no longer waits for `{up}`" in explanation


def test_relaxation_never_touches_the_deploy_gate_even_when_asked(workflow_file):
    plan, _, out = _run(workflow_file, relax_gates=True)
    assert ("build", "deploy") not in plan.gate_relaxation.relaxed
    deploy_reasons = [g.reasons for g in plan.gate_relaxation.kept if g.downstream == "deploy"]
    assert deploy_reasons and "name suggests" in deploy_reasons[0][0]


def test_triage_profile_is_appended_to_the_explanation_without_touching_the_plan(workflow_file):
    profile = {"unit_tests": {"runs": 40, "failures": 6, "classes": {"fixed_by_later_commit": 4, "flaky_same_commit": 2},
                              "share_non_actionable": 2 / 6}}
    plan0, text0, out0 = _run(workflow_file)
    plan1, text1, out1 = _run(workflow_file, triage_profile=profile)
    assert "Failure triage of this workflow's past failures" in text1 and "`unit_tests`: 6 failures in 40 runs" in text1
    assert "Failure triage of this workflow's past failures" not in text0
    assert plan1.summary_dict() == plan0.summary_dict() and out1.read_text(encoding="utf-8") == out0.read_text(encoding="utf-8")
