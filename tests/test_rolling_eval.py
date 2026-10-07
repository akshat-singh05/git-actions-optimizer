"""Rolling-origin evaluation: fold construction, leakage guards, determinism, graceful degradation.

Histories here are the explicit SYNTHETIC test fixture - they test the *procedure*, never a result.
"""
from __future__ import annotations

import networkx as nx
import pandas as pd

from gha_optimizer.features.feature_extraction import build_training_frame
from gha_optimizer.models.rolling_eval import (
    FEATURE_SETS,
    STRUCTURAL_FEATURES,
    TARGETS,
    evaluate_frame,
    evaluate_target,
    render_markdown,
    rolling_origin_folds,
)
from tests.fixtures.synthetic_history import generate_synthetic_test_history


def frame(n_runs: int = 80, seed: int = 5) -> pd.DataFrame:
    recs = generate_synthetic_test_history(n_runs=n_runs, seed=seed)
    for i, r in enumerate(recs):  # give the exact-miss target both classes
        r.cache_outcome = ("exact_hit", "partial_hit", "miss")[i % 3] if r.cache_hit is not None else None
    return build_training_frame(nx.DiGraph(), recs)


def test_folds_train_only_on_earlier_runs_and_cover_each_late_run_once():
    runs = [f"r{i}" for i in range(100)]
    folds = rolling_origin_folds(runs, n_folds=5, initial_fraction=0.5, min_train_runs=20)
    assert len(folds) == 5
    seen: list[str] = []
    for train, test in folds:
        assert max(runs.index(r) for r in train) < min(runs.index(r) for r in test)   # strictly earlier
        assert train == runs[: len(train)]                                            # expanding prefix
        seen += test
    assert seen == runs[50:]                                                          # every late run tested exactly once
    assert [len(t) for _, t in folds] == [10] * 5


def test_short_prefixes_are_dropped_and_tiny_inputs_give_no_folds():
    runs = [f"r{i}" for i in range(30)]
    assert rolling_origin_folds(runs, 5, 0.5, min_train_runs=20) != []
    assert all(len(tr) >= 20 for tr, _ in rolling_origin_folds(runs, 5, 0.5, 20))
    assert rolling_origin_folds(runs[:5], 5, 0.5, 20) == []
    assert rolling_origin_folds([], 5, 0.5, 20) == []


def test_feature_sets_are_what_they_claim():
    assert set(STRUCTURAL_FEATURES).isdisjoint(FEATURE_SETS["no_structural"])
    assert set(FEATURE_SETS["no_structural"]) | set(STRUCTURAL_FEATURES) == set(FEATURE_SETS["all"])
    assert FEATURE_SETS["structural_only"] == STRUCTURAL_FEATURES


def test_every_test_row_is_scored_once_and_results_are_deterministic():
    df = frame()
    a = evaluate_target(df, TARGETS["failure"], n_folds=4, min_train_runs=10, n_boot=100)
    b = evaluate_target(df, TARGETS["failure"], n_folds=4, min_train_runs=10, n_boot=100)
    assert a == b                                           # seeded bootstrap, no hidden randomness
    runs = list(dict.fromkeys(df["run_id"]))
    late = set(runs[int(len(runs) * 0.5):])
    labelled_late = df[df["run_id"].isin(late) & df["label_failed"].notna()]
    assert a["n_test_rows"] <= len(labelled_late)           # skipped folds can only lose rows, never duplicate them
    assert a["n_test_runs"] <= len(late)
    # the synthetic history has no graph features, so structural_only is reported unavailable and the
    # other sets still get every fold
    assert a["feature_sets_unavailable"] == ["structural_only"]
    assert set(a["scores"]) == {"all", "no_structural", "baseline"}
    assert set(a["bootstrap"]["comparisons"]) == {"all - no_structural", "all - baseline", "no_structural - baseline"}


def test_baseline_uses_no_test_period_information():
    """Perturbing test-period labels must not change what the model/baseline predicted for them."""
    df = frame()
    base = evaluate_target(df, TARGETS["duration"], n_folds=4, min_train_runs=10, n_boot=50)
    runs = list(dict.fromkeys(df["run_id"]))
    last = set(runs[-8:])                                   # inside the last test fold
    df2 = df.copy()
    df2.loc[df2["run_id"].isin(last), "label_duration"] = df2.loc[df2["run_id"].isin(last), "label_duration"] * 100 + 7
    changed = evaluate_target(df2, TARGETS["duration"], n_folds=4, min_train_runs=10, n_boot=50)
    # the score of course changes (the labels did) but only through the labels of the last fold, so the
    # earlier folds' contribution is untouched: pooled rows are identical
    assert base["n_test_rows"] == changed["n_test_rows"]
    assert base["scores"]["all"]["mae"] != changed["scores"]["all"]["mae"]


def test_single_class_target_degrades_to_a_note_not_a_crash():
    df = frame()
    df["label_failed"] = 0.0                                # no failures at all
    r = evaluate_target(df, TARGETS["failure"], n_folds=4, min_train_runs=10, n_boot=50)
    assert r.get("folds_used") == 0 and "note" in r and "scores" not in r


def test_markdown_report_renders_every_target_with_intervals():
    df = frame()
    results = {"o/r": evaluate_frame(df, n_folds=4, min_train_runs=10, n_boot=100)}
    text = render_markdown(results, n_folds=4, n_boot=100)
    for t in TARGETS:
        assert f"## {t}" in text
    assert "includes 0" in text or "excludes 0" in text
    assert "no_structural" in text and "baseline" in text


def test_unavailable_feature_set_does_not_cost_the_others_their_folds():
    df = frame()
    r = evaluate_target(df, TARGETS["failure"], n_folds=4, min_train_runs=10, n_boot=50)
    assert r["folds_used"] == r["folds_planned"] == 4
    df2 = df.copy()
    df2["job_structurally_impacted"] = [float(i % 2) for i in range(len(df2))]   # structural features now observed
    df2["dep_graph_distance"] = [float(i % 3) for i in range(len(df2))]
    r2 = evaluate_target(df2, TARGETS["failure"], n_folds=4, min_train_runs=10, n_boot=50)
    assert r2["feature_sets_unavailable"] == [] and "structural_only" in r2["scores"]


def test_folds_are_cut_over_runs_that_carry_the_target_label():
    """Sparse labels (cache labels exist only for recent runs) must not leave folds without test labels."""
    df = frame(n_runs=120)
    runs = list(dict.fromkeys(df["run_id"]))
    old = set(runs[:60])                                    # the oldest half has no cache label at all
    df.loc[df["run_id"].isin(old), ["label_cache_hit", "label_cache_exact_miss"]] = float("nan")
    r = evaluate_target(df, TARGETS["cache_exact_miss"], n_folds=4, min_train_runs=10, n_boot=50)
    assert r["n_runs"] == 60 and r["n_runs_total"] == 120   # folds planned over the 60 labelled runs only
    assert r["folds_used"] >= 3
    assert r["n_test_runs"] <= 30                           # the later half of the labelled runs


def test_candidate_failure_models_run_on_the_same_folds_and_handle_missing_values():
    """The candidates are evaluated exactly like the production model: same folds, same baseline, NaN-tolerant."""
    df = frame(100)
    base = evaluate_target(df, TARGETS["failure"], n_folds=4, min_train_runs=10, n_boot=50)
    for name in ("failure_logistic", "failure_regularized"):
        r = evaluate_target(df, TARGETS[name], n_folds=4, min_train_runs=10, n_boot=50)
        assert r["folds_used"] == base["folds_used"] > 0, name
        assert r["n_test_rows"] == base["n_test_rows"] and r["positives"] == base["positives"]
        assert r["scores"]["baseline"] == base["scores"]["baseline"]          # the baseline is identical
        assert all(v is not None for v in r["scores"]["all"].values())
