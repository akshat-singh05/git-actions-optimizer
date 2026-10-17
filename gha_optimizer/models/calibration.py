"""
Probability calibration for the failure model, and its measurement.

A classifier's score is only a *probability* if "0.3" means about 30% of such jobs fail.  Logistic regression with
regularisation (the default failure model) ranks well enough but is not guaranteed to be calibrated, so a displayed
"P(failure) = 0.93" says little on its own.  Two pieces live here:

* :class:`CalibratedClassifier` - wraps any classifier; the last 25% of the (chronologically ordered) training rows are
  held back, the base model is fitted on the earlier 75%, a Platt sigmoid ``p' = sigmoid(a * logit(p) + b)`` is fitted on
  the held-back tail, and the base model is then refitted on every row.  The sigmoid is increasing in ``p`` (``a > 0`` is
  enforced), so **the ranking - and therefore ROC-AUC and PR-AUC - is unchanged**; only the probabilities move.  With
  too little data (< 40 tail rows or < 5 failures in the tail) the wrapper does not calibrate and says so
  (``calibrated_`` is False).
* :func:`evaluate_calibration_repo` - a rolling-origin measurement (same folds as ``eval-models``) of Brier score,
  log-loss and expected calibration error for the raw model, the calibrated model and the baseline (the job's prior
  failure rate), with a paired cluster bootstrap over runs for the Brier difference.

Rows must be passed in time order (``models.evaluation.chronological``).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss

from gha_optimizer.models.evaluation import chronological, usable_features

TAIL_FRACTION = 0.25
MIN_TAIL_ROWS = 40
MIN_TAIL_POSITIVES = 5
N_BINS = 10
EPS = 1e-6


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


class CalibratedClassifier(BaseEstimator, ClassifierMixin):
    """``base`` + a Platt sigmoid fitted on the chronological tail of the training rows (see the module docstring)."""

    def __init__(self, base: Any = None, tail_fraction: float = TAIL_FRACTION):
        self.base = base
        self.tail_fraction = tail_fraction

    def fit(self, X, y):
        X = pd.DataFrame(X).reset_index(drop=True)
        y = np.asarray(y).astype(int)
        self.classes_ = np.array([0, 1])
        self.calibrated_ = False
        self.slope_, self.intercept_ = 1.0, 0.0
        self.calibration_note_ = "not calibrated: too little data in the validation tail"
        n_tail = int(round(len(X) * self.tail_fraction))
        if n_tail >= MIN_TAIL_ROWS and len(X) - n_tail >= MIN_TAIL_ROWS:
            head, tail = slice(0, len(X) - n_tail), slice(len(X) - n_tail, len(X))
            y_head, y_tail = y[head], y[tail]
            if len(set(y_head)) == 2 and y_tail.sum() >= MIN_TAIL_POSITIVES and (1 - y_tail).sum() >= MIN_TAIL_POSITIVES:
                probe = clone(self.base).fit(X.iloc[head], y_head)
                z = _logit(probe.predict_proba(X.iloc[tail])[:, 1]).reshape(-1, 1)
                sig = LogisticRegression(C=1e6, max_iter=1000).fit(z, y_tail)   # effectively unpenalised Platt scaling
                a, b = float(sig.coef_[0, 0]), float(sig.intercept_[0])
                if a > 0:
                    self.slope_, self.intercept_, self.calibrated_ = a, b, True
                    self.calibration_note_ = (f"Platt sigmoid on the last {n_tail} rows: p' = sigmoid({a:.3f} * logit(p) "
                                              f"{'+' if b >= 0 else '-'} {abs(b):.3f})")
                else:
                    self.calibration_note_ = "not calibrated: the validation tail gave a non-increasing sigmoid"
        self.base_ = clone(self.base).fit(X, y)
        return self

    def predict_proba(self, X):
        p = self.base_.predict_proba(pd.DataFrame(X))[:, 1]
        if self.calibrated_:
            p = 1.0 / (1.0 + np.exp(-(self.slope_ * _logit(p) + self.intercept_)))
        return np.column_stack([1.0 - p, p])


class OutOfSampleCalibrator:
    """Map a raw failure score to the failure rate actually observed for jobs that scored like that, out of sample.

    Fitted on the pooled predictions that rolling-origin folds made on rows their model had not seen
    (:func:`out_of_sample_predictions`), with an increasing (isotonic) map, so it is monotone, cannot invent
    confidence the data does not show and is flat above the highest score ever seen out of sample.  It is a *reporting*
    layer: the scheduler keeps using the raw score.
    """

    def __init__(self, scores: np.ndarray, outcomes: np.ndarray):
        self.n = int(len(scores))
        self.positives = int(np.sum(outcomes))
        self.max_score = float(np.max(scores)) if self.n else 0.0
        self._scores, self._outcomes = np.asarray(scores, dtype=float), np.asarray(outcomes, dtype=float)
        self._iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip").fit(scores, outcomes)

    @property
    def usable(self) -> bool:
        return self.n >= 100 and self.positives >= MIN_TAIL_POSITIVES

    def transform(self, p: np.ndarray) -> np.ndarray:
        return self._iso.predict(np.asarray(p, dtype=float))

    def outside_range(self, p: float) -> bool:
        """True if ``p`` is higher than any score the model produced on unseen rows (its probability is extrapolation)."""
        return p > self.max_score

    def describe(self) -> dict[str, Any]:
        return {"out_of_sample_rows": self.n, "out_of_sample_failures": self.positives, "highest_out_of_sample_score": round(self.max_score, 4)}

    def lookup(self, p: float, n_bins: int = N_BINS) -> dict[str, Any]:
        """What happened to unseen rows that scored like ``p``: their count, failures and observed failure rate.

        Descriptive, not a model: the out-of-sample rows are cut into ``n_bins`` equal-mass bins by score and ``p`` is
        placed in its bin (a score above the highest one ever seen is reported as outside the range, with the top bin's
        figures for reference).  The dashboard shows this next to the raw score and the job's own base rate.
        """
        if not self.n:
            return {"available": False}
        order = np.argsort(self._scores, kind="mergesort")
        scores, outcomes = self._scores[order], self._outcomes[order]
        bins = [idx for idx in np.array_split(np.arange(self.n), n_bins) if len(idx)]
        k = next((i for i, idx in enumerate(bins) if p <= scores[idx[-1]]), len(bins) - 1)
        idx = bins[k]
        return {"available": True, "score_low": round(float(scores[idx[0]]), 4), "score_high": round(float(scores[idx[-1]]), 4),
                "rows": int(len(idx)), "failures": int(outcomes[idx].sum()), "observed_rate": round(float(outcomes[idx].mean()), 4),
                "outside_range": bool(p > self.max_score), "highest_score_seen": round(self.max_score, 4)}


def out_of_sample_predictions(df: pd.DataFrame, factory: Any = None, n_folds: int = 5, initial_fraction: float = 0.5,
                              min_train_runs: int = 20) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(scores, outcomes, baseline)`` of rolling-origin folds on ``df``: rows the fold's model had not been trained on.

    ``baseline`` is the job's prior failure rate for the same rows (the naive competitor of the model)."""
    from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS
    from gha_optimizer.models.failure_predictor import FailurePredictor
    from gha_optimizer.models.rolling_eval import rolling_origin_folds

    factory = factory or (lambda: FailurePredictor(variant="logistic").model)
    d = chronological(df)
    lab = d[d["label_failed"].notna()].copy()
    lab["_y"] = lab["label_failed"].astype(int)
    lab["_run"] = lab["run_id"].astype(str)
    runs = list(dict.fromkeys(lab["_run"]))
    ps, ys, bs = [], [], []
    for train_runs, test_runs in rolling_origin_folds(runs, n_folds, initial_fraction, min_train_runs):
        train, test = lab[lab["_run"].isin(train_runs)], lab[lab["_run"].isin(test_runs)]
        if len(test) == 0 or train["_y"].nunique() < 2:
            continue
        feats = usable_features(train, FEATURE_COLUMNS)
        est = clone(factory()).fit(train[feats].astype(float), train["_y"])
        ps.append(est.predict_proba(test[feats].astype(float))[:, 1])
        ys.append(test["_y"].to_numpy(dtype=float))
        bs.append(test["historical_failure_rate"].astype(float).fillna(float(train["_y"].mean())).to_numpy())
    if not ps:
        return np.array([]), np.array([]), np.array([])
    return np.concatenate(ps), np.concatenate(ys), np.concatenate(bs)


# ---------------------------------------------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------------------------------------------

def expected_calibration_error(y: np.ndarray, p: np.ndarray, n_bins: int = N_BINS) -> float:
    """Mean |observed rate - mean predicted| over equal-mass bins, weighted by bin size."""
    order = np.argsort(p, kind="mergesort")
    y, p = np.asarray(y, dtype=float)[order], np.asarray(p, dtype=float)[order]
    total = 0.0
    for idx in np.array_split(np.arange(len(p)), n_bins):
        if len(idx):
            total += len(idx) * abs(float(y[idx].mean()) - float(p[idx].mean()))
    return total / len(p)


def reliability_table(y: np.ndarray, p: np.ndarray, n_bins: int = N_BINS) -> list[dict[str, float]]:
    order = np.argsort(p, kind="mergesort")
    y, p = np.asarray(y, dtype=float)[order], np.asarray(p, dtype=float)[order]
    rows = []
    for idx in np.array_split(np.arange(len(p)), n_bins):
        if len(idx):
            rows.append({"n": int(len(idx)), "mean_predicted": round(float(p[idx].mean()), 4),
                         "observed_rate": round(float(y[idx].mean()), 4)})
    return rows


def _scores(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    p = np.clip(p, EPS, 1 - EPS)
    return {"brier": round(float(brier_score_loss(y, p)), 5), "log_loss": round(float(log_loss(y, p, labels=[0, 1])), 5),
            "ece": round(float(expected_calibration_error(y, p)), 5), "mean_predicted": round(float(p.mean()), 4)}


def evaluate_calibration_repo(df: pd.DataFrame, n_folds: int = 5, initial_fraction: float = 0.5, min_train_runs: int = 20,
                              n_boot: int = 1000, estimator_factory: Any = None) -> dict[str, Any]:
    """Rolling-origin calibration of the default failure model on one repository's dataset frame."""
    from gha_optimizer.features.feature_extraction import FEATURE_COLUMNS
    from gha_optimizer.models.failure_predictor import FailurePredictor
    from gha_optimizer.models.rolling_eval import SEED, rolling_origin_folds

    d = chronological(df)
    lab = d[d["label_failed"].notna()].copy()
    lab["_y"] = lab["label_failed"].astype(int)
    lab["_run"] = lab["run_id"].astype(str)
    runs = list(dict.fromkeys(lab["_run"]))
    out: dict[str, Any] = {"n_labelled_rows": int(len(lab)), "n_runs": len(runs)}
    factory = estimator_factory or (lambda: FailurePredictor(variant="logistic").model)
    ys, rs, p_raw, p_cal, p_base, p_iso, notes = [], [], [], [], [], [], []
    seen_p: list[np.ndarray] = []      # raw out-of-sample scores of the EARLIER folds (the isotonic arm learns only from these)
    seen_y: list[np.ndarray] = []
    for train_runs, test_runs in rolling_origin_folds(runs, n_folds, initial_fraction, min_train_runs):
        train, test = lab[lab["_run"].isin(train_runs)], lab[lab["_run"].isin(test_runs)]
        if len(test) == 0 or train["_y"].nunique() < 2:
            continue
        feats = usable_features(train, FEATURE_COLUMNS)
        raw = clone(factory()).fit(train[feats].astype(float), train["_y"])
        cal = CalibratedClassifier(factory()).fit(train[feats].astype(float), train["_y"])
        notes.append(cal.calibration_note_)
        p_raw.append(raw.predict_proba(test[feats].astype(float))[:, 1])
        if seen_p and sum(float(v.sum()) for v in seen_y) >= MIN_TAIL_POSITIVES:
            p_iso.append(OutOfSampleCalibrator(np.concatenate(seen_p), np.concatenate(seen_y)).transform(p_raw[-1]))
        else:
            p_iso.append(np.full(len(test), np.nan))     # no earlier out-of-sample evidence: not scored for this arm
        seen_p.append(p_raw[-1])
        seen_y.append(test["_y"].to_numpy(dtype=float))
        p_cal.append(cal.predict_proba(test[feats].astype(float))[:, 1])
        p_base.append(test["historical_failure_rate"].astype(float).fillna(float(train["_y"].mean())).to_numpy())
        ys.append(test["_y"].to_numpy(dtype=float))
        rs.append(test["_run"].to_numpy())
    if not ys:
        out["note"] = "no usable fold"
        return out
    y, run_arr = np.concatenate(ys), np.concatenate(rs)
    P = {"raw": np.concatenate(p_raw), "platt_tail": np.concatenate(p_cal), "isotonic_oos": np.concatenate(p_iso),
         "baseline": np.concatenate(p_base)}
    keep = ~np.isnan(P["isotonic_oos"])      # rows every arm can be scored on (the first fold has no earlier out-of-sample scores)
    y, run_arr = y[keep], run_arr[keep]
    P = {k: v[keep] for k, v in P.items()}
    if not len(y) or not 0 < y.sum() < len(y):
        out["note"] = "too few rows with earlier out-of-sample evidence"
        return out
    out.update({"n_test_rows": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4),
                "folds_used": len(ys), "folds_platt_fitted": sum(n.startswith("Platt") for n in notes),
                "note_rows": "rows of the first fold are excluded so that every arm is scored on the same rows"})
    out["scores"] = {k: _scores(y, p) for k, p in P.items()}
    out["reliability"] = {k: reliability_table(y, P[k]) for k in ("raw", "isotonic_oos")}
    out["highest_raw_score_out_of_sample"] = round(float(P["raw"].max()), 4)
    out["scored_above_half"] = int((P["raw"] > 0.5).sum())
    rng = np.random.default_rng(SEED)
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(run_arr):
        groups.setdefault(r, []).append(i)
    gl = [np.array(v) for v in groups.values()]
    diffs: dict[str, list[float]] = {"platt_tail - raw": [], "isotonic_oos - raw": [], "isotonic_oos - baseline": [], "raw - baseline": []}
    for _ in range(n_boot):
        ix = np.concatenate([gl[i] for i in rng.integers(0, len(gl), len(gl))])
        yb = y[ix]
        if not 0 < yb.sum() < len(yb):
            continue
        b = {k: brier_score_loss(yb, np.clip(p[ix], EPS, 1 - EPS)) for k, p in P.items()}
        diffs["platt_tail - raw"].append(b["platt_tail"] - b["raw"])
        diffs["isotonic_oos - raw"].append(b["isotonic_oos"] - b["raw"])
        diffs["isotonic_oos - baseline"].append(b["isotonic_oos"] - b["baseline"])
        diffs["raw - baseline"].append(b["raw"] - b["baseline"])
    out["brier_difference"] = {k: ({"delta": round(float(np.mean(v)), 5),
                                    "ci95": [round(float(np.percentile(v, 2.5)), 5), round(float(np.percentile(v, 97.5)), 5)]}
                                   if len(v) >= 50 else {"note": "too few usable resamples"}) for k, v in diffs.items()}
    return out


def render_calibration(results: dict[str, dict[str, Any]], n_boot: int) -> str:
    lines = ["# Calibration of the failure probabilities\n",
             "Generated by `gha-optimizer eval-calibration`. Method: `gha_optimizer/models/calibration.py`. Rolling-origin folds as in "
             f"`eval-models`; pooled out-of-sample predictions; paired cluster bootstrap over runs ({n_boot} resamples). "
             "Arms: **raw** = the default failure model's score; **platt_tail** = a sigmoid fitted on the newest 25% of the training "
             "rows; **isotonic_oos** = an increasing map from score to the failure rate observed on the *earlier folds'* out-of-sample "
             "predictions (it learns only from the past); **baseline** = the job's prior failure rate. All arms are scored on the "
             "same rows (the first fold has no earlier out-of-sample evidence and is left out).\n",
             "**Brier score, log-loss and ECE (expected calibration error, 10 equal-mass bins): lower is better; ECE 0 means a "
             "predicted 30% fails 30% of the time.** The calibrators are increasing maps, so ranking metrics are not worse than "
             "the raw model's (isotonic can create ties).\n",
             "| repo | test rows | positives (base rate) | Brier raw / platt_tail / isotonic_oos / baseline | "
             "ECE raw / platt_tail / isotonic_oos / baseline | Brier: platt_tail - raw | Brier: isotonic_oos - raw | "
             "Brier: isotonic_oos - baseline | Brier: raw - baseline |", "|---|---|---|---|---|---|---|---|---|"]

    def ci(d: dict[str, Any]) -> str:
        return f"{d['delta']:+.5f} [{d['ci95'][0]:+.5f}, {d['ci95'][1]:+.5f}]" if "ci95" in d else "n/a"

    order = ("raw", "platt_tail", "isotonic_oos", "baseline")
    for repo, r in results.items():
        if "scores" not in r:
            lines.append(f"| {repo} | - | - | - | - | {r.get('note', 'no result')} | - | - | - |")
            continue
        s = r["scores"]
        b = r["brier_difference"]
        lines.append(f"| {repo} | {r['n_test_rows']} | {r['positives']} ({100 * r['base_rate']:.1f}%) | "
                     + " / ".join(f"{s[k]['brier']:.4f}" for k in order) + " | "
                     + " / ".join(f"{s[k]['ece']:.4f}" for k in order) + " | "
                     f"{ci(b['platt_tail - raw'])} | {ci(b['isotonic_oos - raw'])} | {ci(b['isotonic_oos - baseline'])} | {ci(b['raw - baseline'])} |")
    lines.append("\n## How far can a displayed probability be trusted?\n")
    lines.append("| repo | highest raw score on unseen rows | rows scored above 0.5 on unseen rows |")
    lines.append("|---|---|---|")
    for repo, r in results.items():
        if "highest_raw_score_out_of_sample" in r:
            lines.append(f"| {repo} | {r['highest_raw_score_out_of_sample']:.3f} | {r.get('scored_above_half', 'n/a')} |")
    lines.append("\nA score above the highest one the model ever produced on unseen rows is an extrapolation: no out-of-sample "
                 "evidence says how often such a job fails. The dashboard flags such scores and shows the observed failure rate of unseen rows that scored alike next to "
                 "the raw score, the job's own base rate and the baseline.\n")
    lines.append("## Reliability (predicted vs observed failure rate, 10 equal-mass bins of the pooled out-of-sample rows)\n")
    for repo, r in results.items():
        if "reliability" not in r:
            continue
        lines.append(f"**{repo}**\n")
        lines.append("| bin | rows | raw: mean predicted | raw: observed | isotonic_oos: mean predicted | isotonic_oos: observed |")
        lines.append("|---|---|---|---|---|---|")
        for i, (a, c) in enumerate(zip(r["reliability"]["raw"], r["reliability"]["isotonic_oos"]), 1):
            lines.append(f"| {i} | {a['n']} | {a['mean_predicted']:.3f} | {a['observed_rate']:.3f} | "
                         f"{c['mean_predicted']:.3f} | {c['observed_rate']:.3f} |")
        lines.append("")
    return "\n".join(lines) + "\n"
