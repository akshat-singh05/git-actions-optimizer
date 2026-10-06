"""
Evaluation helpers shared by the three tabular models.

Principles (see docs/DATASET_REPORT.md):

* **Temporal split, never random.**  The frame is ordered by time; the last
  ``test_fraction`` of *runs* (all rows of a run stay together) is held out.
* **Report the base rate and a baseline next to every metric.**  With a rare
  positive class (CI failures are 1–13% of rows in the ingested repos) accuracy
  and ROC-AUC flatter the model; PR-AUC is reported against the prevalence
  (the PR-AUC of a no-skill classifier), and regression is compared with the
  "predict the job's prior mean" baseline.
* A metric that is undefined (one class in the test set, empty test set) is
  reported as ``None`` with a reason — never filled in.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    f1_score,
    mean_absolute_error,
    median_absolute_error,
    precision_score,
    r2_score,
    recall_score,
    roc_auc_score,
)


def usable_features(df: pd.DataFrame, columns: list[str]) -> list[str]:
    """Feature columns with at least one observed value.

    A column that is entirely NaN (e.g. graph features that were never computed for this
    history) carries no information and cannot be binned by the tree learners; it is
    dropped for that fit and reported, not filled with a constant."""
    return [c for c in columns if c in df.columns and df[c].notna().any()]


def chronological(df: pd.DataFrame) -> pd.DataFrame:
    """Stable chronological order (timestamp, run-id) when those columns exist."""
    if "timestamp" not in df.columns:
        return df
    key = df["timestamp"].astype(str)
    if "run_id" in df.columns:
        rid = df["run_id"].astype(str)
        order = pd.DataFrame({"t": key, "l": rid.str.len(), "r": rid}).sort_values(["t", "l", "r"], kind="stable").index
    else:
        order = key.sort_values(kind="stable").index
    return df.loc[order]


def temporal_split(
    df: pd.DataFrame, test_fraction: float = 0.2, run_col: str = "run_id"
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Train on earlier runs, test on the later ``test_fraction`` of runs."""
    df = chronological(df)
    runs = list(dict.fromkeys(df[run_col])) if run_col in df.columns else list(range(len(df)))
    info: dict[str, Any] = {"n_runs": len(runs)}
    if len(runs) < 2:
        return df, df.iloc[0:0], {**info, "boundary": None}
    n_test = min(max(1, int(round(len(runs) * test_fraction))), len(runs) - 1)
    test_runs = set(runs[-n_test:])
    mask = df[run_col].isin(test_runs) if run_col in df.columns else pd.Series(
        [i >= len(df) - n_test for i in range(len(df))], index=df.index
    )
    train, test = df[~mask], df[mask]
    boundary = str(test["timestamp"].iloc[0]) if "timestamp" in test.columns and len(test) else None
    return train, test, {**info, "boundary": boundary, "train_runs": len(runs) - n_test, "test_runs": n_test}


def pick_threshold(y_val: pd.Series, p_val: np.ndarray, min_positives: int = 5) -> tuple[float, str]:
    """F1-maximising threshold chosen on a *validation* slice (never on the test set).

    Falls back to 0.5 — and says so — when the validation slice has too few positives."""
    y = np.asarray(y_val)
    if y.sum() < min_positives or y.sum() == len(y):
        return 0.5, f"default 0.5 (validation slice has {int(y.sum())} positives < {min_positives})"
    best_t, best_f = 0.5, -1.0
    for t in np.unique(np.round(p_val, 4)):
        f = f1_score(y, (p_val >= t).astype(int), zero_division=0)
        if f > best_f:
            best_t, best_f = float(t), f
    return best_t, "F1-max on the validation tail of the training period"


def binary_report(y_true: pd.Series, prob: np.ndarray, threshold: float = 0.5) -> dict[str, Any]:
    y = np.asarray(y_true).astype(int)
    out: dict[str, Any] = {"n_test": int(len(y)), "positives_test": int(y.sum()), "threshold": round(float(threshold), 4)}
    if len(y) == 0:
        return {**out, "note": "empty test set"}
    base = float(y.mean())
    out["base_rate_test"] = round(base, 4)
    pred = (prob >= threshold).astype(int)
    out["precision"] = round(float(precision_score(y, pred, zero_division=0)), 4)
    out["recall"] = round(float(recall_score(y, pred, zero_division=0)), 4)
    out["f1"] = round(float(f1_score(y, pred, zero_division=0)), 4)
    out["brier"] = round(float(brier_score_loss(y, prob)), 4)
    out["brier_baseline"] = round(float(base * (1 - base)), 4)  # predicting the test base rate
    if y.sum() > 0:
        out["pr_auc"] = round(float(average_precision_score(y, prob)), 4)
        out["pr_auc_baseline"] = round(base, 4)               # no-skill PR-AUC == prevalence
    else:
        out["pr_auc"] = None
        out["note"] = "no positives in the test period: PR-AUC undefined"
    if 0 < y.sum() < len(y):
        out["roc_auc"] = round(float(roc_auc_score(y, prob)), 4)
    else:
        out["roc_auc"] = None
    return out


def regression_report(y_true: pd.Series, pred: np.ndarray, baseline: np.ndarray | None) -> dict[str, Any]:
    y = np.asarray(y_true, dtype=float)
    out: dict[str, Any] = {"n_test": int(len(y))}
    if len(y) == 0:
        return {**out, "note": "empty test set"}
    out["mae_seconds"] = round(float(mean_absolute_error(y, pred)), 2)
    out["median_ae_seconds"] = round(float(median_absolute_error(y, pred)), 2)
    out["r2"] = round(float(r2_score(y, pred)), 4) if len(y) > 1 else None
    if baseline is not None:
        ok = ~np.isnan(baseline)
        if ok.sum() > 0:
            out["baseline_mae_seconds"] = round(float(mean_absolute_error(y[ok], baseline[ok])), 2)
            out["baseline"] = "predict the job's prior (strictly earlier) decayed mean duration"
            out["baseline_n"] = int(ok.sum())
    return out


def permutation_importance_dict(model: Any, X: pd.DataFrame, y: Any, scoring: str, seed: int = 13) -> dict[str, float]:
    """Permutation importance on the temporal test slice (works for every estimator)."""
    from sklearn.inspection import permutation_importance

    if len(X) < 5:
        return {}
    try:
        res = permutation_importance(model, X, y, scoring=scoring, n_repeats=5, random_state=seed)
    except Exception:  # undefined score (e.g. a single class) -> no importance rather than a made-up one
        return {}
    return {c: float(v) for c, v in zip(X.columns, res.importances_mean)}
