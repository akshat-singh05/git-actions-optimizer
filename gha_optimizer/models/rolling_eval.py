"""
Rolling-origin (expanding-window) evaluation with feature-set ablation and paired bootstrap intervals.

Why this exists: a single "last 20% of runs" holdout holds 1-11 failure positives per repository, so
its metrics cannot separate a model from chance.  Here

* the runs are ordered by time and cut into ``n_folds`` consecutive test blocks after an initial
  training prefix; each fold trains on **all earlier runs only** and predicts its block, so every
  test row is out-of-sample and appears exactly once;
* the out-of-sample predictions of all folds are **pooled** (more positives than any single holdout);
* the same folds are used for every feature set, so feature sets are compared like for like:
  ``all`` features, ``no_structural`` (the graph features removed), ``structural_only``, and a naive
  **baseline** that needs no model (the job's prior failure rate / prior cache-miss rate / prior mean
  duration, NaN filled with the *training fold's* rate - never the test rate);
* every difference between two feature sets carries a **paired cluster bootstrap** interval (runs, not
  rows, are resampled: jobs of one run share a commit and are not independent).

What it does not fix: consecutive runs of one PR still share a lineage, so intervals are optimistic, and a
small number of positives still gives wide intervals - the report prints the counts so that is visible.
Nothing here is tuned on the test folds; hyper-parameters are those of the production model classes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.metrics import average_precision_score, brier_score_loss, mean_absolute_error, roc_auc_score

from gha_optimizer.features.feature_extraction import FAILURE_V2_COLUMNS, FEATURE_COLUMNS
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor, miss_target
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.evaluation import chronological, from_log_ratio, log_ratio, usable_features
from gha_optimizer.models.failure_predictor import FailurePredictor

STRUCTURAL_FEATURES = ["job_structurally_impacted", "cache_key_touched", "dep_graph_distance"]
FEATURE_SETS: dict[str, list[str]] = {
    "all": list(FEATURE_COLUMNS),
    "no_structural": [c for c in FEATURE_COLUMNS if c not in STRUCTURAL_FEATURES],
    "structural_only": list(STRUCTURAL_FEATURES),
}
SEED = 13


@dataclass
class TargetSpec:
    name: str
    kind: str                                       # "binary" | "regression"
    label: Callable[[pd.DataFrame], pd.Series]      # label per row (NaN = unlabelled)
    baseline: Callable[[pd.DataFrame], pd.Series]   # naive score per row (NaN allowed)
    estimator: Callable[[], Any]
    baseline_description: str
    log_ratio: bool = False  # regression on log((y+1)/(prior+1)) with prior = the baseline score (see DurationEstimator)
    features: list[str] | None = None  # replaces FEATURE_COLUMNS as the 'all' feature set (the v2 failure candidates)


TARGETS: dict[str, TargetSpec] = {
    "failure": TargetSpec(
        "failure", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor().model, "the job's prior decayed failure rate"),
    # The other FailurePredictor variants, evaluated on the same folds against the same baseline.  "failure" above
    # is the default variant (logistic); the variants' settings were fixed before any fold result was seen.
    "failure_tree": TargetSpec(
        "failure_tree", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor(variant="tree").model, "the job's prior decayed failure rate"),
    "failure_regularized_tree": TargetSpec(
        "failure_regularized_tree", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor(variant="regularized_tree").model, "the job's prior decayed failure rate"),
    # Protocol-v2 candidates (docs/IMPROVEMENT_PLAN.md): extra run-context / lineage / change-content features
    "failure_v2_logistic": TargetSpec(
        "failure_v2_logistic", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor(variant="v2_logistic").model, "the job's prior decayed failure rate",
        features=FAILURE_V2_COLUMNS),
    "failure_v2_hgb": TargetSpec(
        "failure_v2_hgb", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor(variant="v2_hgb").model, "the job's prior decayed failure rate",
        features=FAILURE_V2_COLUMNS),
    "failure_v2_blend": TargetSpec(
        "failure_v2_blend", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor(variant="v2_blend").model, "the job's prior decayed failure rate",
        features=FAILURE_V2_COLUMNS),
    "failure_v2_shrunk": TargetSpec(
        "failure_v2_shrunk", "binary", lambda d: d["label_failed"], lambda d: d["historical_failure_rate"],
        lambda: FailurePredictor(variant="v2_shrunk").model, "the job's prior decayed failure rate",
        features=FAILURE_V2_COLUMNS),
    "cache_any_miss": TargetSpec(
        "cache_any_miss", "binary",
        lambda d: miss_target(d, "any_miss").where(d["label_cache_hit"].notna()),
        lambda d: 1 - d["cache_key_stability"],
        lambda: CacheMissPredictor().model, "1 - the job's prior decayed cache-hit rate"),
    "cache_exact_miss": TargetSpec(
        "cache_exact_miss", "binary", lambda d: d["label_cache_exact_miss"],
        lambda d: 1 - d["cache_key_stability"],
        lambda: CacheMissPredictor(target="exact_miss").model,
        "1 - the job's prior decayed cache-hit rate (a proxy: no exact-key prior is tracked)"),
    "duration": TargetSpec(
        "duration", "regression", lambda d: d["label_duration"], lambda d: d["historical_mean_duration"],
        lambda: DurationEstimator(target="seconds").model, "the job's prior decayed mean duration"),
    "duration_ratio": TargetSpec(
        "duration_ratio", "regression", lambda d: d["label_duration"], lambda d: d["historical_mean_duration"],
        lambda: DurationEstimator(target="log_ratio").model, "the job's prior decayed mean duration",
        log_ratio=True),
}


def feature_sets_for(spec: TargetSpec) -> dict[str, list[str]]:
    """The feature sets compared for ``spec``: its own ``features`` (default: the shared contract) as ``all``."""
    if spec.features is None:
        return FEATURE_SETS
    return {"all": list(spec.features),
            "no_structural": [c for c in spec.features if c not in STRUCTURAL_FEATURES],
            "structural_only": list(STRUCTURAL_FEATURES)}


def rolling_origin_folds(
    runs: list[str], n_folds: int = 5, initial_fraction: float = 0.5, min_train_runs: int = 20
) -> list[tuple[list[str], list[str]]]:
    """``[(train_runs, test_runs)]``: the runs after the initial prefix are cut into ``n_folds`` consecutive
    blocks; each block is tested with every earlier run as training data.  Folds whose training prefix is
    shorter than ``min_train_runs`` are dropped."""
    start = int(len(runs) * initial_fraction)
    rest = runs[start:]
    if not rest or n_folds < 1:
        return []
    blocks = [list(b) for b in np.array_split(np.array(rest, dtype=object), min(n_folds, len(rest))) if len(b)]
    folds, cursor = [], start
    for block in blocks:
        if cursor >= min_train_runs:
            folds.append((runs[:cursor], block))
        cursor += len(block)
    return folds


def _score_binary(y: np.ndarray, p: np.ndarray) -> dict[str, float | None]:
    out: dict[str, float | None] = {"brier": float(brier_score_loss(y, p)) if len(y) else None}
    if 0 < y.sum() < len(y):
        out["pr_auc"] = float(average_precision_score(y, p))
        out["roc_auc"] = float(roc_auc_score(y, p))
    else:
        out["pr_auc"] = out["roc_auc"] = None
    return out


def _metric(spec: TargetSpec, name: str, y: np.ndarray, p: np.ndarray) -> float | None:
    if spec.kind == "regression":
        return float(mean_absolute_error(y, p)) if len(y) else None
    return _score_binary(y, p)[name] if len(y) else None


def evaluate_target(
    df: pd.DataFrame, spec: TargetSpec, n_folds: int = 5, initial_fraction: float = 0.5,
    min_train_runs: int = 20, n_boot: int = 1000, cutoff: str | None = None,
) -> dict[str, Any]:
    """``cutoff`` (ISO timestamp, same format as the data): instead of rolling folds, ONE split - train on every
    run that started at or before the cutoff, test on every later run.  Used to validate a choice that was made
    from earlier evaluations on runs that arrived afterwards."""
    d = chronological(df)
    y_all = spec.label(d).astype(float)
    lab = d[y_all.notna()].copy()
    lab["_y"] = y_all[y_all.notna()]
    lab["_run"] = lab["run_id"].astype(str)
    # folds are cut over the runs that carry a label for THIS target: cache labels come from job logs and exist
    # only inside GitHub's log-retention window, so cutting over all runs left most folds without test labels
    runs = list(dict.fromkeys(lab["_run"]))
    if cutoff is None:
        folds = rolling_origin_folds(runs, n_folds, initial_fraction, min_train_runs)
    else:
        first_ts = lab.groupby("_run", sort=False)["timestamp"].first()
        before = [r for r in runs if str(first_ts[r]) <= cutoff]
        after = [r for r in runs if str(first_ts[r]) > cutoff]
        folds = [(before, after)] if len(before) >= min_train_runs and after else []
    result: dict[str, Any] = {"target": spec.name, "kind": spec.kind, "n_runs": len(runs),
                              "n_runs_total": int(d["run_id"].nunique()),
                              "n_labelled_rows": int(len(lab)), "baseline": spec.baseline_description}
    # a feature set with no observed value anywhere (e.g. structural_only when graph features were never
    # computed) is reported as unavailable; it must not cost the other sets their folds
    all_sets = feature_sets_for(spec)
    sets = {k: v for k, v in all_sets.items() if usable_features(lab, v)}
    result["feature_sets_unavailable"] = [k for k in all_sets if k not in sets]
    pooled: dict[str, list[np.ndarray]] = {k: [] for k in [*sets, "baseline"]}
    ys, run_ids, used, skipped = [], [], 0, []
    for i, (train_runs, test_runs) in enumerate(folds):
        train, test = lab[lab["_run"].isin(train_runs)], lab[lab["_run"].isin(test_runs)]
        if len(test) == 0 or len(train) < 10 or (spec.kind == "binary" and train["_y"].nunique() < 2):
            skipped.append(i)
            continue
        preds: dict[str, np.ndarray] = {}
        fill = float(train["_y"].mean())  # the training fold's own rate / mean fills a missing prior
        fit_rows = train
        if spec.log_ratio:  # rows without a prior have no ratio to learn from
            fit_rows = train[spec.baseline(train).notna()]
        for fname, cols in sets.items():
            feats = usable_features(fit_rows, cols)
            if not feats or len(fit_rows) < 10:
                break
            if spec.log_ratio:
                y_fit = log_ratio(fit_rows["_y"], spec.baseline(fit_rows).astype(float))
            else:
                y_fit = fit_rows["_y"].astype(int if spec.kind == "binary" else float)
            est = clone(spec.estimator()).fit(fit_rows[feats].astype(float), y_fit)
            if spec.kind == "binary":
                preds[fname] = est.predict_proba(test[feats].astype(float))[:, 1]
            elif spec.log_ratio:
                prior = spec.baseline(test).astype(float).fillna(fill).to_numpy()
                preds[fname] = from_log_ratio(prior, est.predict(test[feats].astype(float)))
            else:
                preds[fname] = np.maximum(est.predict(test[feats].astype(float)), 0.0)
        if len(preds) != len(sets):
            skipped.append(i)
            continue
        preds["baseline"] = spec.baseline(test).astype(float).fillna(fill).to_numpy()
        for k, v in preds.items():
            pooled[k].append(v)
        ys.append(test["_y"].to_numpy(dtype=float))
        run_ids.append(test["_run"].to_numpy())
        used += 1
    result.update({"folds_planned": len(folds), "folds_used": used, "folds_skipped": skipped})
    if used == 0:
        result["note"] = "no usable fold (too few runs or a single class in every training prefix)"
        return result

    y = np.concatenate(ys)
    runs_arr = np.concatenate(run_ids)
    P = {k: np.concatenate(v) for k, v in pooled.items()}
    result["n_test_rows"] = int(len(y))
    result["n_test_runs"] = int(len(set(runs_arr)))
    if spec.kind == "binary":
        result["positives"] = int(y.sum())
        result["base_rate"] = round(float(y.mean()), 4)
    names = ["pr_auc", "roc_auc", "brier"] if spec.kind == "binary" else ["mae"]
    result["scores"] = {
        k: ({n: _round(_metric(spec, n, y, p)) for n in names} if spec.kind == "binary"
            else {"mae": _round(_metric(spec, "mae", y, p))})
        for k, p in P.items()
    }

    # paired cluster bootstrap over runs
    rng = np.random.default_rng(SEED)
    run_lists: dict[str, list[int]] = {}
    for idx, r in enumerate(runs_arr):
        run_lists.setdefault(r, []).append(idx)
    run_index = {r: np.array(ix) for r, ix in run_lists.items()}
    keys = list(run_index)
    primary = "pr_auc" if spec.kind == "binary" else "mae"
    comparisons = [c for c in (("all", "no_structural"), ("all", "baseline"), ("no_structural", "baseline"))
                   if c[0] in P and c[1] in P]
    samples: dict[tuple[str, str], list[float]] = {c: [] for c in comparisons}
    for _ in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        ix = np.concatenate([run_index[keys[j]] for j in pick])
        yb = y[ix]
        if spec.kind == "binary" and not (0 < yb.sum() < len(yb)):
            continue
        for a, b in comparisons:
            ma, mb = _metric(spec, primary, yb, P[a][ix]), _metric(spec, primary, yb, P[b][ix])
            if ma is not None and mb is not None:
                samples[(a, b)].append(ma - mb)
    result["bootstrap"] = {
        "metric": primary, "resamples": n_boot, "higher_is_better": spec.kind == "binary",
        "comparisons": {
            f"{a} - {b}": _interval(v, n_boot) for (a, b), v in samples.items()
        },
    }
    return result


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def _interval(values: list[float], n_boot: int) -> dict[str, Any]:
    if len(values) < 50:
        return {"note": f"only {len(values)} usable resamples", "n": len(values)}
    v = np.array(values)
    return {"delta": round(float(np.mean(v)), 4), "ci95": [round(float(np.percentile(v, 2.5)), 4),
                                                           round(float(np.percentile(v, 97.5)), 4)],
            "n": len(values)}


def evaluate_frame(df: pd.DataFrame, targets: list[str] | None = None, **kw: Any) -> dict[str, dict[str, Any]]:
    return {t: evaluate_target(df, TARGETS[t], **kw) for t in (targets or list(TARGETS))}


# ---------------------------------------------------------------------------
# Markdown rendering (every figure comes from the dicts above)
# ---------------------------------------------------------------------------

MIN_POSITIVES_TO_JUDGE = 20


def win_loss(results: dict[str, dict[str, dict[str, Any]]], target: str,
             comparison: str = "all - baseline") -> dict[str, list[str]]:
    """Per repository: did ``target`` beat (``wins``) or lose to (``losses``) the baseline by a paired-bootstrap interval
    that excludes 0?  ``not_judged``: fewer than ``MIN_POSITIVES_TO_JUDGE`` positives (or no usable fold)."""
    out: dict[str, list[str]] = {"wins": [], "losses": [], "inconclusive": [], "not_judged": []}
    for repo, per in results.items():
        r = per.get(target, {})
        ci = r.get("bootstrap", {}).get("comparisons", {}).get(comparison, {})
        if "scores" not in r or r.get("positives", 0) < MIN_POSITIVES_TO_JUDGE or "ci95" not in ci:
            out["not_judged"].append(repo)
            continue
        lo, hi = ci["ci95"]
        out["wins" if lo > 0 else "losses" if hi < 0 else "inconclusive"].append(repo)
    return out


def _fmt_ci(c: dict[str, Any]) -> str:
    if "ci95" not in c:
        return c.get("note", "n/a")
    lo, hi = c["ci95"]
    verdict = "excludes 0" if (lo > 0 or hi < 0) else "includes 0"
    return f"{c['delta']:+.4f} [{lo:+.4f}, {hi:+.4f}] ({verdict})"


def render_markdown(results: dict[str, dict[str, dict[str, Any]]], n_folds: int, n_boot: int,
                    cutoff: str | None = None) -> str:
    design = (f"Expanding-window folds ({n_folds} requested), pooled out-of-sample predictions"
              if cutoff is None else
              f"**Fresh-data check: ONE split.** Trained on every run that started at or before {cutoff}, tested once "
              "on every later run (none of which existed when the choice under test was made)")
    out = ["# Rolling-origin model evaluation\n" if cutoff is None else "# Fresh-data model evaluation\n",
           "Generated by `gha-optimizer eval-models` from the database; every figure is computed. "
           f"{design}, paired cluster "
           f"bootstrap over runs ({n_boot} resamples, seed {SEED}). Method and caveats: "
           "`gha_optimizer/models/rolling_eval.py` docstring.\n",
           "Reading guide: a difference whose interval **includes 0** is not distinguishable from no difference "
           "on this data. Intervals are optimistic (runs of one PR share a lineage) and wide when positives are few.\n"]
    for target in next(iter(results.values())):
        spec = TARGETS[target]
        out.append(f"## {target}\n")
        out.append(f"Baseline (no model): {spec.baseline_description}.\n")
        rows, brows = [], []
        for repo, per in results.items():
            r = per[target]
            if "scores" not in r:
                rows.append([repo, r.get("n_labelled_rows", 0), "-", "-", "-", "-", "-", r.get("note", "no result")])
                continue
            s = r["scores"]
            if spec.kind == "binary":
                rows.append([repo, f"{r['n_test_rows']} / {r['n_test_runs']}", f"{r['positives']} ({100 * r['base_rate']:.1f}%)",
                             f"{r['folds_used']}/{r['folds_planned']}"] +
                            [f"{s[k]['pr_auc']}" if k in s else "n/a" for k in ("all", "no_structural", "structural_only", "baseline")])
            else:
                rows.append([repo, f"{r['n_test_rows']} / {r['n_test_runs']}", "-", f"{r['folds_used']}/{r['folds_planned']}"] +
                            [f"{s[k]['mae']}" if k in s else "n/a" for k in ("all", "no_structural", "structural_only", "baseline")])
            cmp = r["bootstrap"]["comparisons"]
            brows.append([repo] + [_fmt_ci(cmp[c]) if c in cmp else "n/a"
                                   for c in ("all - no_structural", "all - baseline", "no_structural - baseline")])
        metric = "PR-AUC (higher is better; a no-skill model scores the base rate)" if spec.kind == "binary" \
            else "MAE in seconds (lower is better)"
        hdr = ["repo", "test rows / runs", "positives", "folds used", "all", "no_structural", "structural_only", "baseline"]
        out.append(f"Pooled out-of-sample {metric}:\n")
        out.append(_md_table(hdr, rows))
        sign = "higher is better" if spec.kind == "binary" else "negative is better (lower MAE)"
        out.append(f"Paired difference ({'PR-AUC' if spec.kind == 'binary' else 'MAE'}; {sign}), mean and 95% interval:\n")
        out.append(_md_table(["repo", "all - no_structural", "all - baseline", "no_structural - baseline"], brows))
        if spec.kind == "binary":
            wl = win_loss(results, target)
            out.append(f"Versus the baseline (`all - baseline`, interval excludes 0; repositories with fewer than "
                       f"{MIN_POSITIVES_TO_JUDGE} positives are not judged): **{len(wl['wins'])} won**, "
                       f"{len(wl['losses'])} lost, {len(wl['inconclusive'])} inconclusive, "
                       f"{len(wl['not_judged'])} not judged. Won: {', '.join(wl['wins']) or 'none'}; "
                       f"lost: {', '.join(wl['losses']) or 'none'}.\n")
    return "\n".join(out)


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"
