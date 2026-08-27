"""
Model A — Cache-Miss Predictor (tabular baseline).

Predicts P(cache_miss) for a given job on the current diff.  Uses Gradient
Boosted Trees with cross-validation, feature-importance extraction, and
optional XGBoost backend.

The GNN-based upgrade (Model D — the primary patent-differentiating
component) operates directly on the ``networkx.DiGraph`` from
``gha_optimizer.graph.dependency_graph`` and lives in
``gnn_cache_predictor.py``; it does not require changing any other layer's
interface so the two can be evaluated independently (ablation study).
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_predict, train_test_split
from sklearn.metrics import (
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS

# Try XGBoost as a drop-in upgrade
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
    ):
        """
        Parameters
        ----------
        backend : "auto" | "sklearn" | "xgboost"
            ``"auto"`` uses XGBoost if available, else sklearn GBT.
        """
        self.random_state = random_state

        if backend == "auto":
            use_xgb = _HAS_XGBOOST
        elif backend == "xgboost":
            if not _HAS_XGBOOST:
                raise ImportError("XGBoost requested but not installed.")
            use_xgb = True
        else:
            use_xgb = False

        if use_xgb:
            self.model = XGBClassifier(
                n_estimators=n_estimators,
                max_depth=max_depth,
                learning_rate=learning_rate,
                random_state=random_state,
                eval_metric="logloss",
                use_label_encoder=False,
            )
            self._backend_name = "xgboost"
        else:
            self.model = GradientBoostingClassifier(
                n_estimators=n_estimators,
                max_depth=max_depth,
                learning_rate=learning_rate,
                random_state=random_state,
            )
            self._backend_name = "sklearn"

        self._fitted = False
        self._feature_importance: dict[str, float] = {}

    def fit(self, df: pd.DataFrame, cv: bool = True) -> dict:
        """
        Train the model.

        Parameters
        ----------
        df : Training dataframe with ``FEATURE_COLUMNS`` + ``label_cache_hit``.
        cv : If True, report cross-validated metrics instead of a single
             holdout split (more robust for small datasets).

        Returns
        -------
        dict with precision, recall, f1, roc_auc (if applicable).
        """
        X = df[FEATURE_COLUMNS].copy()
        y = 1 - df["label_cache_hit"]  # predict MISS, not hit

        if cv and len(y) >= 20 and y.nunique() > 1:
            metrics = self._fit_with_cv(X, y)
        else:
            metrics = self._fit_holdout(X, y)

        # Extract feature importance
        if hasattr(self.model, "feature_importances_"):
            self._feature_importance = dict(
                zip(FEATURE_COLUMNS, self.model.feature_importances_)
            )

        return metrics

    def _fit_with_cv(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """Stratified 5-fold cross-validation."""
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=self.random_state)

        probs = cross_val_predict(
            self.model, X, y, cv=skf, method="predict_proba"
        )[:, 1]
        preds = (probs >= 0.5).astype(int)

        # Refit on full data for production use
        self.model.fit(X, y)
        self._fitted = True

        metrics = {
            "backend": self._backend_name,
            "validation": "5-fold CV",
            "precision": round(precision_score(y, preds, zero_division=0), 4),
            "recall": round(recall_score(y, preds, zero_division=0), 4),
            "f1": round(f1_score(y, preds, zero_division=0), 4),
        }
        if y.nunique() > 1:
            metrics["roc_auc"] = round(roc_auc_score(y, probs), 4)
        return metrics

    def _fit_holdout(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """Single train/test split fallback."""
        stratify = y if y.nunique() > 1 else None
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=self.random_state, stratify=stratify
        )
        self.model.fit(X_train, y_train)
        self._fitted = True

        preds = self.model.predict(X_test)
        probs = self.model.predict_proba(X_test)[:, 1]

        metrics = {
            "backend": self._backend_name,
            "validation": "holdout 75/25",
            "precision": round(precision_score(y_test, preds, zero_division=0), 4),
            "recall": round(recall_score(y_test, preds, zero_division=0), 4),
            "f1": round(f1_score(y_test, preds, zero_division=0), 4),
        }
        if y_test.nunique() > 1:
            metrics["roc_auc"] = round(roc_auc_score(y_test, probs), 4)
        return metrics

    def predict_proba(self, df: pd.DataFrame) -> pd.Series:
        """Return P(cache_miss) per row."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict_proba().")
        return pd.Series(
            self.model.predict_proba(df[FEATURE_COLUMNS])[:, 1], index=df.index
        )

    @property
    def feature_importance(self) -> dict[str, float]:
        """Feature name → importance (higher = more predictive)."""
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        """Persist the trained model."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"model": self.model, "backend": self._backend_name},
            path,
        )

    def load(self, path: str | Path) -> None:
        """Load a previously saved model."""
        data = joblib.load(path)
        self.model = data["model"]
        self._backend_name = data.get("backend", "sklearn")
        self._fitted = True
