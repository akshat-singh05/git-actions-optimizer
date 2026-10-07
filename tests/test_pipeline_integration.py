"""Integration test: runs the full pipeline end to end.

The history here is the explicitly-named SYNTHETIC TEST FIXTURE (deterministic input
for wiring tests).  No number from it is a result; real history comes from ``ingest``.
"""
import copy
import tempfile
from pathlib import Path

import pytest

from gha_optimizer.ingestion.history import InsufficientHistoryError
from gha_optimizer.pipeline import run_pipeline
from tests.fixtures.synthetic_history import generate_synthetic_test_history

FIXTURE_WORKFLOW = Path(__file__).resolve().parent / "fixtures" / "sample_workflow.yml"
FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"

TRACKED_FILES = [
    "src/app.py", "src/utils.py", "src/api/routes.py",
    "tests/unit/test_app.py", "tests/integration/test_api.py",
    "requirements.txt",
]
CHANGED_FILES = ["src/api/routes.py", "requirements.txt"]


def _run(history):
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        out_path = f.name
    plan, explanation = run_pipeline(
        workflow_path=FIXTURE_WORKFLOW,
        repo_root=FIXTURE_REPO,
        tracked_files=TRACKED_FILES,
        history=history,
        changed_files=CHANGED_FILES,
        out_path=out_path,
    )
    return plan, explanation, out_path


def test_full_pipeline_produces_plan():
    plan, explanation, out_path = _run(generate_synthetic_test_history(n_runs=50))

    # Plan should have all 5 jobs
    assert len(plan.jobs) == 5

    # Should produce an explanation string that labels its figures honestly
    assert len(explanation) > 0
    assert "gha-optimizer" in explanation
    assert "ESTIMATED" in explanation
    assert "temporal holdout" in explanation

    # Estimates should be positive
    assert plan.baseline_wall_clock_seconds > 0
    assert plan.estimated_wall_clock_seconds > 0
    assert plan.baseline_cost_usd > 0
    assert plan.estimated_cost_usd > 0
    assert plan.baseline_energy_wh > 0
    assert plan.estimated_energy_wh > 0

    # Output file should exist
    assert Path(out_path).exists()


def test_pipeline_declines_with_too_little_history_and_writes_nothing():
    """Cold start: no synthetic fallback, no default predictions — it refuses."""
    out = Path(tempfile.mkdtemp()) / "should_not_exist.yml"
    with pytest.raises(InsufficientHistoryError) as ei:
        run_pipeline(
            workflow_path=FIXTURE_WORKFLOW, repo_root=FIXTURE_REPO, tracked_files=TRACKED_FILES,
            history=generate_synthetic_test_history(n_runs=10), changed_files=CHANGED_FILES, out_path=out,
        )
    assert ei.value.reason == "too few ingested runs" and ei.value.details["runs"] == 10
    assert not out.exists()


def test_pipeline_declines_with_no_history_at_all():
    with pytest.raises(InsufficientHistoryError) as ei:
        _run([])
    assert "no ingested history" in ei.value.reason


def test_pipeline_declines_when_a_workflow_job_has_no_history():
    history = [r for r in generate_synthetic_test_history(n_runs=50) if r.job_id != "deploy"]
    with pytest.raises(InsufficientHistoryError) as ei:
        _run(history)
    assert ei.value.details["jobs"] == ["deploy"]


def test_pipeline_declines_when_cache_labels_are_missing():
    history = copy.deepcopy(generate_synthetic_test_history(n_runs=50))
    for r in history:
        r.cache_hit = None  # e.g. logs expired: the label is unknown, not "hit"
    with pytest.raises(InsufficientHistoryError) as ei:
        _run(history)
    assert "cache-miss model" in ei.value.reason and ei.value.details["labelled_rows"] == 0


def test_explanation_separates_assumed_from_derived_and_output_is_valid_workflow():
    import yaml

    history = generate_synthetic_test_history(n_runs=50)
    plan, explanation, out_path = _run(history)
    assert "Where the estimated saving comes from" in explanation
    assert "ASSUMED speed-up exponent" in explanation and "Assumed speed-up exponent | Wall-clock saved" in explanation
    assert plan.runner_upsizing is False
    doc = yaml.safe_load(Path(out_path).read_text(encoding="utf-8"))
    assert {("on" if k is True else k) for k in doc} <= {"name", "run-name", "on", "permissions", "env", "defaults",
                                                         "concurrency", "jobs"}
    assert {j["runs-on"] for j in doc["jobs"].values()} == {"ubuntu-latest"}      # nothing relabelled by default


def test_opt_in_upsizing_flag_reaches_the_plan():
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        out = f.name
    plan, explanation = run_pipeline(
        workflow_path=FIXTURE_WORKFLOW, repo_root=FIXTURE_REPO, tracked_files=TRACKED_FILES,
        history=generate_synthetic_test_history(n_runs=50), changed_files=CHANGED_FILES, out_path=out,
        allow_runner_upsizing=True, speedup_exponent=0.9,
    )
    assert plan.runner_upsizing is True and plan.speedup_exponent == 0.9
    assert "Runner upsizing in this plan: **ON**" in explanation
