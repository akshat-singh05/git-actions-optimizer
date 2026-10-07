"""Cache-miss targets: 'any_miss' (restore-keys fallback counts as a hit) vs 'exact_miss' (exact key not restored)."""
from __future__ import annotations

import math

import networkx as nx
import pytest

from gha_optimizer.features.feature_extraction import build_training_frame
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor, miss_target
from tests.fixtures.synthetic_history import generate_synthetic_test_history

OUTCOMES = ["exact_hit", "partial_hit", "miss", "hit_unresolved", None]
HIT = {"exact_hit": True, "partial_hit": True, "miss": False, "hit_unresolved": True, None: None}


def history():
    """SYNTHETIC wiring input: real record shape, outcomes assigned round-robin."""
    recs = generate_synthetic_test_history(n_runs=40, seed=3)
    for i, r in enumerate(recs):
        r.cache_outcome = OUTCOMES[i % len(OUTCOMES)]
        r.cache_hit = HIT[r.cache_outcome]
    return recs


def test_exact_miss_label_semantics():
    df = build_training_frame(nx.DiGraph(), history())
    for outcome, want_exact, want_any in (
        ("exact_hit", 0.0, 0.0),      # (label_cache_exact_miss, 1 - label_cache_hit)
        ("partial_hit", 1.0, 0.0),    # exact key not restored, but the cache was restored
        ("miss", 1.0, 1.0),
    ):
        sub = df[df.cache_outcome == outcome]
        assert len(sub) > 0
        assert set(sub.label_cache_exact_miss) == {want_exact}
        assert set(1 - sub.label_cache_hit) == {want_any}
    # unresolved restores and rows without a label carry no exact-miss label
    assert df[df.cache_outcome == "hit_unresolved"].label_cache_exact_miss.isna().all()
    assert df[df.cache_outcome.isna()].label_cache_exact_miss.isna().all()


def test_targets_select_the_right_label_column():
    df = build_training_frame(nx.DiGraph(), history())
    labelled = df.dropna(subset=["label_cache_exact_miss"])
    assert miss_target(labelled, "exact_miss").equals(labelled["label_cache_exact_miss"])
    assert miss_target(labelled, "any_miss").equals(1 - labelled["label_cache_hit"])
    # the two targets genuinely differ exactly on partial hits
    differing = labelled[miss_target(labelled, "exact_miss") != miss_target(labelled, "any_miss")]
    assert set(differing.cache_outcome) == {"partial_hit"}


def test_model_trains_on_each_target_and_persists_it(tmp_path):
    df = build_training_frame(nx.DiGraph(), history())
    for target in ("any_miss", "exact_miss"):
        model = CacheMissPredictor(backend="sklearn", target=target)
        metrics = model.fit(df)
        assert metrics["target"] == target
        path = tmp_path / f"{target}.joblib"
        model.save(path)
        loaded = CacheMissPredictor(backend="sklearn")
        loaded.load(path)
        assert loaded.target == target
    # the exact target has more positives (partial hits are misses) than the any-miss target
    a = CacheMissPredictor(backend="sklearn", target="any_miss").fit(df)["miss_rate_all_labelled"]
    e = CacheMissPredictor(backend="sklearn", target="exact_miss").fit(df)["miss_rate_all_labelled"]
    assert e > a
    assert not math.isnan(e)


def test_unknown_target_is_rejected():
    with pytest.raises(ValueError):
        CacheMissPredictor(target="bogus")
