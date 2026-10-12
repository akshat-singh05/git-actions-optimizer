"""FailurePredictor variants: logistic (default), tree (the original) and regularised tree.

Histories are the explicit SYNTHETIC fixture - these tests check mechanics, never a result.
"""
from __future__ import annotations

import joblib
import networkx as nx
import numpy as np
import pytest

from gha_optimizer.features.feature_extraction import build_training_frame
from gha_optimizer.models.failure_predictor import VARIANTS, FailurePredictor
from gha_optimizer.models.rolling_eval import TARGETS
from tests.fixtures.synthetic_history import generate_synthetic_test_history


def frame(n_runs: int = 80):
    return build_training_frame(nx.DiGraph(), generate_synthetic_test_history(n_runs=n_runs, seed=9))


def test_logistic_is_the_default_variant():
    assert FailurePredictor().variant == "logistic"
    assert set(VARIANTS) == {"logistic", "tree", "regularized_tree", "v2_logistic", "v2_hgb", "v2_blend"}


@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_fits_predicts_probabilities_and_reports_its_variant(variant):
    df = frame()
    m = FailurePredictor(variant=variant)
    metrics = m.fit(df)
    assert metrics["variant"] == variant
    p = m.predict_proba(df.tail(25)).to_numpy()
    assert p.shape == (25,) and np.all((p >= 0) & (p <= 1)) and not np.isnan(p).any()


def test_logistic_handles_missing_values_without_dropping_rows():
    df = frame()
    assert df["historical_failure_rate"].isna().any()                  # the first runs of every job have no prior
    m = FailurePredictor()
    m.fit(df)
    first = df[df["historical_failure_rate"].isna()].head(5)
    assert m.predict_proba(first).notna().all()


def test_variant_survives_save_load_and_old_artifacts_load_as_the_original_tree(tmp_path):
    df = frame()
    m = FailurePredictor(variant="regularized_tree")
    m.fit(df)
    m.save(tmp_path / "f.joblib")
    loaded = FailurePredictor()                                          # constructor default is logistic ...
    loaded.load(tmp_path / "f.joblib")
    assert loaded.variant == "regularized_tree"                          # ... the artifact decides
    assert np.allclose(loaded.predict_proba(df.tail(10)), m.predict_proba(df.tail(10)))
    # an artifact written before the variant option existed has no "variant" key: it was the original tree
    legacy = joblib.load(tmp_path / "f.joblib")
    legacy.pop("variant")
    joblib.dump(legacy, tmp_path / "legacy.joblib")
    old = FailurePredictor()
    old.load(tmp_path / "legacy.joblib")
    assert old.variant == "tree"


def test_unknown_variant_is_rejected():
    with pytest.raises(ValueError):
        FailurePredictor(variant="bogus")


def test_evaluator_targets_are_built_from_the_model_class_not_duplicated_settings():
    from sklearn.linear_model import LogisticRegression
    default = TARGETS["failure"].estimator()
    assert isinstance(default.steps[-1][1], LogisticRegression)          # "failure" == the default variant
    assert repr(TARGETS["failure_tree"].estimator()) == repr(FailurePredictor(variant="tree").model)
    assert repr(TARGETS["failure_regularized_tree"].estimator()) == repr(
        FailurePredictor(variant="regularized_tree").model)


def test_v2_variants_use_the_extended_feature_list_and_the_old_ones_do_not():
    from gha_optimizer.features.feature_extraction import FAILURE_V2_COLUMNS, FEATURE_COLUMNS

    df = frame()
    for variant in ("v2_logistic", "v2_hgb", "v2_blend"):
        m = FailurePredictor(variant=variant)
        m.fit(df)
        assert m._feature_list == list(FAILURE_V2_COLUMNS)
        assert set(m._features) <= set(FAILURE_V2_COLUMNS)
    assert FailurePredictor(variant="logistic")._feature_list == list(FEATURE_COLUMNS)


def test_blend_is_the_mean_of_its_members_and_survives_save_load(tmp_path):
    df = frame()
    blend, lo, hg = (FailurePredictor(variant=v) for v in ("v2_blend", "v2_logistic", "v2_hgb"))
    for m in (blend, lo, hg):
        m.fit(df)
    tail = df.tail(15)
    expected = (lo.predict_proba(tail) + hg.predict_proba(tail)) / 2
    assert np.allclose(blend.predict_proba(tail), expected, atol=1e-9)
    blend.save(tmp_path / "b.joblib")
    loaded = FailurePredictor()
    loaded.load(tmp_path / "b.joblib")
    assert loaded.variant == "v2_blend" and np.allclose(loaded.predict_proba(tail), blend.predict_proba(tail))


def test_v2_targets_are_registered_with_their_own_feature_sets():
    from gha_optimizer.features.feature_extraction import FAILURE_V2_COLUMNS
    from gha_optimizer.models.rolling_eval import feature_sets_for

    for name in ("failure_v2_logistic", "failure_v2_hgb", "failure_v2_blend"):
        sets = feature_sets_for(TARGETS[name])
        assert sets["all"] == list(FAILURE_V2_COLUMNS)
        assert "job_structurally_impacted" not in sets["no_structural"]
