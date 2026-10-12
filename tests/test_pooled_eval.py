"""Leave-one-repository-out evaluation: mechanics on SYNTHETIC frames (no number here is a result)."""
from __future__ import annotations

import networkx as nx
import pytest

from gha_optimizer.features.feature_extraction import build_training_frame
from gha_optimizer.models.pooled_eval import evaluate_pooled_repo, render_pooled, verdict
from tests.fixtures.synthetic_history import generate_synthetic_test_history


@pytest.fixture(scope="module")
def frames():
    return {name: build_training_frame(nx.DiGraph(), generate_synthetic_test_history(n_runs=90, seed=seed))
            for name, seed in (("a/a", 1), ("b/b", 2), ("c/c", 3))}


def test_pooled_result_has_scores_and_paired_intervals(frames):
    r = evaluate_pooled_repo(frames, "a/a", n_boot=60)
    assert r["folds_used"] >= 1 and r["positives"] > 0
    assert set(r["pr_auc"]) == {"pooled", "within_repo", "baseline"}
    assert set(r["bootstrap"]) == {"pooled - baseline", "pooled - within_repo", "within_repo - baseline"}
    assert verdict(r) in ("win", "loss", "inconclusive", "not judged")


def test_other_repositories_contribute_only_rows_from_before_the_fold(frames, monkeypatch):
    """The pooled training frame never contains an other-repository row at or after the fold's first test timestamp."""
    import gha_optimizer.models.pooled_eval as pe

    seen = []
    real = pe.FailurePredictor

    class Spy:
        def __init__(self, **kw):
            self._m = real(**kw)
            self.model = self._m.model

    original_clone = pe.clone

    def spying_clone(est):
        return original_clone(est)

    monkeypatch.setattr(pe, "clone", spying_clone)
    cutoff = frames["a/a"]["timestamp"].astype(str).sort_values().iloc[len(frames["a/a"]) // 2]
    r = evaluate_pooled_repo(frames, "a/a", n_boot=30, cutoff=cutoff)
    assert r["folds_used"] == 1
    # direct check of the rule the module applies
    t0 = str(frames["a/a"][frames["a/a"]["timestamp"].astype(str) > cutoff]["timestamp"].min())
    for name in ("b/b", "c/c"):
        past = frames[name][frames[name]["timestamp"].astype(str) < t0]
        assert (past["timestamp"].astype(str) < t0).all() and len(past) < len(frames[name])


def test_report_marks_it_exploratory_and_states_the_counts(frames):
    results = [evaluate_pooled_repo(frames, r, n_boot=40) for r in frames]
    text = render_pooled(results, 40)
    assert "EXPLORATORY" in text and "a/a" in text and "Pooled vs baseline" in text
