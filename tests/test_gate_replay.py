"""Gate-relaxation replay checked against hand-computed numbers."""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.gate_replay import gate_replay_repo, render_gate_replay
from gha_optimizer.server.db import models as m
from gha_optimizer.server.db.session import Base

WORKFLOW = """\
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [{run: ruff check .}]
  test:
    needs: lint
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
  typecheck:
    needs: lint
    runs-on: ubuntu-latest
    steps: [{run: mypy .}]
"""


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(m.WorkflowBlob(repo="o/r", blob_sha="b1", path="ci.yml", content=WORKFLOW))
    gid = itertools.count(1)

    def run(i, jobs):
        rid = f"r{i:03d}"
        s.add(m.WorkflowRun(run_id=rid, repo="o/r", status="completed", jobs_fetched_at=datetime(2026, 10, 1),
                            workflow_path="ci.yml", workflow_blob_sha="b1", timestamp=f"2026-10-01T10:{i:02d}:00Z"))
        for key, (conclusion, secs) in jobs.items():
            s.add(m.WorkflowJob(run_id=rid, job_id=key, job_key=key, conclusion=conclusion, duration_seconds=secs,
                                github_job_id=next(gid)))

    ok = {"lint": ("success", 30.0), "test": ("success", 100.0), "typecheck": ("success", 40.0)}
    for i in range(25):
        # 25 runs: the first 10 are the selection window; of the 15 scored runs, runs 12, 17 and 22 fail at lint
        if i in (12, 17, 22):
            run(i, {"lint": ("failure", 30.0), "test": ("skipped", None), "typecheck": ("skipped", None)})
        else:
            run(i, ok)
    s.commit()
    return s


def test_replay_matches_the_hand_computed_figures(db):
    r = gate_replay_repo(db, "o/r")
    assert r["selection_runs"] == 10 and r["scored_runs"] == 15
    assert (r["needs_edges_in_latest_workflow"], r["edges_relaxed_in_latest_workflow"]) == (2, 1)   # only lint -> test helps
    # all-success scored runs: 12; critical path 30 + 100 = 130 -> 100 once `test` stops waiting for `lint` (typecheck 30+40 < 100)
    assert r["all_success_runs"] == 12 and r["runs_with_saving"] == 12
    assert r["median_saving_s"] == 30.0 and r["mean_saving_s"] == 30.0 and r["p90_saving_s"] == 30.0
    assert r["median_saving_pct"] == pytest.approx(100 * 30 / 130)
    # the 3 failing runs: `test` (median 100 s) would now have run behind the failed lint job
    assert r["failing_runs"] == 3 and r["failing_runs_with_extra_work"] == 3
    assert r["extra_billable_hours_estimated"] == pytest.approx(300 / 3600)
    assert r["total_billable_hours_scored"] == pytest.approx((12 * 170 + 3 * 30) / 3600)
    assert r["extra_billable_pct_estimated"] == pytest.approx(100 * 300 / (12 * 170 + 3 * 30))


def test_too_little_history_and_rendering(db):
    assert "note" in gate_replay_repo(db, "nobody/none")
    text = render_gate_replay([gate_replay_repo(db, "o/r"), {"repo": "x/y", "runs": 3, "note": "fewer than 20 usable runs"}])
    assert "MEASURED on history" in text and "ESTIMATE" in text and "o/r" in text and "1 of 2" in text
