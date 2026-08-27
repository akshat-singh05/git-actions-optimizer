"""
Model B — Job Failure Predictor.

Upgraded from the baseline logistic-regression to Gradient Boosted Trees,
with cross-validation, ROC-AUC, sequence-aware features (recent streaks),
and threshold calibration.

A production upgrade would further replace this with a sequence model
(LSTM / small Transformer) consuming each job's ordered execution history
to capture temporal patterns that a tabular model can only approximate.
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
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)

from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS

try:
    from xgboost import XGBClassifier
    _HAS_XGBOOST = True
except ImportError:
    _HAS_XGBOOST = False


class FailurePredictor:
    """Predict P(failure) per job using tabular features."""

    def __init__(
        self,
        backend: str = "auto",
        n_estimators: int = 150,
        max_depth: int = 3,
        learning_rate: float = 0.08,
        random_state: int = 13,
    ):
        self.random_state = random_state

        use_xgb = (backend == "auto" and _HAS_XGBOOST) or backend == "xgboost"
        if backend == "xgboost" and not _HAS_XGBOOST:
            raise ImportError("XGBoost requested but not installed.")

        if use_xgb:
            self.model = XGBClassifier(
                n_estimators=n_estimators,
                max_depth=max_depth,
                learning_rate=learning_rate,
                random_state=random_state,
                scale_pos_weight=3.0,  # address class imbalance (failures are rarer)
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
        self._optimal_threshold: float = 0.5
        self._feature_importance: dict[str, float] = {}

    def fit(self, df: pd.DataFrame, cv: bool = True) -> dict:
        """
        Train the model and compute validation metrics.

        Returns dict with precision, recall, f1, roc_auc, and the
        optimal probability threshold.
        """
        X = df[FEATURE_COLUMNS].copy()
        y = df["label_failed"]

        if cv and len(y) >= 20 and y.nunique() > 1:
            metrics = self._fit_with_cv(X, y)
        else:
            metrics = self._fit_holdout(X, y)

        if hasattr(self.model, "feature_importances_"):
            self._feature_importance = dict(
                zip(FEATURE_COLUMNS, self.model.feature_importances_)
            )

        return metrics

    def _fit_with_cv(self, X: pd.DataFrame, y: pd.Series) -> dict:
        skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=self.random_state)

        probs = cross_val_predict(
            self.model, X, y, cv=skf, method="predict_proba"
        )[:, 1]

        # Find optimal threshold via F1 maximization on CV predictions
        self._optimal_threshold = self._find_optimal_threshold(y, probs)
        preds = (probs >= self._optimal_threshold).astype(int)

        # Refit on full data
        self.model.fit(X, y)
        self._fitted = True

        metrics = {
            "backend": self._backend_name,
            "validation": "5-fold CV",
            "precision": round(precision_score(y, preds, zero_division=0), 4),
            "recall": round(recall_score(y, preds, zero_division=0), 4),
            "f1": round(f1_score(y, preds, zero_division=0), 4),
            "optimal_threshold": round(self._optimal_threshold, 4),
        }
        if y.nunique() > 1:
            metrics["roc_auc"] = round(roc_auc_score(y, probs), 4)
        return metrics

    def _fit_holdout(self, X: pd.DataFrame, y: pd.Series) -> dict:
        stratify = y if y.nunique() > 1 else None
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=self.random_state, stratify=stratify
        )
        self.model.fit(X_train, y_train)
        self._fitted = True

        probs = self.model.predict_proba(X_test)[:, 1]
        self._optimal_threshold = self._find_optimal_threshold(y_test, probs)
        preds = (probs >= self._optimal_threshold).astype(int)

        metrics = {
            "backend": self._backend_name,
            "validation": "holdout 75/25",
            "precision": round(precision_score(y_test, preds, zero_division=0), 4),
            "recall": round(recall_score(y_test, preds, zero_division=0), 4),
            "f1": round(f1_score(y_test, preds, zero_division=0), 4),
            "optimal_threshold": round(self._optimal_threshold, 4),
        }
        if y_test.nunique() > 1:
            metrics["roc_auc"] = round(roc_auc_score(y_test, probs), 4)
        return metrics

    @staticmethod
    def _find_optimal_threshold(y_true: pd.Series, y_proba: np.ndarray) -> float:
        """Pick the probability threshold maximizing F1."""
        if y_true.nunique() <= 1:
            return 0.5
        precision, recall, thresholds = precision_recall_curve(y_true, y_proba)
        f1_scores = 2 * (precision * recall) / (precision + recall + 1e-10)
        best_idx = f1_scores.argmax()
        if best_idx < len(thresholds):
            return float(thresholds[best_idx])
        return 0.5

    def predict_proba(self, df: pd.DataFrame) -> pd.Series:
        """Return P(failure) per row."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict_proba().")
        return pd.Series(
            self.model.predict_proba(df[FEATURE_COLUMNS])[:, 1], index=df.index
        )

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Return binary failure predictions using the optimal threshold."""
        probs = self.predict_proba(df)
        return (probs >= self._optimal_threshold).astype(int)

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "backend": self._backend_name,
                "threshold": self._optimal_threshold,
            },
            path,
        )

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._backend_name = data.get("backend", "sklearn")
        self._optimal_threshold = data.get("threshold", 0.5)
        self._fitted = True
