"""Deferral replay (docs/DEFERRAL_PLAN.md) checked against numbers worked out by hand."""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.replay import deferral_repo, deferral_verdict, render_deferral
from gha_optimizer.server.db import models as m
from gha_optimizer.server.db.session import Base

WORKFLOW = """\
name: CI
on: [push, pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps: [{run: ruff check .}]
  unit:
    needs: lint
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
  docs:
    runs-on: ubuntu-latest
    steps: [{run: make docs}]
"""


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(m.Repository(full_name="o/r", default_branch="main"))
    s.add(m.WorkflowBlob(repo="o/r", blob_sha="b1", path=".github/workflows/ci.yml", content=WORKFLOW))
    gid = itertools.count(1000)
    day = itertools.count(1)

    def run(rid, event, branch, commit, base, jobs):
        s.add(m.WorkflowRun(run_id=rid, repo="o/r", status="completed", jobs_fetched_at=datetime(2026, 10, 1),
                            diff_source="commit", workflow_blob_sha="b1", workflow_path=".github/workflows/ci.yml",
                            event=event, head_branch=branch, commit_sha=commit, diff_base_sha=base,
                            gh_created_at=datetime(2026, 10, next(day))))
        for key, (impacted, conclusion, secs) in jobs.items():
            s.add(m.WorkflowJob(run_id=rid, job_id=key, job_key=key, conclusion=conclusion, duration_seconds=secs,
                                github_job_id=next(gid)))
            s.add(m.RunGraphFeatures(run_id=rid, job_key=key, job_structurally_impacted=impacted,
                                     feature_version=GRAPH_FEATURE_VERSION))

    # base push runs on the default branch (every job unreached; irrelevant for the PR runs' own policy)
    run("P1", "push", "main", "c0", None, {"lint": (0, "success", 100), "unit": (0, "failure", 50), "docs": (0, "success", 20)})
    run("P2", "push", "main", "c1", None, {"lint": (0, "success", 100), "unit": (0, "success", 50), "docs": (0, "success", 20)})

    def pr(rid, base, unit, docs):                         # lint/unit unreached, docs (make) reached
        run(rid, "pull_request", "feature", "x" + rid, base,
            {"lint": (0, "success", 10), "unit": (0, unit, 20), "docs": (1, docs, 5)})

    pr("R1", "c0", "failure", "success")    # unit fails, and it already failed at the base      -> pre-existing
    pr("R2", "c1", "failure", "success")    # unit fails, passed at the base                      -> new
    pr("R3", "zzz", "failure", "success")   # no push run at the base commit                      -> unknown
    pr("R4", "c1", "success", "failure")    # the failing job (docs) is reached, so not deferred  -> not masked
    pr("R5", "c1", "success", "success")    # green
    s.commit()
    return s


def test_deferral_matches_the_hand_computed_figures(db):
    b = deferral_repo(db, "o/r", n_boot=50)["readable"]
    # every PR run executes lint(10)+unit(20)+docs(5)=35 s; lint and unit are deferred (30 s); docs is `make` -> kept
    assert b["pr_runs"] == 5 and b["pr_seconds"] == 175.0 and b["deferred_seconds"] == 150.0
    assert b["total_seconds"] == 515.0                                   # 2 push runs x 170 s + 175 s
    assert b["saving_of_pr_time"] == pytest.approx(150 / 175)
    assert b["saving_of_total_time"] == pytest.approx(150 / 515)
    # failing PR runs R1-R4; masked (all failed jobs deferred) R1-R3
    assert b["failing_pr_runs"] == 4 and b["masked_runs"] == 3
    assert (b["masked_new"], b["masked_unknown_only"], b["masked_pre_existing"]) == (1, 1, 1)
    assert b["hidden_share_worst_case"] == pytest.approx(2 / 4)          # R2 (new) + R3 (unknown)
    assert b["hidden_share_new_only"] == pytest.approx(1 / 4)
    assert b["base_found_runs"] == 4                                     # all but R3
    assert (b["deferred_failed_jobs"], b["deferred_failed_jobs_new"], b["deferred_failed_jobs_unknown"],
            b["deferred_failed_jobs_pre_existing"]) == (3, 1, 1, 1)
    assert b["verdict"].startswith("inconclusive")                       # 4 failing PR runs < 20


def test_plain_dag_safe_policy_also_defers_the_opaque_job_where_it_is_unreached(db):
    # docs is reached in every PR run, so even the policy without the readability guard defers the same two jobs
    b = deferral_repo(db, "o/r", n_boot=20)["dag_safe"]
    assert b["deferred_seconds"] == 150.0 and b["masked_runs"] == 3


def test_deferral_is_deterministic_and_has_intervals(db):
    a, b = deferral_repo(db, "o/r", n_boot=100), deferral_repo(db, "o/r", n_boot=100)
    assert a == b
    lo, hi = a["readable"]["saving_of_total_time_ci95"]
    assert 0.0 <= lo <= hi <= 1.0


def _block(**kw):
    base = {"failing_pr_runs": 30, "saving_of_total_time": 0.10, "hidden_share_worst_case": 0.02}
    return {**base, **kw}


def test_verdict_follows_the_prestated_criteria():
    assert deferral_verdict(_block()).startswith("supported")
    assert deferral_verdict(_block(failing_pr_runs=19)).startswith("inconclusive")
    assert deferral_verdict(_block(saving_of_total_time=0.049)).startswith("not supported: saves under")
    assert deferral_verdict(_block(hidden_share_worst_case=0.05)).startswith("not supported: at least")
    assert "NOT measured" in deferral_verdict(_block())


def test_unknown_repo_and_markdown_rendering(db):
    assert "note" in deferral_repo(db, "nobody/none", n_boot=10)
    text = render_deferral([deferral_repo(db, "o/r", n_boot=30)], 30)
    assert "docs/DEFERRAL_PLAN.md" in text and "pre-existing" in text and "not measured" in text
    assert "o/r" in text and "inconclusive" in text
