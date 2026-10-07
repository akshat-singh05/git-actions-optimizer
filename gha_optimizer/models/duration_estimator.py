"""
Model C — Duration Estimator (regression).

Predicts per-job runtime in seconds from real ``completed_at - started_at``
timestamps (success/failure rows only; cancelled/skipped jobs have no duration
label), optionally with quantile models (P10/P50/P90).  Feeds the cost-aware
runner allocator in ``gha_optimizer.scheduler.optimizer``.

Evaluation is a temporal holdout compared against the naive baseline "predict the
job's strictly-prior decayed mean duration" — a model that cannot beat that
baseline is not evidence of learning.

Two targets (the ``target`` argument):

* ``"log_ratio"`` (default): regress ``log((duration+1)/(prior_mean+1))`` with an absolute-error loss and return
  ``prior_mean * exp(prediction)``; the model only has to learn a *correction* to the job's own history
  instead of rediscovering job identity from a handful of rows.  Rows without a prior (a job's first
  runs) cannot be trained on and are predicted from the mean duration of the training data.
  On the ingested repositories it beat raw seconds everywhere and the prior-mean baseline in poetry and
  fastapi (docs/MODEL_EVAL.md); it is *not* better than the baseline in prometheus and dubbo.
* ``"seconds"``: regress the duration directly (kept for comparison; significantly worse than the
  prior-mean baseline in every repository).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import HistGradientBoostingRegressor

from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS
from gha_optimizer.models.evaluation import (
    chronological,
    from_log_ratio,
    log_ratio,
    permutation_importance_dict,
    regression_report,
    temporal_split,
    usable_features,
)

try:
    from xgboost import XGBRegressor
    _HAS_XGBOOST = True
except ImportError:
    _HAS_XGBOOST = False


class DurationEstimator:
    """Predict job duration in seconds, with optional quantile estimates."""

    def __init__(
        self,
        backend: str = "auto",
        n_estimators: int = 150,
        max_depth: int = 3,
        learning_rate: float = 0.08,
        random_state: int = 13,
        quantiles: tuple[float, ...] = (0.1, 0.5, 0.9),
        test_fraction: float | None = None,
        target: str = "log_ratio",
    ):
        from gha_optimizer.config import config

        if target not in ("seconds", "log_ratio"):
            raise ValueError(f"unknown duration target {target!r}; expected 'seconds' or 'log_ratio'")
        self.target = target
        self._fallback_prior = float("nan")  # mean training duration; prior for rows without history (log_ratio)
        self.random_state = random_state
        self.quantiles = quantiles
        self.test_fraction = test_fraction if test_fraction is not None else config.model.temporal_test_fraction

        if backend == "xgboost" and not _HAS_XGBOOST:
            raise ImportError("XGBoost requested but not installed.")
        use_xgb = _HAS_XGBOOST if backend == "auto" else backend == "xgboost"
        if use_xgb:
            self.model: Any = XGBRegressor(
                n_estimators=n_estimators, max_depth=max_depth, learning_rate=learning_rate,
                random_state=random_state,
                **({"objective": "reg:absoluteerror"} if target == "log_ratio" else {}),
            )
            self._backend_name = "xgboost"
        else:
            self.model = HistGradientBoostingRegressor(
                max_iter=n_estimators, max_depth=max_depth, learning_rate=learning_rate,
                random_state=random_state,
                loss="absolute_error" if target == "log_ratio" else "squared_error",
            )
            self._backend_name = "sklearn-hist"

        # Quantile regression models (sklearn backend only)
        self._quantile_models: dict[float, Any] = {}
        if not use_xgb:
            for q in quantiles:
                self._quantile_models[q] = HistGradientBoostingRegressor(
                    max_iter=n_estimators, max_depth=max_depth, learning_rate=learning_rate,
                    random_state=random_state, loss="quantile", quantile=q,
                )

        self._fitted = False
        self._features: list[str] = list(FEATURE_COLUMNS)
        self._feature_importance: dict[str, float] = {}

    # ---- target transform (identity for "seconds") -------------------------------------------------
    def _trainable(self, df: pd.DataFrame) -> pd.DataFrame:
        """Rows the model can learn from: for ``log_ratio`` only those with a prior to take the ratio against."""
        return df if self.target == "seconds" else df[df["historical_mean_duration"].notna()]

    def _target(self, df: pd.DataFrame) -> np.ndarray:
        y = df["label_duration"].astype(float).to_numpy()
        if self.target == "seconds":
            return y
        return log_ratio(y, df["historical_mean_duration"].astype(float).to_numpy())

    def _to_seconds(self, df: pd.DataFrame, raw: np.ndarray, fallback: float) -> np.ndarray:
        if self.target == "seconds":
            return np.maximum(raw, 0.0)
        prior = df["historical_mean_duration"].astype(float).fillna(fallback).to_numpy()
        return from_log_ratio(prior, raw)

    def fit(self, df: pd.DataFrame) -> dict:
        """Train on ``label_duration``; return temporal-holdout metrics incl. the naive baseline."""
        d = chronological(df).dropna(subset=["label_duration"])
        metrics: dict[str, Any] = {
            "backend": self._backend_name,
            "target": self.target,
            "n_rows_total": int(len(df)),
            "n_rows_labelled": int(len(d)),
            "n_rows_excluded_null_label": int(len(df) - len(d)),
        }
        if len(self._trainable(d)) < 2:
            raise ValueError(f"duration model needs labelled rows (labelled={len(d)}, with a prior={len(self._trainable(d))})")
        features = usable_features(d, FEATURE_COLUMNS)
        metrics["features_dropped_all_nan"] = [c for c in FEATURE_COLUMNS if c not in features]

        train, test, info = temporal_split(d, self.test_fraction)
        importance: dict[str, float] = {}
        tr = self._trainable(train)
        if len(test) and len(tr) >= 2:
            train_feats = usable_features(tr, FEATURE_COLUMNS)
            fb = float(train["label_duration"].mean())
            holdout_model = clone(self.model).fit(tr[train_feats].astype(float), self._target(tr))
            X_test = test[train_feats].astype(float)
            y_test = test["label_duration"].astype(float)
            pred = self._to_seconds(test, holdout_model.predict(X_test), fb)
            baseline = test["historical_mean_duration"].astype(float).to_numpy()
            metrics["validation"] = f"temporal holdout (last {int(self.test_fraction * 100)}% of runs)"
            metrics.update(info)
            metrics.update(regression_report(y_test, pred, baseline))
            importance = permutation_importance_dict(
                holdout_model, X_test, y_test, "neg_mean_absolute_error", self.random_state
            )
        else:
            metrics["validation"] = "none: no temporal holdout possible (too few runs)"

        dt = self._trainable(d)
        features = usable_features(dt, FEATURE_COLUMNS)
        X_all, y_fit = dt[features].astype(float), self._target(dt)
        self._features = features
        self._fallback_prior = float(d["label_duration"].mean())
        self.model.fit(X_all, y_fit)
        for qmodel in self._quantile_models.values():
            qmodel.fit(X_all, y_fit)
        self._fitted = True
        if hasattr(self.model, "feature_importances_"):
            importance = dict(zip(features, self.model.feature_importances_))
        self._feature_importance = importance
        return metrics

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Point-estimate duration in seconds per row (clamped at 0)."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict().")
        raw = self.model.predict(df[self._features].astype(float))
        return pd.Series(self._to_seconds(df, raw, self._fallback_prior), index=df.index)

    def predict_quantiles(self, df: pd.DataFrame) -> pd.DataFrame:
        """Columns ``q0.1``, ``q0.5``, ``q0.9`` per row (sklearn backend only)."""
        if not self._quantile_models:
            p = self.predict(df)
            return pd.DataFrame({f"q{q}": p for q in self.quantiles}, index=df.index)
        result = {}
        for q, qmodel in self._quantile_models.items():
            result[f"q{q}"] = self._to_seconds(df, qmodel.predict(df[self._features].astype(float)), self._fallback_prior)
        return pd.DataFrame(result, index=df.index)

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"model": self.model, "quantile_models": self._quantile_models, "backend": self._backend_name,
             "features": self._features, "target": self.target, "fallback_prior": self._fallback_prior,
             "feature_importance": self._feature_importance},
            path,
        )

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._quantile_models = data.get("quantile_models", {})
        self._backend_name = data.get("backend", "sklearn-hist")
        self._features = data.get("features", list(FEATURE_COLUMNS))
        self.target = data.get("target", "seconds")  # artifacts saved before the target option are 'seconds'
        # (the constructor default is now "log_ratio"; a loaded artifact always keeps the target it was trained with)
        self._fallback_prior = data.get("fallback_prior", float("nan"))
        self._feature_importance = data.get("feature_importance", {})
        self._fitted = True
