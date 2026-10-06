"""
Model C — Duration Estimator (regression).

Predicts per-job runtime in seconds from real ``completed_at - started_at``
timestamps (success/failure rows only; cancelled/skipped jobs have no duration
label), optionally with quantile models (P10/P50/P90).  Feeds the cost-aware
runner allocator in ``gha_optimizer.scheduler.optimizer``.

Evaluation is a temporal holdout compared against the naive baseline "predict the
job's strictly-prior decayed mean duration" — a model that cannot beat that
baseline is not evidence of learning.
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
    ):
        from gha_optimizer.config import config

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
            )
            self._backend_name = "xgboost"
        else:
            self.model = HistGradientBoostingRegressor(
                max_iter=n_estimators, max_depth=max_depth, learning_rate=learning_rate,
                random_state=random_state,
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

    def fit(self, df: pd.DataFrame) -> dict:
        """Train on ``label_duration``; return temporal-holdout metrics incl. the naive baseline."""
        d = chronological(df).dropna(subset=["label_duration"])
        metrics: dict[str, Any] = {
            "backend": self._backend_name,
            "n_rows_total": int(len(df)),
            "n_rows_labelled": int(len(d)),
            "n_rows_excluded_null_label": int(len(df) - len(d)),
        }
        if len(d) < 2:
            raise ValueError(f"duration model needs labelled rows (labelled={len(d)})")
        y_all = d["label_duration"].astype(float)
        features = usable_features(d, FEATURE_COLUMNS)
        metrics["features_dropped_all_nan"] = [c for c in FEATURE_COLUMNS if c not in features]

        train, test, info = temporal_split(d, self.test_fraction)
        importance: dict[str, float] = {}
        if len(test) and len(train) >= 2:
            train_feats = usable_features(train, FEATURE_COLUMNS)
            holdout_model = clone(self.model).fit(train[train_feats].astype(float), train["label_duration"].astype(float))
            X_test = test[train_feats].astype(float)
            y_test = test["label_duration"].astype(float)
            pred = np.maximum(holdout_model.predict(X_test), 0.0)
            baseline = test["historical_mean_duration"].astype(float).to_numpy()
            metrics["validation"] = f"temporal holdout (last {int(self.test_fraction * 100)}% of runs)"
            metrics.update(info)
            metrics.update(regression_report(y_test, pred, baseline))
            importance = permutation_importance_dict(
                holdout_model, X_test, y_test, "neg_mean_absolute_error", self.random_state
            )
        else:
            metrics["validation"] = "none: no temporal holdout possible (too few runs)"

        X_all = d[features].astype(float)
        self._features = features
        self.model.fit(X_all, y_all)
        for qmodel in self._quantile_models.values():
            qmodel.fit(X_all, y_all)
        self._fitted = True
        if hasattr(self.model, "feature_importances_"):
            importance = dict(zip(features, self.model.feature_importances_))
        self._feature_importance = importance
        return metrics

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Point-estimate duration in seconds per row (clamped at 0)."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict().")
        preds = self.model.predict(df[self._features].astype(float))
        return pd.Series(np.maximum(preds, 0.0), index=df.index)

    def predict_quantiles(self, df: pd.DataFrame) -> pd.DataFrame:
        """Columns ``q0.1``, ``q0.5``, ``q0.9`` per row (sklearn backend only)."""
        if not self._quantile_models:
            p = self.predict(df)
            return pd.DataFrame({f"q{q}": p for q in self.quantiles}, index=df.index)
        result = {}
        for q, qmodel in self._quantile_models.items():
            result[f"q{q}"] = np.maximum(qmodel.predict(df[self._features].astype(float)), 0.0)
        return pd.DataFrame(result, index=df.index)

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"model": self.model, "quantile_models": self._quantile_models, "backend": self._backend_name,
             "features": self._features},
            path,
        )

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._quantile_models = data.get("quantile_models", {})
        self._backend_name = data.get("backend", "sklearn-hist")
        self._features = data.get("features", list(FEATURE_COLUMNS))
        self._fitted = True
