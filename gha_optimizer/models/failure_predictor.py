"""
Model B — Job Failure Predictor.

Gradient-boosted trees (NaN-capable) over structural, historical and temporal
features.  Failures are the rare class in real CI history (1–13% of job rows in
the ingested repositories), so evaluation is a **temporal holdout** reported as
PR-AUC *against the test-period prevalence*, plus Brier score vs the base-rate
predictor.  The decision threshold is chosen on a validation tail of the
*training* period — never on the test set.

The label is ``label_failed``: 1 = failure/timed-out, 0 = success; cancelled and
skipped jobs carry no label and are excluded (counted in the metrics).

A production upgrade would replace this with a sequence model over each job's
ordered execution history.
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
    pick_threshold,
    temporal_split,
    usable_features,
)

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
        test_fraction: float | None = None,
    ):
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
        self._optimal_threshold: float = 0.5
        self._features: list[str] = list(FEATURE_COLUMNS)
        self._feature_importance: dict[str, float] = {}

    def fit(self, df: pd.DataFrame) -> dict:
        """Train on ``label_failed``; return temporal-holdout metrics."""
        d = chronological(df).dropna(subset=["label_failed"])
        y_all = d["label_failed"].astype(int)
        metrics: dict[str, Any] = {
            "backend": self._backend_name,
            "n_rows_total": int(len(df)),
            "n_rows_labelled": int(len(d)),
            "n_rows_excluded_null_label": int(len(df) - len(d)),
        }
        if len(d) == 0 or y_all.nunique() < 2:
            raise ValueError(
                f"failure model needs both outcomes in the labelled history "
                f"(labelled={len(d)}, positives={int(y_all.sum()) if len(d) else 0})"
            )
        metrics["failure_rate_all_labelled"] = round(float(y_all.mean()), 4)
        features = usable_features(d, FEATURE_COLUMNS)
        metrics["features_dropped_all_nan"] = [c for c in FEATURE_COLUMNS if c not in features]

        train, test, info = temporal_split(d, self.test_fraction)
        y_train = train["label_failed"].astype(int)
        importance: dict[str, float] = {}
        if len(test) and y_train.nunique() == 2:
            # threshold from a validation tail of the training period
            inner_train, inner_val, _ = temporal_split(train, 0.25)
            threshold, how = 0.5, "default 0.5 (training period too short for a validation tail)"
            if len(inner_val) and inner_train["label_failed"].nunique() == 2:
                inner_feats = usable_features(inner_train, FEATURE_COLUMNS)
                m_val = clone(self.model).fit(inner_train[inner_feats].astype(float), inner_train["label_failed"].astype(int))
                p_val = m_val.predict_proba(inner_val[inner_feats].astype(float))[:, 1]
                threshold, how = pick_threshold(inner_val["label_failed"].astype(int), p_val)
            self._optimal_threshold = threshold

            train_feats = usable_features(train, FEATURE_COLUMNS)
            holdout_model = clone(self.model).fit(train[train_feats].astype(float), y_train)
            X_test = test[train_feats].astype(float)
            y_test = test["label_failed"].astype(int)
            prob = holdout_model.predict_proba(X_test)[:, 1]
            metrics["validation"] = f"temporal holdout (last {int(self.test_fraction * 100)}% of runs)"
            metrics.update(info)
            metrics["failure_rate_train"] = round(float(y_train.mean()), 4)
            metrics["threshold_selection"] = how
            metrics.update(binary_report(y_test, prob, threshold))
            importance = permutation_importance_dict(holdout_model, X_test, y_test, "neg_log_loss", self.random_state)
        else:
            metrics["validation"] = "none: no temporal holdout possible (too few runs or one outcome in the training period)"

        self.model.fit(d[features].astype(float), y_all)  # production fit on all labelled history
        self._features = features
        self._fitted = True
        if hasattr(self.model, "feature_importances_"):
            importance = dict(zip(features, self.model.feature_importances_))
        self._feature_importance = importance
        return metrics

    def predict_proba(self, df: pd.DataFrame) -> pd.Series:
        """Return P(failure) per row."""
        if not self._fitted:
            raise RuntimeError("Call fit() before predict_proba().")
        return pd.Series(
            self.model.predict_proba(df[self._features].astype(float))[:, 1], index=df.index
        )

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Binary failure predictions at the validation-selected threshold."""
        probs = self.predict_proba(df)
        return (probs >= self._optimal_threshold).astype(int)

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict(sorted(self._feature_importance.items(), key=lambda x: -x[1]))

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"model": self.model, "backend": self._backend_name, "threshold": self._optimal_threshold,
             "features": self._features},
            path,
        )

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._backend_name = data.get("backend", "sklearn-hist")
        self._optimal_threshold = data.get("threshold", 0.5)
        self._features = data.get("features", list(FEATURE_COLUMNS))
        self._fitted = True
