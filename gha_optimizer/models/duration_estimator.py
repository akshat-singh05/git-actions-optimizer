"""
Model C — Duration Estimator (regression).

Predicts per-job runtime in seconds, optionally with quantile regression
for uncertainty estimation (P10/P50/P90 bands).  Feeds directly into the
cost-aware runner allocator in ``gha_optimizer.scheduler.optimizer``.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import KFold, cross_val_predict, train_test_split
from sklearn.metrics import (
    mean_absolute_error,
    mean_absolute_percentage_error,
    r2_score,
)

from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS

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
    ):
        self.random_state = random_state
        self.quantiles = quantiles

        use_xgb = (backend == "auto" and _HAS_XGBOOST) or backend == "xgboost"
        if backend == "xgboost" and not _HAS_XGBOOST:
            raise ImportError("XGBoost requested but not installed.")

        if use_xgb:
            self.model = XGBRegressor(
                n_estimators=n_estimators,
                max_depth=max_depth,
                learning_rate=learning_rate,
                random_state=random_state,
            )
            self._backend_name = "xgboost"
        else:
            self.model = GradientBoostingRegressor(
                n_estimators=n_estimators,
                max_depth=max_depth,
                learning_rate=learning_rate,
                random_state=random_state,
            )
            self._backend_name = "sklearn"

        # Quantile regression models (sklearn only — uses loss='quantile')
        self._quantile_models: dict[float, GradientBoostingRegressor] = {}
        if not use_xgb:
            for q in quantiles:
                self._quantile_models[q] = GradientBoostingRegressor(
                    n_estimators=n_estimators,
                    max_depth=max_depth,
                    learning_rate=learning_rate,
                    random_state=random_state,
                    loss="quantile",
                    alpha=q,
                )

        self._fitted = False
        self._feature_importance: dict[str, float] = {}

    def fit(self, df: pd.DataFrame, cv: bool = True) -> dict:
        """
        Train the model and compute validation metrics.

        Returns dict with MAE, MAPE, R², and backend info.
        """
        X = df[FEATURE_COLUMNS].copy()
        y = df["label_duration"]

        if cv and len(y) >= 20:
            metrics = self._fit_with_cv(X, y)
        else:
            metrics = self._fit_holdout(X, y)

        # Train quantile models on full data
        for q, qmodel in self._quantile_models.items():
            qmodel.fit(X, y)

        if hasattr(self.model, "feature_importances_"):
            self._feature_importance = dict(
                zip(FEATURE_COLUMNS, self.model.feature_importances_)
            )

        return metrics

    def _fit_with_cv(self, X: pd.DataFrame, y: pd.Series) -> dict:
        kf = KFold(n_splits=5, shuffle=True, random_state=self.random_state)

        preds = cross_val_predict(self.model, X, y, cv=kf)

        # Refit on full data
        self.model.fit(X, y)
        self._fitted = True

        return {
            "backend": self._backend_name,
            "validation": "5-fold CV",
            "mae_seconds": round(mean_absolute_error(y, preds), 2),
            "mape": round(mean_absolute_percentage_error(y, preds), 4),
            "r2": round(r2_score(y, preds), 4),
        }

    def _fit_holdout(self, X: pd.DataFrame, y: pd.Series) -> dict:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=self.random_state
        )
        self.model.fit(X_train, y_train)
        self._fitted = True

        preds = self.model.predict(X_test)
        return {
            "backend": self._backend_name,
            "validation": "holdout 75/25",
            "mae_seconds": round(mean_absolute_error(y_test, preds), 2),
            "mape": round(mean_absolute_percentage_error(y_test, preds), 4),
            "r2": round(r2_score(y_test, preds), 4),
        }

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Return point-estimate duration in seconds per row."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict().")
        preds = self.model.predict(df[FEATURE_COLUMNS])
        # Clamp to non-negative
        return pd.Series(np.maximum(preds, 0.0), index=df.index)

    def predict_quantiles(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return a DataFrame with columns like ``q0.1``, ``q0.5``, ``q0.9``
        for each input row.  Only available when using the sklearn backend.
        """
        if not self._quantile_models:
            # Fallback: return the point estimate for all quantiles
            p = self.predict(df)
            return pd.DataFrame(
                {f"q{q}": p for q in self.quantiles}, index=df.index
            )

        result = {}
        for q, qmodel in self._quantile_models.items():
            preds = qmodel.predict(df[FEATURE_COLUMNS])
            result[f"q{q}"] = np.maximum(preds, 0.0)
        return pd.DataFrame(result, index=df.index)

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "quantile_models": self._quantile_models,
                "backend": self._backend_name,
            },
            path,
        )

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._quantile_models = data.get("quantile_models", {})
        self._backend_name = data.get("backend", "sklearn")
        self._fitted = True
