"""Persisted models are used only when they were trained on exactly the data being scored.

Histories are the explicit SYNTHETIC fixture - these tests check the mechanism, never a result.
"""
from __future__ import annotations

import copy
import json
import random
import tempfile
from pathlib import Path

import networkx as nx
import numpy as np

from gha_optimizer.features.feature_extraction import build_inference_frame, build_training_frame
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.failure_predictor import FailurePredictor
from gha_optimizer.models.store import ARTIFACTS, ModelStore, fingerprint
from gha_optimizer.pipeline import run_pipeline
from tests.fixtures.synthetic_history import generate_synthetic_test_history

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FILES = ["src/app.py", "src/utils.py", "src/api/routes.py", "tests/unit/test_app.py",
         "tests/integration/test_api.py", "requirements.txt"]


def fit_all(history):
    df = build_training_frame(nx.DiGraph(), history)
    models = {"cache_miss": CacheMissPredictor(), "failure": FailurePredictor(), "duration": DurationEstimator()}
    metrics = {k: m.fit(df) for k, m in models.items()}
    return df, models, metrics


def test_fingerprint_ignores_input_order_but_sees_any_data_change():
    h = generate_synthetic_test_history(n_runs=30)
    shuffled = h[:]
    random.Random(2).shuffle(shuffled)
    assert fingerprint(h) == fingerprint(shuffled)
    changed = copy.deepcopy(h)
    changed[7].duration_seconds = (changed[7].duration_seconds or 0) + 1.0
    assert fingerprint(changed) != fingerprint(h)
    relabelled = copy.deepcopy(h)
    relabelled[3].conclusion = "failure" if relabelled[3].conclusion != "failure" else "success"
    assert fingerprint(relabelled) != fingerprint(h)
    assert fingerprint(h[:-1]) != fingerprint(h)


def test_fingerprint_sees_the_feature_contract_and_the_estimators(monkeypatch):
    h = generate_synthetic_test_history(n_runs=10)
    base = fingerprint(h)
    import gha_optimizer.models.store as store

    monkeypatch.setattr(store, "FEATURE_COLUMNS", list(store.FEATURE_COLUMNS)[:-1])
    assert fingerprint(h) != base
    monkeypatch.undo()
    monkeypatch.setattr(store, "SCHEMA_VERSION", store.SCHEMA_VERSION + 1)
    assert fingerprint(h) != base


def test_round_trip_reproduces_predictions_metrics_and_importance(tmp_path):
    history = generate_synthetic_test_history(n_runs=50)
    df, models, metrics = fit_all(history)
    store, fp = ModelStore(tmp_path), fingerprint(history)
    store.save(fp, ".github/workflows/ci.yml", models, metrics)

    got = store.try_load(fp, ".github/workflows/ci.yml")
    assert got is not None
    loaded, loaded_metrics = got
    assert json.loads(json.dumps(metrics, default=str)) == json.loads(json.dumps(loaded_metrics, default=str))
    probe = build_inference_frame(nx.DiGraph(), sorted({r.job_id for r in history}), ["src/app.py"], {})
    probe.index = probe["job_id"]
    for name in ARTIFACTS:
        assert loaded[name].feature_importance == models[name].feature_importance
    assert np.allclose(loaded["cache_miss"].predict_proba(probe), models["cache_miss"].predict_proba(probe))
    assert np.allclose(loaded["failure"].predict_proba(probe), models["failure"].predict_proba(probe))
    assert np.allclose(loaded["duration"].predict(probe), models["duration"].predict(probe))
    assert loaded["duration"].target == models["duration"].target


def test_nothing_is_loaded_for_other_data_missing_corrupt_or_partial_artifacts(tmp_path):
    history = generate_synthetic_test_history(n_runs=50)
    _, models, metrics = fit_all(history)
    store, fp = ModelStore(tmp_path), fingerprint(history)
    assert store.try_load(fp, "wf") is None                                   # empty directory
    d = store.save(fp, "wf", models, metrics)
    assert store.try_load("a-different-fingerprint", "wf") is None            # different data -> refit
    assert store.try_load(fp, "another-workflow") is None                     # slots are per workflow
    (d / "failure.joblib").write_bytes(b"not a joblib file")
    assert store.try_load(fp, "wf") is None                                   # corrupt artifact -> refit, no crash
    store.save(fp, "wf", models, metrics)
    (d / "duration.joblib").unlink()
    assert store.try_load(fp, "wf") is None                                   # partial set -> refit
    store.save(fp, "wf", models, metrics)
    (d / "manifest.json").unlink()
    assert store.try_load(fp, "wf") is None                                   # artifacts without a manifest are not trusted


def _run(history, model_dir):
    with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
        out = f.name
    return run_pipeline(
        workflow_path=FIXTURES / "sample_workflow.yml", repo_root=FIXTURES / "sample_repo", tracked_files=FILES,
        history=history, changed_files=["src/api/routes.py"], out_path=out, model_dir=model_dir,
    )


def test_pipeline_fits_and_saves_then_loads_then_refits_when_the_data_changes(tmp_path):
    history = generate_synthetic_test_history(n_runs=50)
    plan1, text1 = _run(history, tmp_path)
    assert "fit on the ingested history just now and saved to" in text1
    plan2, text2 = _run(history, tmp_path)
    assert "loaded from" in text2 and "fit on the ingested history" not in text2
    assert plan2.estimated_wall_clock_seconds == plan1.estimated_wall_clock_seconds       # same models -> same plan
    assert [j.predicted_failure_prob for j in plan2.jobs] == [j.predicted_failure_prob for j in plan1.jobs]
    assert "Top feature importances" in text2 and "- " in text2.split("Top feature importances")[1]

    changed = copy.deepcopy(history)
    changed[5].duration_seconds = (changed[5].duration_seconds or 0) + 5.0
    _, text3 = _run(changed, tmp_path)
    assert "fit on the ingested history just now and saved to" in text3                   # stale models were not used


def test_default_never_touches_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, text = _run(generate_synthetic_test_history(n_runs=50), None)
    assert "fit on the ingested history just now" in text and "saved to" not in text
    assert list(tmp_path.iterdir()) == []
