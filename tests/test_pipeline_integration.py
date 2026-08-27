"""Integration test: runs the full pipeline end to end."""
from pathlib import Path

from examples.synthetic_history import generate_history
from gha_optimizer.pipeline import run_pipeline

FIXTURE_WORKFLOW = Path(__file__).resolve().parent.parent / "examples" / "sample_workflow.yml"
FIXTURE_REPO = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"

TRACKED_FILES = [
    "src/app.py", "src/utils.py", "src/api/routes.py",
    "tests/unit/test_app.py", "tests/integration/test_api.py",
    "requirements.txt",
]
CHANGED_FILES = ["src/api/routes.py", "requirements.txt"]


def test_full_pipeline_produces_plan():
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        out_path = f.name

    history = generate_history(n_runs=50)
    plan, explanation = run_pipeline(
        workflow_path=FIXTURE_WORKFLOW,
        repo_root=FIXTURE_REPO,
        tracked_files=TRACKED_FILES,
        history=history,
        changed_files=CHANGED_FILES,
        out_path=out_path,
    )

    # Plan should have all 5 jobs
    assert len(plan.jobs) == 5

    # Should produce an explanation string
    assert len(explanation) > 0
    assert "gha-optimizer" in explanation

    # Estimates should be positive
    assert plan.baseline_wall_clock_seconds > 0
    assert plan.estimated_wall_clock_seconds > 0
    assert plan.baseline_cost_usd > 0
    assert plan.estimated_cost_usd > 0
    assert plan.baseline_energy_wh > 0
    assert plan.estimated_energy_wh > 0

    # Output file should exist
    assert Path(out_path).exists()


def test_pipeline_with_small_history():
    """Pipeline should work even with very few historical runs."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        out_path = f.name

    history = generate_history(n_runs=10)
    plan, explanation = run_pipeline(
        workflow_path=FIXTURE_WORKFLOW,
        repo_root=FIXTURE_REPO,
        tracked_files=TRACKED_FILES,
        history=history,
        changed_files=CHANGED_FILES,
        out_path=out_path,
    )

    assert len(plan.jobs) == 5
    assert plan.baseline_wall_clock_seconds > 0
