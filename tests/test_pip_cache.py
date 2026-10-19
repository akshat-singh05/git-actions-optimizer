"""Pip caching (gha_optimizer/pip_cache.py): where ``cache: pip`` is added, where it never is, and that the result validates together with install pruning."""
import copy
from pathlib import Path

import pytest
import yaml

from gha_optimizer.ingestion.workflow_parser import parse_workflow
from gha_optimizer.install_pruning import apply_pruning, plan_install_pruning
from gha_optimizer.pip_cache import apply_pip_cache, plan_pip_cache, validate_step_edits

TEST_JOB = {
    "runs-on": "ubuntu-latest",
    "steps": [
        {"uses": "actions/checkout@v4"},
        {"uses": "actions/setup-python@v5", "with": {"python-version": "3.11"}},
        {"run": "python -m pip install --upgrade pip\npip install -r requirements.txt"},
        {"run": "pytest"},
    ],
}


def make(tmp_path: Path, jobs: dict, files=("requirements.txt",)):
    for f in files:
        (tmp_path / f).write_text("numpy\n", encoding="utf-8")
    wf = tmp_path / "ci.yml"
    wf.write_text(yaml.safe_dump({"name": "CI", "on": {"push": {}}, "jobs": jobs}, sort_keys=False), encoding="utf-8")
    return parse_workflow(wf), tmp_path


def test_adds_cache_input_keyed_on_exactly_the_installed_requirement_files(tmp_path):
    wf, root = make(tmp_path, {"test": TEST_JOB})
    r = plan_pip_cache(wf, root)
    assert [(a.job, a.files) for a in r.actions] == [("test", ["requirements.txt"])]
    out = apply_pip_cache(wf.raw, r.actions)
    w = out["jobs"]["test"]["steps"][1]["with"]
    assert w == {"python-version": "3.11", "cache": "pip", "cache-dependency-path": "requirements.txt"}
    assert out["jobs"]["test"]["steps"][2:] == wf.raw["jobs"]["test"]["steps"][2:]
    assert validate_step_edits(wf, out, root, cache=True) == []


def test_two_requirement_files_become_a_multiline_dependency_path(tmp_path):
    job = copy.deepcopy(TEST_JOB)
    job["steps"][2] = {"run": "pip install -r requirements.txt -r dev.txt"}
    wf, root = make(tmp_path, {"test": job}, files=("requirements.txt", "dev.txt"))
    out = apply_pip_cache(wf.raw, plan_pip_cache(wf, root).actions)
    assert out["jobs"]["test"]["steps"][1]["with"]["cache-dependency-path"] == "requirements.txt\ndev.txt"


@pytest.mark.parametrize("mutate,why", [
    (lambda j: j["steps"][1]["with"].update({"cache": "pip"}), "already has a cache input"),
    (lambda j: j.update({"runs-on": "self-hosted"}), "not a plain GitHub-hosted label"),
    (lambda j: j.update({"runs-on": "${{ matrix.os }}"}), "not a plain GitHub-hosted label"),
    (lambda j: j["steps"].append({"uses": "actions/cache@v4", "with": {"path": "x", "key": "k"}}), "its own caching step"),
    (lambda j: j["steps"][2].update({"run": "pip install --no-cache-dir -r requirements.txt"}), "--no-cache-dir"),
    (lambda j: j["steps"][2].update({"run": "pip install -r requirements.txt && pytest"}), "does not parse"),
    (lambda j: j["steps"][2].update({"run": "pip install pytest"}), "installs no requirement file"),
    (lambda j: j["steps"][2].update({"run": "pip install -r missing.txt"}), "not in the checkout"),
])
def test_job_is_left_alone_when_the_input_is_not_provably_safe_and_meaningful(tmp_path, mutate, why):
    job = copy.deepcopy(TEST_JOB)
    mutate(job)
    wf, root = make(tmp_path, {"test": job})
    r = plan_pip_cache(wf, root)
    assert r.actions == [] and why in r.skipped["test"]


def test_a_job_whose_install_is_pruned_gets_no_cache_and_both_edits_validate_together(tmp_path):
    lint = {"runs-on": "ubuntu-latest", "steps": [
        {"uses": "actions/checkout@v4"}, {"uses": "actions/setup-python@v5", "with": {"python-version": "3.11"}},
        {"run": "pip install -r requirements.txt\npip install ruff"}, {"run": "ruff check ."}]}
    wf, root = make(tmp_path, {"lint": lint, "test": TEST_JOB})
    prune = plan_install_pruning(wf, root, {"lint": 10.0, "test": 20.0})
    assert [a.job for a in prune.actions] == ["lint"]
    cache = plan_pip_cache(wf, root, {a.job for a in prune.actions})
    assert [a.job for a in cache.actions] == ["test"] and "pruning" in cache.skipped["lint"]
    out = apply_pruning(apply_pip_cache(wf.raw, cache.actions), prune.actions)
    assert validate_step_edits(wf, out, root, prune=True, cache=True) == []
    bad = copy.deepcopy(out)
    bad["jobs"]["test"]["steps"][3]["run"] = "echo nothing"
    assert any("job 'test'" in p for p in validate_step_edits(wf, bad, root, prune=True, cache=True))
