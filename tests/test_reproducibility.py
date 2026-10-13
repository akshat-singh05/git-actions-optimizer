"""Identical inputs must give identical numbers.

The hour of day and weekday of the scored run are model features, and they used to default to the wall clock at scoring
time, so the same repository gave different durations / failure probabilities at different hours.  ``as_of`` makes that
instant an explicit input.  History is the explicitly SYNTHETIC test fixture: nothing here is a result.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import networkx as nx

from gha_optimizer.features.feature_extraction import build_inference_frame, compute_history_stats
from gha_optimizer.pipeline import run_pipeline
from tests.fixtures.synthetic_history import generate_synthetic_test_history

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRACKED = ["src/app.py", "src/utils.py", "src/api/routes.py", "tests/unit/test_app.py",
           "tests/integration/test_api.py", "requirements.txt"]
MONDAY_9 = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)


def test_time_features_come_from_as_of_not_from_the_wall_clock():
    frame = lambda t: build_inference_frame(nx.DiGraph(), ["a"], ["x.py"], {}, as_of=t).iloc[0]   # noqa: E731
    a, b = frame(MONDAY_9), frame(MONDAY_9 + timedelta(days=5, hours=8))
    assert (a["hour_of_day"], a["day_of_week"], a["is_weekend"]) == (9.0, 0.0, 0.0)
    assert (b["hour_of_day"], b["day_of_week"], b["is_weekend"]) == (17.0, 5.0, 1.0)
    # a naive timestamp is taken as UTC, an offset one is converted
    assert frame(datetime(2026, 10, 5, 9, 30)).equals(frame(MONDAY_9))
    ist = datetime(2026, 10, 5, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert frame(ist).equals(frame(MONDAY_9))


def test_hours_since_last_failure_is_measured_to_as_of():
    history = generate_synthetic_test_history(n_runs=60, seed=2)
    failing = [r for r in history if r.conclusion == "failure"]
    assert failing, "the synthetic fixture contains failures"
    t0 = datetime.fromisoformat(max(r.timestamp for r in history).replace("Z", "+00:00"))
    early, _ = compute_history_stats(history, as_of=t0 + timedelta(hours=1))
    late, _ = compute_history_stats(history, as_of=t0 + timedelta(hours=101))
    job = next(j for j in early if early[j]["hours_since_last_failure"] == early[j]["hours_since_last_failure"])
    assert late[job]["hours_since_last_failure"] - early[job]["hours_since_last_failure"] == 100.0


def _run(as_of):
    out = Path(tempfile.mkdtemp()) / "out.yml"
    plan, explanation = run_pipeline(
        workflow_path=FIXTURES / "sample_workflow.yml", repo_root=FIXTURES / "sample_repo", tracked_files=TRACKED,
        history=generate_synthetic_test_history(n_runs=60, seed=5), changed_files=["src/api/routes.py"], out_path=out, as_of=as_of)
    return plan, explanation, out.read_text(encoding="utf-8")


def test_the_whole_pipeline_is_reproducible_for_a_fixed_as_of():
    p1, e1, y1 = _run(MONDAY_9)
    p2, e2, y2 = _run(MONDAY_9)
    assert [(j.job_id, j.predicted_failure_prob, j.estimated_duration_seconds) for j in p1.jobs] == \
           [(j.job_id, j.predicted_failure_prob, j.estimated_duration_seconds) for j in p2.jobs]
    assert (p1.baseline_wall_clock_seconds, p1.estimated_wall_clock_seconds) == (p2.baseline_wall_clock_seconds, p2.estimated_wall_clock_seconds)
    assert y1 == y2 and e1 == e2
    assert "scored at 2026-10-05 09:30 UTC" in e1
