"""
Model B — Job Failure Predictor.

Over structural, historical and temporal features.  Failures are the rare class in
real CI history (1–13% of job rows in the ingested repositories), so evaluation is a
**temporal holdout** reported as PR-AUC *against the test-period prevalence*, plus Brier
score vs the base-rate predictor.  The decision threshold is chosen on a validation tail
of the *training* period — never on the test set.

Three variants (the ``variant`` argument), all evaluated in ``models/rolling_eval.py``:

* ``"logistic"`` (default): median-imputed, standardised, L2-regularised logistic regression (C=0.3).  On
  the ingested repositories it is the only variant never significantly worse than the "prior failure rate"
  baseline (docs/MODEL_EVAL.md) - it can reproduce that baseline because the job's own failure history is
  one of its inputs.  Its probabilities are close to the observed rate on average but over-confident in the
  tail (few predictions above 0.85, and those that are fail far less often), so nothing should act on a
  probability alone.  The choice was made after looking at the evaluation folds; confirm it on fresh runs
  (``eval-models --after``).
* ``"tree"``: NaN-capable gradient-boosted trees (XGBoost when importable) - the original model.  Significantly
  worse than the baseline in dubbo and quarkus.
* ``"regularized_tree"``: heavily regularised boosted trees (min_samples_leaf=40, l2=5).  Best in poetry,
  fastapi and prometheus, clearly worse in dubbo and quarkus.

The label is ``label_failed``: 1 = failure/timed-out, 0 = success; cancelled and
skipped jobs carry no label and are excluded (counted in the metrics).

A production upgrade would replace this with a sequence model over each job's
ordered execution history.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from gha_optimizer.features.feature_extraction import FAILURE_V2_COLUMNS, FEATURE_COLUMNS
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


VARIANTS = ("logistic", "tree", "regularized_tree", "v2_logistic", "v2_hgb", "v2_blend", "v2_shrunk")


class BlendClassifier(BaseEstimator, ClassifierMixin):
    """Mean of the members' P(failure).  A plain, clone-able sklearn estimator (no stacking, nothing to tune)."""

    def __init__(self, members: list | None = None):
        self.members = members

    def fit(self, X, y):
        self.members_ = [clone(m).fit(X, y) for m in (self.members or [])]
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, X):
        p = np.mean([m.predict_proba(X)[:, 1] for m in self.members_], axis=0)
        return np.column_stack([1.0 - p, p])


class ShrunkClassifier(BaseEstimator, ClassifierMixin):
    """A model anchored to the job's own prior failure rate (EXPLORATORY, designed after seeing docs/IMPROVEMENT_RESULTS.md A).

    The first ``1 - holdout`` of the (chronologically ordered) training rows fit the base model; on the remaining rows a two-feature
    logistic regression learns how far to trust ``logit(prior failure rate)`` versus ``logit(base score)`` (strong L2, so an
    uninformative base score gets weight near 0 and the output falls back to the prior).  The base model is then refit on all rows.
    Rows whose job has no prior yet use the training failure rate.  Nothing here is tuned on the evaluation folds."""

    def __init__(self, base: Any = None, prior_col: str = "historical_failure_rate", holdout: float = 0.3, C: float = 0.5):
        self.base = base
        self.prior_col = prior_col
        self.holdout = holdout
        self.C = C

    @staticmethod
    def _logit(p: np.ndarray) -> np.ndarray:
        p = np.clip(p, 1e-4, 1 - 1e-4)
        return np.log(p / (1 - p))

    def _meta_features(self, X: pd.DataFrame, base_score: np.ndarray) -> np.ndarray:
        # a feature-set ablation may remove the prior column: the model then has no prior to anchor to and uses the base rate
        prior = X[self.prior_col].to_numpy(dtype=float) if self.prior_col in X else np.full(len(X), np.nan)
        prior = np.where(np.isnan(prior), self.global_rate_, prior)
        return np.column_stack([self._logit(prior), self._logit(base_score)])

    def fit(self, X, y):
        X = pd.DataFrame(X).reset_index(drop=True)
        y = np.asarray(y).astype(int)
        self.classes_ = np.array([0, 1])
        self.global_rate_ = float(y.mean())
        cut = int(len(X) * (1 - self.holdout))
        self.meta_ = None
        if cut > 0 and len(set(y[:cut])) == 2 and len(set(y[cut:])) == 2:
            try:
                first = clone(self.base).fit(X.iloc[:cut], y[:cut])
            except ValueError:                       # e.g. a feature with no observed value in the first part: no anchoring, base model alone
                first = None
            if first is not None:
                z = self._meta_features(X.iloc[cut:], first.predict_proba(X.iloc[cut:])[:, 1])
                self.meta_ = LogisticRegression(C=self.C, max_iter=1000).fit(z, y[cut:])
        self.base_ = clone(self.base).fit(X, y)
        return self

    def predict_proba(self, X):
        X = pd.DataFrame(X).reset_index(drop=True)
        s = self.base_.predict_proba(X)[:, 1]
        p = s if self.meta_ is None else self.meta_.predict_proba(self._meta_features(X, s))[:, 1]
        return np.column_stack([1.0 - p, p])


def _logistic() -> Any:
    return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                         LogisticRegression(C=0.3, max_iter=2000))


def _regularized_hgb(random_state: int) -> Any:
    return HistGradientBoostingClassifier(max_iter=100, max_depth=3, learning_rate=0.05, min_samples_leaf=40,
                                          l2_regularization=5.0, random_state=random_state)


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
        variant: str = "logistic",
    ):
        """``variant``: ``"logistic"`` (default) | ``"tree"`` | ``"regularized_tree"`` (see the module docstring);
        ``backend`` (``"auto"`` | ``"sklearn"`` | ``"xgboost"``) only applies to ``variant="tree"``."""
        from gha_optimizer.config import config

        if variant not in VARIANTS:
            raise ValueError(f"unknown failure-model variant {variant!r}; expected one of {VARIANTS}")
        self.variant = variant
        self.random_state = random_state
        self.test_fraction = test_fraction if test_fraction is not None else config.model.temporal_test_fraction

        self._feature_list = list(FAILURE_V2_COLUMNS if variant.startswith("v2_") else FEATURE_COLUMNS)
        self.model: Any
        if variant == "v2_logistic":
            self.model, self._backend_name = _logistic(), "sklearn-logistic-v2"
        elif variant == "v2_hgb":
            self.model, self._backend_name = _regularized_hgb(random_state), "sklearn-hist-regularized-v2"
        elif variant == "v2_shrunk":
            self.model = ShrunkClassifier(BlendClassifier([_logistic(), _regularized_hgb(random_state)]))
            self._backend_name = "shrunk-blend-v2"
        elif variant == "v2_blend":
            self.model = BlendClassifier([_logistic(), _regularized_hgb(random_state)])
            self._backend_name = "blend-v2"
        elif variant == "logistic":
            self.model = make_pipeline(
                SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                LogisticRegression(C=0.3, max_iter=2000),
            )
            self._backend_name = "sklearn-logistic"
        elif variant == "regularized_tree":
            self.model = HistGradientBoostingClassifier(
                max_iter=100, max_depth=3, learning_rate=0.05, min_samples_leaf=40,
                l2_regularization=5.0, random_state=random_state,
            )
            self._backend_name = "sklearn-hist-regularized"
        else:
            if backend == "xgboost" and not _HAS_XGBOOST:
                raise ImportError("XGBoost requested but not installed.")
            use_xgb = _HAS_XGBOOST if backend == "auto" else backend == "xgboost"
            if use_xgb:
                self.model = XGBClassifier(
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
            "variant": self.variant,
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
        features = usable_features(d, self._feature_list)
        metrics["features_dropped_all_nan"] = [c for c in self._feature_list if c not in features]

        train, test, info = temporal_split(d, self.test_fraction)
        y_train = train["label_failed"].astype(int)
        importance: dict[str, float] = {}
        if len(test) and y_train.nunique() == 2:
            # threshold from a validation tail of the training period
            inner_train, inner_val, _ = temporal_split(train, 0.25)
            threshold, how = 0.5, "default 0.5 (training period too short for a validation tail)"
            if len(inner_val) and inner_train["label_failed"].nunique() == 2:
                inner_feats = usable_features(inner_train, self._feature_list)
                m_val = clone(self.model).fit(inner_train[inner_feats].astype(float), inner_train["label_failed"].astype(int))
                p_val = m_val.predict_proba(inner_val[inner_feats].astype(float))[:, 1]
                threshold, how = pick_threshold(inner_val["label_failed"].astype(int), p_val)
            self._optimal_threshold = threshold

            train_feats = usable_features(train, self._feature_list)
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
             "features": self._features, "feature_importance": self._feature_importance,
             "variant": self.variant},
            path,
        )

    def load(self, path: str | Path) -> None:
        data = joblib.load(path)
        self.model = data["model"]
        self._backend_name = data.get("backend", "sklearn-hist")
        self.variant = data.get("variant", "tree")  # artifacts saved before the variant option are the original trees
        self._optimal_threshold = data.get("threshold", 0.5)
        self._feature_importance = data.get("feature_importance", {})
        self._features = data.get("features", list(FEATURE_COLUMNS))
        self._feature_list = list(FAILURE_V2_COLUMNS if self.variant.startswith("v2_") else FEATURE_COLUMNS)
        self._fitted = True
