"""Calibration helpers: statistical sanity checks on generated data (nothing here is a project result)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from gha_optimizer.models.calibration import (
    CalibratedClassifier,
    OutOfSampleCalibrator,
    expected_calibration_error,
    reliability_table,
    render_calibration,
)


def test_ece_is_zero_for_a_calibrated_score_and_large_for_an_overconfident_one():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.0, 1.0, 20000)
    y = (rng.uniform(size=p.size) < p).astype(float)
    assert expected_calibration_error(y, p) < 0.02
    assert expected_calibration_error(y, np.clip(p * 1.0 + 0.0, 0, 1) ** 0.3) > 0.1
    table = reliability_table(y, p)
    assert len(table) == 10 and sum(r["n"] for r in table) == p.size


def _overconfident_data(n=1500, seed=1):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 2))
    true_p = 1 / (1 + np.exp(-(0.8 * x[:, 0] - 1.5)))
    y = (rng.uniform(size=n) < true_p).astype(int)
    return pd.DataFrame(x, columns=["a", "b"]), y


def test_platt_tail_keeps_the_ranking_and_corrects_an_overconfident_base_model():
    X, y = _overconfident_data()
    train, test = slice(0, 1000), slice(1000, 1500)
    over = LogisticRegression(C=1e6, max_iter=1000)
    # an over-confident base model: the same ranking with the logit stretched by 3 (class weights would do the same)
    from sklearn.base import BaseEstimator, ClassifierMixin

    class Stretched(BaseEstimator, ClassifierMixin):
        def __init__(self, stretch=3.0):
            self.stretch = stretch

        def fit(self, X, y):
            self.m_ = LogisticRegression(C=1e6, max_iter=1000).fit(X, y)
            self.classes_ = np.array([0, 1])
            return self

        def predict_proba(self, X):
            z = self.m_.decision_function(X) * self.stretch
            p = 1 / (1 + np.exp(-z))
            return np.column_stack([1 - p, p])

    raw = Stretched().fit(X[train], y[train])
    cal = CalibratedClassifier(Stretched()).fit(X[train], y[train])
    assert cal.calibrated_ and cal.slope_ > 0
    p_raw, p_cal = raw.predict_proba(X[test])[:, 1], cal.predict_proba(X[test])[:, 1]
    assert brier_score_loss(y[test], p_cal) < brier_score_loss(y[test], p_raw)
    assert roc_auc_score(y[test], p_cal) == pytest.approx(roc_auc_score(y[test], p_raw), abs=0.02)   # ranking preserved
    assert over is not None


def test_too_little_data_leaves_the_model_uncalibrated_and_says_so():
    X, y = _overconfident_data(n=60)
    cal = CalibratedClassifier(LogisticRegression(max_iter=500)).fit(X, y)
    assert not cal.calibrated_ and "too little data" in cal.calibration_note_
    p = cal.predict_proba(X)[:, 1]
    assert p.min() >= 0 and p.max() <= 1


def test_lookup_reports_what_happened_to_unseen_rows_and_flags_extrapolation():
    rng = np.random.default_rng(3)
    scores = rng.uniform(0.0, 0.5, 2000)
    outcomes = (rng.uniform(size=2000) < scores).astype(float)
    oos = OutOfSampleCalibrator(scores, outcomes)
    assert oos.usable
    inside = oos.lookup(0.45)
    assert inside["available"] and not inside["outside_range"] and inside["rows"] > 100
    assert abs(inside["observed_rate"] - 0.45) < 0.12
    outside = oos.lookup(0.93)                                   # higher than anything seen on unseen rows
    assert outside["outside_range"] and outside["highest_score_seen"] < 0.51
    assert (oos.transform(np.array([0.1, 0.4])) >= 0).all()
    assert not OutOfSampleCalibrator(scores[:20], outcomes[:20]).usable


def test_render_handles_missing_results():
    text = render_calibration({"o/r": {"note": "no ingested rows"}}, 10)
    assert "no ingested rows" in text and "Brier" in text
