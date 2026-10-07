"""Duration target 'log_ratio': regress a correction to the job's own prior mean instead of raw seconds.

Histories are the explicit SYNTHETIC fixture - these tests check the mechanics, never a result.
"""
from __future__ import annotations

import math

import networkx as nx
import numpy as np
import pytest

from gha_optimizer.features.feature_extraction import build_training_frame
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.evaluation import from_log_ratio, log_ratio
from gha_optimizer.models.rolling_eval import TARGETS, evaluate_target
from tests.fixtures.synthetic_history import generate_synthetic_test_history


def frame(n_runs: int = 80):
    return build_training_frame(nx.DiGraph(), generate_synthetic_test_history(n_runs=n_runs, seed=11))


def test_log_ratio_round_trips_and_is_finite_for_subsecond_jobs():
    d = np.array([0.0, 0.4, 30.0, 900.0])
    p = np.array([5.0, 0.2, 30.0, 100.0])
    assert np.allclose(from_log_ratio(p, log_ratio(d, p)), d)
    assert np.isfinite(log_ratio(d, p)).all()
    assert log_ratio(30.0, 30.0) == 0.0                           # same as the prior -> ratio 1 -> log 0
    assert (from_log_ratio(p, np.full(4, -50.0)) >= 0).all()      # never negative seconds


def test_model_recovers_a_known_multiplicative_relationship():
    """If every run took exactly 1.5x the job's prior mean, the ratio model must predict ~1.5x the prior."""
    df = frame()
    has_prior = df["historical_mean_duration"].notna()
    df.loc[has_prior, "label_duration"] = df.loc[has_prior, "historical_mean_duration"] * 1.5
    model = DurationEstimator(backend="sklearn", target="log_ratio")
    model.fit(df)
    probe = df[has_prior].tail(40)
    pred = model.predict(probe).to_numpy()
    want = probe["historical_mean_duration"].to_numpy() * 1.5
    assert np.median(np.abs(pred / want - 1.0)) < 0.05


def test_rows_without_a_prior_use_the_training_mean_not_nan():
    df = frame()
    model = DurationEstimator(backend="sklearn", target="log_ratio")
    metrics = model.fit(df)
    assert metrics["target"] == "log_ratio"
    first = df[df["historical_mean_duration"].isna()].head(5)
    assert len(first) > 0
    pred = model.predict(first)
    assert pred.notna().all() and (pred >= 0).all()
    assert not math.isnan(model._fallback_prior) and model._fallback_prior > 0


def test_log_ratio_is_the_default_and_seconds_remains_available():
    df = frame()
    assert DurationEstimator().target == "log_ratio"
    m = DurationEstimator(backend="sklearn", target="seconds")
    metrics = m.fit(df)
    assert metrics["target"] == "seconds"
    assert (m.predict(df.tail(10)) >= 0).all()
    assert DurationEstimator(backend="sklearn").fit(df)["target"] == "log_ratio"


def test_loaded_artifacts_keep_the_target_they_were_trained_with(tmp_path):
    df = frame()
    old = DurationEstimator(backend="sklearn", target="seconds")
    old.fit(df)
    old.save(tmp_path / "old.joblib")
    loaded = DurationEstimator(backend="sklearn")             # constructor default is now log_ratio ...
    loaded.load(tmp_path / "old.joblib")
    assert loaded.target == "seconds"                         # ... but the artifact decides
    assert np.allclose(loaded.predict(df.tail(10)), old.predict(df.tail(10)))


def test_the_seconds_evaluation_target_really_uses_the_seconds_estimator():
    from sklearn.ensemble import HistGradientBoostingRegressor
    est = TARGETS["duration"].estimator()
    ratio = TARGETS["duration_ratio"].estimator()
    if isinstance(est, HistGradientBoostingRegressor):
        assert est.loss == "squared_error" and ratio.loss == "absolute_error"


def test_target_and_fallback_survive_save_load(tmp_path):
    df = frame()
    m = DurationEstimator(backend="sklearn", target="log_ratio")
    m.fit(df)
    m.save(tmp_path / "d.joblib")
    loaded = DurationEstimator(backend="sklearn")
    loaded.load(tmp_path / "d.joblib")
    assert loaded.target == "log_ratio" and loaded._fallback_prior == m._fallback_prior
    probe = df.tail(10)
    assert np.allclose(loaded.predict(probe), m.predict(probe))
    # quantile predictions go through the same back-transform and stay ordered
    q = loaded.predict_quantiles(probe)
    assert (q["q0.1"] <= q["q0.5"] + 1e-9).all() and (q["q0.5"] <= q["q0.9"] + 1e-9).all()


def test_unknown_target_is_rejected():
    with pytest.raises(ValueError):
        DurationEstimator(target="bogus")


def test_rolling_evaluation_scores_the_ratio_target_against_the_same_baseline():
    df = frame(120)
    a = evaluate_target(df, TARGETS["duration"], n_folds=4, min_train_runs=10, n_boot=50)
    b = evaluate_target(df, TARGETS["duration_ratio"], n_folds=4, min_train_runs=10, n_boot=50)
    assert b["folds_used"] == a["folds_used"] > 0
    assert b["n_test_rows"] == a["n_test_rows"]                   # identical test rows -> like-for-like
    assert b["scores"]["baseline"] == a["scores"]["baseline"]     # the baseline does not depend on the target transform
    assert b["scores"]["all"]["mae"] is not None
