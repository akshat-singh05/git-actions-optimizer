"""Tests for the enhanced feature extraction module."""
from pathlib import Path

import pandas as pd

from tests.fixtures.synthetic_history import generate_synthetic_test_history
from gha_optimizer.features.feature_extraction import (
    FEATURE_COLUMNS,
    build_inference_frame,
    build_training_frame,
    _exponential_decay_mean,
    _streak_count,
)
from gha_optimizer.graph.dependency_graph import build_dependency_graph
from gha_optimizer.ingestion.workflow_parser import parse_workflow

FIXTURE_WORKFLOW = Path(__file__).resolve().parent / "fixtures" / "sample_workflow.yml"
FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"

TRACKED_FILES = [
    "src/app.py", "src/utils.py", "src/api/routes.py",
    "tests/unit/test_app.py", "tests/integration/test_api.py",
    "requirements.txt",
]


def _setup():
    workflow = parse_workflow(FIXTURE_WORKFLOW)
    graph = build_dependency_graph(workflow, FIXTURE_REPO, TRACKED_FILES)
    history = generate_synthetic_test_history(n_runs=30)
    return graph, history, workflow


def test_training_frame_has_all_feature_columns():
    graph, history, _ = _setup()
    df = build_training_frame(graph, history)
    for col in FEATURE_COLUMNS:
        assert col in df.columns, f"Missing feature column: {col}"


def test_training_frame_has_labels():
    graph, history, _ = _setup()
    df = build_training_frame(graph, history)
    assert "label_cache_hit" in df.columns
    assert "label_failed" in df.columns
    assert "label_duration" in df.columns


def test_inference_frame_has_all_feature_columns():
    graph, _, workflow = _setup()
    stats = {j: {"failure_rate": 0.1, "mean_duration": 60.0} for j in workflow.jobs}
    df = build_inference_frame(graph, list(workflow.jobs.keys()), ["src/app.py"], stats)
    for col in FEATURE_COLUMNS:
        assert col in df.columns, f"Missing feature column: {col}"


def test_inference_frame_rows_match_jobs():
    graph, _, workflow = _setup()
    stats = {j: {"failure_rate": 0.1, "mean_duration": 60.0} for j in workflow.jobs}
    df = build_inference_frame(graph, list(workflow.jobs.keys()), ["src/app.py"], stats)
    assert len(df) == len(workflow.jobs)


def test_exponential_decay_mean():
    # Most recent value should dominate
    result = _exponential_decay_mean([0.0, 0.0, 0.0, 1.0], decay=0.5)
    assert result > 0.5  # recent 1.0 should pull mean up


def test_exponential_decay_mean_empty():
    assert _exponential_decay_mean([]) == 0.0


def test_streak_count():
    assert _streak_count([0, 1, 1, 1], 1) == 3
    assert _streak_count([1, 1, 0, 0], 0) == 2
    assert _streak_count([0, 0, 0], 1) == 0
    assert _streak_count([], 1) == 0


def test_new_features_populated():
    graph, history, _ = _setup()
    df = build_training_frame(graph, history)
    # LOC features should exist
    assert "loc_added" in df.columns
    assert "loc_deleted" in df.columns
    # Author feature
    assert "author_failure_rate" in df.columns
    # Streak features
    assert "recent_failure_streak" in df.columns
    assert "recent_success_streak" in df.columns
    # Graph distance
    assert "dep_graph_distance" in df.columns
    # Temporal
    assert "day_of_week" in df.columns
    assert "is_weekend" in df.columns
