"""
Leave-one-repository-out ("pooled") evaluation of the v2 failure model (EXPLORATORY - see docs/IMPROVEMENT_RESULTS.md).

Idea: a repository with few failures cannot teach a model much, but failure *patterns* that the v2 features describe
(a failing job tends to keep failing in the same PR, bot actors, dependency changes ...) may transfer.  For a target
repository the model is trained on

* the target's own runs before the fold, and
* the most recent ``OTHER_ROWS_CAP`` labelled rows of every OTHER repository that were **already in the past** when the
  fold's first test run started (so nothing from the future of any repository is used),

and scored on the fold's test runs, exactly like ``rolling_eval`` (same folds, same baseline, same paired cluster bootstrap).
It is compared with the same model trained on the target alone.

This was designed AFTER the pre-registered v2 evaluation (docs/IMPROVEMENT_PLAN.md, docs/IMPROVE_EVAL_TEST.md), so the
repositories it is evaluated on are no longer unseen: the figures are a lead.  The confirmation is a frozen fresh-data test
(``--after``) on runs that did not exist when the model was fixed.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.metrics import average_precision_score

from gha_optimizer.features.feature_extraction import FAILURE_V2_COLUMNS
from gha_optimizer.models.evaluation import chronological, usable_features
from gha_optimizer.models.failure_predictor import FailurePredictor
from gha_optimizer.models.rolling_eval import MIN_POSITIVES_TO_JUDGE, SEED, rolling_origin_folds

OTHER_ROWS_CAP = 5000
STRUCTURAL = ["job_structurally_impacted", "cache_key_touched", "dep_graph_distance", "any_changed_file_in_graph"]


def _labelled(df: pd.DataFrame) -> pd.DataFrame:
    d = chronological(df)
    d = d[d["label_failed"].notna()].copy()
    d["_run"] = d["run_id"].astype(str)
    return d


def evaluate_pooled_repo(frames: dict[str, pd.DataFrame], target: str, n_folds: int = 5, initial_fraction: float = 0.5,
                         min_train_runs: int = 20, n_boot: int = 1000, cutoff: str | None = None) -> dict[str, Any]:
    own = _labelled(frames[target])
    others = {r: _labelled(f) for r, f in frames.items() if r != target and len(f)}
    feats_all = [c for c in FAILURE_V2_COLUMNS if c not in STRUCTURAL]     # no graph features: they are repo-specific
    runs = list(dict.fromkeys(own["_run"]))
    result: dict[str, Any] = {"repo": target, "n_runs": len(runs)}
    if cutoff is None:
        folds = rolling_origin_folds(runs, n_folds, initial_fraction, min_train_runs)
    else:
        first_ts = own.groupby("_run", sort=False)["timestamp"].first()
        before = [r for r in runs if str(first_ts[r]) <= cutoff]
        after = [r for r in runs if str(first_ts[r]) > cutoff]
        folds = [(before, after)] if len(before) >= min_train_runs and after else []
    ys, rs, p_pool, p_own, p_base = [], [], [], [], []
    used = 0
    for train_runs, test_runs in folds:
        train, test = own[own["_run"].isin(train_runs)], own[own["_run"].isin(test_runs)]
        if len(test) == 0 or train["label_failed"].nunique() < 2:
            continue
        t0 = str(test["timestamp"].min())
        pool_parts = [train] + [o[o["timestamp"].astype(str) < t0].tail(OTHER_ROWS_CAP) for o in others.values()]
        pooled = pd.concat([p for p in pool_parts if len(p)], ignore_index=True)
        feats = usable_features(pooled, feats_all)
        est = clone(FailurePredictor(variant="v2_hgb").model)
        p_pool.append(est.fit(pooled[feats].astype(float), pooled["label_failed"].astype(int)).predict_proba(test[feats].astype(float))[:, 1])
        f_own = usable_features(train, feats_all)
        est2 = clone(FailurePredictor(variant="v2_hgb").model)
        p_own.append(est2.fit(train[f_own].astype(float), train["label_failed"].astype(int)).predict_proba(test[f_own].astype(float))[:, 1])
        fill = float(train["label_failed"].mean())
        p_base.append(test["historical_failure_rate"].astype(float).fillna(fill).to_numpy())
        ys.append(test["label_failed"].to_numpy(dtype=float))
        rs.append(test["_run"].to_numpy())
        used += 1
    result.update({"folds_planned": len(folds), "folds_used": used})
    if not used:
        result["note"] = "no usable fold"
        return result
    y, run_arr = np.concatenate(ys), np.concatenate(rs)
    P = {"pooled": np.concatenate(p_pool), "within_repo": np.concatenate(p_own), "baseline": np.concatenate(p_base)}
    result.update({"n_test_rows": int(len(y)), "positives": int(y.sum()), "base_rate": round(float(y.mean()), 4)})
    if not 0 < y.sum() < len(y):
        result["note"] = "one outcome class in the test rows"
        return result
    result["pr_auc"] = {k: round(float(average_precision_score(y, p)), 4) for k, p in P.items()}
    rng = np.random.default_rng(SEED)
    idx: dict[str, list[int]] = {}
    for i, r in enumerate(run_arr):
        idx.setdefault(r, []).append(i)
    groups = [np.array(v) for v in idx.values()]
    samples: dict[str, list[float]] = {"pooled - baseline": [], "pooled - within_repo": [], "within_repo - baseline": []}
    for _ in range(n_boot):
        ix = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        yb = y[ix]
        if not 0 < yb.sum() < len(yb):
            continue
        m = {k: average_precision_score(yb, p[ix]) for k, p in P.items()}
        samples["pooled - baseline"].append(m["pooled"] - m["baseline"])
        samples["pooled - within_repo"].append(m["pooled"] - m["within_repo"])
        samples["within_repo - baseline"].append(m["within_repo"] - m["baseline"])
    result["bootstrap"] = {k: ({"delta": round(float(np.mean(v)), 4),
                                "ci95": [round(float(np.percentile(v, 2.5)), 4), round(float(np.percentile(v, 97.5)), 4)],
                                "n": len(v)} if len(v) >= 50 else {"note": f"only {len(v)} usable resamples"})
                           for k, v in samples.items()}
    return result


def verdict(r: dict[str, Any], key: str = "pooled - baseline") -> str:
    ci = r.get("bootstrap", {}).get(key, {})
    if "ci95" not in r.get("bootstrap", {}).get(key, {}) or r.get("positives", 0) < MIN_POSITIVES_TO_JUDGE:
        return "not judged"
    lo, hi = ci["ci95"]
    return "win" if lo > 0 else "loss" if hi < 0 else "inconclusive"


def render_pooled(results: list[dict[str, Any]], n_boot: int, cutoff: str | None = None) -> str:
    out = ["# Pooled (leave-one-repository-out) failure model - EXPLORATORY\n",
           "Generated by `gha-optimizer eval-pooled`. Method, limits and why this is only a lead: "
           "`gha_optimizer/models/pooled_eval.py`. For each repository the v2 boosted-tree model is trained on that repository's "
           "own earlier runs plus the most recent past rows of every other repository, and compared with the same model trained "
           f"on the repository alone and with the baseline (the job's prior failure rate). Paired cluster bootstrap over runs "
           f"({n_boot} resamples, seed {SEED}); "
           + ("rolling-origin folds.\n" if cutoff is None else f"ONE split at {cutoff} (fresh-data test).\n")]
    rows = []
    for r in results:
        if "pr_auc" not in r:
            rows.append([r["repo"], r.get("positives", "-"), "-", "-", "-", "-", "-", r.get("note", "no result")])
            continue
        b = r["bootstrap"]

        def f(k):
            return (f"{b[k]['delta']:+.4f} [{b[k]['ci95'][0]:+.4f}, {b[k]['ci95'][1]:+.4f}]" if "ci95" in b.get(k, {}) else "n/a")
        rows.append([r["repo"], f"{r['positives']} ({100 * r['base_rate']:.1f}%)", r["pr_auc"]["pooled"], r["pr_auc"]["within_repo"],
                     r["pr_auc"]["baseline"], f("pooled - baseline") + f" **{verdict(r)}**", f("pooled - within_repo"),
                     f("within_repo - baseline")])
    out.append(_table(["repo", "positives", "pooled PR-AUC", "own-data-only PR-AUC", "baseline PR-AUC", "pooled - baseline",
                       "pooled - own-data-only", "own-data-only - baseline"], rows))
    wins = [r["repo"] for r in results if verdict(r) == "win"]
    losses = [r["repo"] for r in results if verdict(r) == "loss"]
    out.append(f"Pooled vs baseline (interval excludes 0, >= {MIN_POSITIVES_TO_JUDGE} positives): **{len(wins)} won** "
               f"({', '.join(wins) or 'none'}), {len(losses)} lost ({', '.join(losses) or 'none'}).\n")
    return "\n".join(out)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"
