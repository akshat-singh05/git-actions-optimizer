"""A workflow in which no step can restore a cache has no cache labels by construction: the report-only cache model is skipped (NaN, never a default).

The history in the pipeline test is the explicit SYNTHETIC test fixture (wiring only; no number from it is a result)."""
import copy
import dataclasses
import math
import tempfile
from pathlib import Path

import yaml

from gha_optimizer.graph.ci_semantics import workflow_cannot_restore_cache
from gha_optimizer.models.cache_miss_predictor import NoCacheModel
from gha_optimizer.pipeline import run_pipeline
from tests.fixtures.synthetic_history import generate_synthetic_test_history

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def jobs(*steps):
    return {"j": {"runs-on": "ubuntu-latest", "steps": [dict(s) for s in steps]}}


def test_plain_workflows_cannot_restore_a_cache():
    assert workflow_cannot_restore_cache(jobs({"uses": "actions/checkout@v4"}, {"uses": "actions/setup-python@v5", "with": {"python-version": "3.11"}}, {"run": "pytest"}))
    assert workflow_cannot_restore_cache(jobs({"uses": "actions/setup-node@v4", "with": {"node-version": 20}}))


def test_anything_that_may_cache_or_is_unknown_returns_false():
    assert not workflow_cannot_restore_cache(jobs({"uses": "actions/cache@v4", "with": {"path": "x", "key": "k"}}))
    assert not workflow_cannot_restore_cache(jobs({"uses": "actions/setup-python@v5", "with": {"cache": "pip"}}))
    assert not workflow_cannot_restore_cache(jobs({"uses": "astral-sh/setup-uv@v5"}))            # caches by default
    assert not workflow_cannot_restore_cache(jobs({"uses": "Swatinem/rust-cache@v2"}))
    assert not workflow_cannot_restore_cache(jobs({"uses": "./.github/actions/setup"}))            # contents unknown
    assert not workflow_cannot_restore_cache({"j": {"uses": "./.github/workflows/reusable.yml"}})  # contents unknown


def test_no_cache_model_is_not_applicable_never_a_default():
    import pandas as pd

    m = NoCacheModel()
    assert "not applicable" in m.fit(pd.DataFrame({"a": [1, 2]}))["status"]
    p = m.predict_proba(pd.DataFrame({"a": [1, 2]}, index=["lint", "test"]))
    assert list(p.index) == ["lint", "test"] and all(math.isnan(v) for v in p)


def test_pipeline_runs_without_cache_labels_when_the_workflow_cannot_cache(tmp_path):
    spec = yaml.safe_load((FIXTURES / "sample_workflow.yml").read_text(encoding="utf-8"))
    for body in spec["jobs"].values():
        body["steps"] = [s for s in body["steps"] if "cache" not in str(s.get("uses", ""))]
    wf = tmp_path / "ci.yml"
    wf.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    history = [dataclasses.replace(r, cache_hit=None) for r in generate_synthetic_test_history(n_runs=50)]
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        out = f.name
    plan, explanation = run_pipeline(workflow_path=wf, repo_root=FIXTURES / "sample_repo", tracked_files=["src/app.py", "requirements.txt"], history=history,
                                     changed_files=["src/app.py"], out_path=out)
    assert len(plan.jobs) == 5 and plan.baseline_wall_clock_seconds > 0
    assert "n/a (no step can restore a cache)" in explanation and "not defined for this workflow" in explanation
    assert all(math.isnan(jp.predicted_cache_miss_prob) for jp in plan.jobs)
    assert copy.deepcopy(plan.time_saved_pct) == 0.0          # the default plan is unchanged by this
