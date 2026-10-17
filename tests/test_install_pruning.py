"""Install pruning (gha_optimizer/install_pruning.py): what is removed, what is never removed, and that the result validates."""
from pathlib import Path

import pytest
import yaml

from gha_optimizer.ingestion.workflow_parser import parse_workflow
from gha_optimizer.install_pruning import apply_pruning, plan_install_pruning, validate_pruning

LINT = {
    "runs-on": "ubuntu-latest",
    "steps": [
        {"uses": "actions/checkout@v4"},
        {"uses": "actions/setup-python@v5", "with": {"python-version": "3.10"}},
        {"name": "Install dependencies", "run": "pip install -r requirements.txt\npip install ruff"},
        {"name": "Lint", "run": "ruff check src/ --select E,F"},
    ],
}
TEST = {"runs-on": "ubuntu-latest", "needs": "lint",
        "steps": [{"uses": "actions/checkout@v4"}, {"name": "Install", "run": "pip install -r requirements.txt\npip install pytest"},
                  {"name": "Run tests", "run": "pytest tests/"}]}


def make(tmp_path: Path, jobs: dict, requirements: str = "numpy==1.26.0\npandas\n") -> tuple:
    (tmp_path / "requirements.txt").write_text(requirements, encoding="utf-8")
    wf = tmp_path / "ci.yml"
    wf.write_text(yaml.safe_dump({"name": "CI", "on": {"push": {}}, "jobs": jobs}, sort_keys=False), encoding="utf-8")
    return parse_workflow(wf), tmp_path


def plan(tmp_path, jobs, requirements="numpy==1.26.0\npandas\n", step_seconds=None):
    wf, root = make(tmp_path, jobs, requirements)
    return wf, root, plan_install_pruning(wf, root, {j: 60.0 for j in jobs}, step_seconds)


def test_tool_only_job_loses_exactly_the_requirements_install(tmp_path):
    wf, root, r = plan(tmp_path, {"lint": LINT, "test": TEST}, step_seconds={("lint", "Install dependencies"): 26.0})
    assert [(a.job, a.removed, a.kept) for a in r.actions] == [("lint", ["pip install -r requirements.txt"], ["pip install ruff"])]
    assert r.actions[0].est_seconds == pytest.approx(13.0)          # median 26 s x 1 of 2 pip lines
    assert "test" in r.skipped and "pytest" in r.skipped["test"]    # runs pytest: needs the dependencies
    out = apply_pruning(wf.raw, r.actions)
    steps = out["jobs"]["lint"]["steps"]
    assert steps[2]["run"] == "pip install ruff" and len(steps) == 4
    assert out["jobs"]["test"] == wf.raw["jobs"]["test"]
    assert validate_pruning(wf, out, root) == []


def test_step_is_dropped_when_nothing_else_remains(tmp_path):
    job = {"runs-on": "ubuntu-latest", "steps": [{"uses": "actions/checkout@v4"},
           {"run": "pip install ruff"}, {"name": "deps", "run": "pip install -r requirements.txt"}, {"run": "ruff check ."}]}
    wf, root, r = plan(tmp_path, {"lint": job})
    out = apply_pruning(wf.raw, r.actions)
    assert [s.get("run") for s in out["jobs"]["lint"]["steps"] if "run" in s] == ["pip install ruff", "ruff check ."]
    assert validate_pruning(wf, out, root) == []


@pytest.mark.parametrize("mutate,why", [
    (lambda j: j["steps"].append({"run": "mypy src"}), "not a stand-alone tool"),
    (lambda j: j["steps"].append({"run": "ruff check . && echo ok"}), "&&"),
    (lambda j: j["steps"].append({"uses": "some/action@v1"}), "may run project code"),
    (lambda j: j["steps"][3].update({"if": "github.event_name == 'push'"}), "if / working-directory"),
    (lambda j: j["steps"][2].update({"run": "pip install -r requirements.txt ruff"}), "mixes a requirements file"),
    (lambda j: j["steps"][2].update({"run": "pip install -e .\npip install ruff"}), "installs the project itself"),
    (lambda j: j["steps"][2].update({"run": "pip install -r requirements.txt"}), "not installed by the job itself"),
    (lambda j: j.update({"strategy": {"matrix": {"py": ["3.9", "3.10"]}}}), "matrix"),
])
def test_job_is_left_alone_when_it_cannot_be_shown_safe(tmp_path, mutate, why):
    import copy
    job = copy.deepcopy(LINT)
    mutate(job)
    _, _, r = plan(tmp_path, {"lint": job})
    assert r.actions == [] and why in r.skipped["lint"]


def test_requirements_file_that_pins_the_tool_blocks_the_prune(tmp_path):
    _, _, r = plan(tmp_path, {"lint": LINT}, requirements="ruff==0.4.0\nnumpy\n")
    assert r.actions == [] and "mentions ruff" in r.skipped["lint"]


def test_nested_or_missing_requirements_block_the_prune(tmp_path):
    _, _, r = plan(tmp_path, {"lint": LINT}, requirements="-r base.txt\n")
    assert r.actions == [] and "nested" in r.skipped["lint"]
    wf, root = make(tmp_path, {"lint": LINT})
    (root / "requirements.txt").unlink()
    assert plan_install_pruning(wf, root, {"lint": 10.0}).actions == []


def test_estimate_feeds_the_critical_path_and_is_none_without_history(tmp_path):
    wf, root, r = plan(tmp_path, {"lint": LINT, "test": TEST}, step_seconds={("lint", "Install dependencies"): 26.0})
    assert r.baseline_seconds == 120.0 and r.pruned_seconds == pytest.approx(107.0)
    _, _, r2 = plan(tmp_path, {"lint": LINT, "test": TEST})
    assert r2.actions[0].est_seconds is None and r2.pruned_seconds == r2.baseline_seconds


def test_validation_rejects_any_other_step_edit(tmp_path):
    wf, root, r = plan(tmp_path, {"lint": LINT, "test": TEST})
    out = apply_pruning(wf.raw, r.actions)
    out["jobs"]["test"]["steps"][2]["run"] = "echo skipped"          # an edit the planner did not make
    assert any("job 'test'" in p for p in validate_pruning(wf, out, root))
