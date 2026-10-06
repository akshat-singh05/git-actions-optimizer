"""
Model A — Cache-Miss Predictor (tabular baseline).

Predicts P(cache_miss) for a job on the current diff, trained on **log-derived**
labels only.  Rows whose cache label is NULL (no log / no cache line) are excluded
— never defaulted — and counted in the returned metrics.

Estimator: gradient-boosted trees that accept NaN natively
(``HistGradientBoostingClassifier``; XGBoost when importable), because real
features are legitimately missing at cold start.  Evaluation is a **temporal
holdout** (see :mod:`gha_optimizer.models.evaluation`), not shuffled CV.

The GNN-based upgrade lives in ``gnn_cache_predictor.py`` and does not change
this interface.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import HistGradientBoostingClassifier

from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS
from gha_optimizer.models.evaluation import (
    binary_report,
    chronological,
    permutation_importance_dict,
    temporal_split,
    usable_features,
)

try:
    from xgboost import XGBClassifier
    _HAS_XGBOOST = True
except ImportError:
    _HAS_XGBOOST = False


class CacheMissPredictor:
    """Predict P(cache_miss) per job using tabular features."""

    def __init__(
        self,
        backend: str = "auto",
        n_estimators: int = 150,
        max_depth: int = 3,
        learning_rate: float = 0.08,
        random_state: int = 13,
        test_fraction: float | None = None,
    ):
        """``backend``: ``"auto"`` (XGBoost if installed, else sklearn hist-GBT) | ``"sklearn"`` | ``"xgboost"``."""
        from gha_optimizer.config import config

        self.random_state = random_state
        self.test_fraction = test_fraction if test_fraction is not None else config.model.temporal_test_fraction

        if backend == "xgboost" and not _HAS_XGBOOST:
            raise ImportError("XGBoost requested but not installed.")
        use_xgb = _HAS_XGBOOST if backend == "auto" else backend == "xgboost"
        if use_xgb:
            self.model: Any = XGBClassifier(
                n_estimators=n_estimators, max_depth=max_depth, learning_rate=learning_rate,
                random_state=random_state, eval_metric="logloss",
            )
            self._backend_name = "xgboost"
        else:
            self.model = HistGradientBoostingClassifier(
                max_iter=n_estimators, max_depth=max_depth, learning_rate=learning_rate,
                random_state=random_state,
            )
            self._backend_name = "sklearn-hist"

        self._fitted = False
        self._features: list[str] = list(FEATURE_COLUMNS)
        self._feature_importance: dict[str, float] = {}

    def fit(self, df: pd.DataFrame) -> dict:
        """Train on ``label_cache_hit`` (predicting MISS = 1 - hit); return temporal-holdout metrics."""
        d = chronological(df).dropna(subset=["label_cache_hit"])
        metrics: dict[str, Any] = {
            "backend": self._backend_name,
            "n_rows_total": int(len(df)),
            "n_rows_labelled": int(len(d)),
            "n_rows_excluded_null_label": int(len(df) - len(d)),
        }
        y_all = 1 - d["label_cache_hit"].astype(int)
        if len(d) == 0 or y_all.nunique() < 2:
            raise ValueError(
                f"cache-miss model needs labelled rows of both classes "
                f"(labelled={len(d)}, classes={sorted(y_all.unique().tolist())})"
            )
        metrics["miss_rate_all_labelled"] = round(float(y_all.mean()), 4)
        features = usable_features(d, FEATURE_COLUMNS)
        metrics["features_dropped_all_nan"] = [c for c in FEATURE_COLUMNS if c not in features]

        train, test, info = temporal_split(d, self.test_fraction)
        y_train = 1 - train["label_cache_hit"].astype(int)
        importance: dict[str, float] = {}
        if len(test) and y_train.nunique() == 2:
            train_feats = usable_features(train, FEATURE_COLUMNS)
            holdout_model = clone(self.model).fit(train[train_feats].astype(float), y_train)
            X_test = test[train_feats].astype(float)
            y_test = 1 - test["label_cache_hit"].astype(int)
            prob = holdout_model.predict_proba(X_test)[:, 1]
            metrics["validation"] = f"temporal holdout (last {int(self.test_fraction * 100)}% of runs)"
            metrics.update(info)
            metrics["miss_rate_train"] = round(float(y_train.mean()), 4)
            metrics.update(binary_report(y_test, prob, 0.5))
            importance = permutation_importance_dict(holdout_model, X_test, y_test, "neg_log_loss", self.random_state)
        else:
            metrics["validation"] = "none: no temporal holdout possible (too few runs or one class in the training period)"

        self.model.fit(d[features].astype(float), y_all)  # production fit on all labelled history
        self._features = features
        self._fitted = True
        if hasattr(self.model, "feature_importances_"):
            importance = dict(zip(features, self.model.feature_importances_))
        self._feature_importance = importance
        return metrics

    def predict_proba(self, df: pd.DataFrame) -> pd.Series:
        """Return P(cache_miss) per row."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict_proba().")
        return pd.Series(
            self.model.predict_proba(df[self._features].astype(float))[:, 1], index=df.index
        )

    @property
    def feature_importance(self) -> dict[str, float]:
        """Feature name -> importance (permutation importance on the temporal holdout, or native)."""
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": self.model, "backend": self._backend_name, "features": self._features}, path)

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._backend_name = data.get("backend", "sklearn-hist")
        self._features = data.get("features", list(FEATURE_COLUMNS))
        self._fitted = True
