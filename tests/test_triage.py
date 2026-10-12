"""Failure triage checked against hand-counted scenarios on a tiny database."""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.server.db import models as m
from gha_optimizer.server.db.session import Base
from gha_optimizer.triage import (
    CLASSES,
    job_failure_profile,
    render_profile,
    render_triage,
    step_bucket,
    triage_repo,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(m.Repository(full_name="o/r", default_branch="main"))
    gid, minute = itertools.count(100), itertools.count(1)

    def run(rid, commit, jobs, pr=None, event="pull_request", branch="feat", base=None, impacted=None):
        """jobs: {job_key: (conclusion, failing step name or None)}"""
        s.add(m.WorkflowRun(run_id=rid, repo="o/r", status="completed", jobs_fetched_at=datetime(2026, 10, 1),
                            workflow_path="ci.yml", event=event, head_branch=branch, commit_sha=commit, pr_number=pr,
                            diff_base_sha=base, actor_login="dev", timestamp=f"2026-10-01T10:{next(minute):02d}:00Z"))
        for key, (conclusion, step) in jobs.items():
            g = next(gid)
            s.add(m.WorkflowJob(run_id=rid, job_id=key, job_key=key, conclusion=conclusion, duration_seconds=60.0,
                                github_job_id=g))
            if step:
                s.add(m.WorkflowStep(github_job_id=g, number=1, name=step, conclusion="failure"))
            if impacted is not None and key in impacted:
                s.add(m.RunGraphFeatures(run_id=rid, job_key=key, job_structurally_impacted=impacted[key],
                                         feature_version=GRAPH_FEATURE_VERSION))

    F, OK = "failure", "success"
    # P1: the push run on main at commit b1 where `test` already fails (the base of R5)
    run("P1", "b1", {"test": (F, "Run pytest")}, event="push", branch="main")
    run("R1", "c1", {"test": (F, "Run pytest")}, pr=1)                       # fixed by the next commit of the PR
    run("R2", "c2", {"test": (OK, None)}, pr=1)
    run("R3", "c3", {"test": (F, "Run pytest")}, pr=2)                       # passes on a re-trigger of the SAME commit
    run("R4", "c3", {"test": (OK, None)}, pr=2)
    run("R5", "c5", {"test": (F, "Run pytest")}, pr=3, base="b1")            # already failing at the base commit
    run("R6", "c6", {"test": (F, "Run pytest"), "status": (F, "Run false")}, pr=4)   # status is derived
    run("R7", "c7", {"test": (F, "Run pytest")}, pr=5)                       # fails twice in a row
    run("R8", "c8", {"test": (F, "Run pytest")}, pr=5)
    run("R9", "c9", {"test": (F, "Run pytest")}, pr=6, impacted={"test": 0.0})   # the graph says the change cannot reach it
    s.commit()
    return s


def test_every_failed_job_gets_the_class_its_evidence_supports(db):
    r = triage_repo(db, "o/r")
    assert r["runs"] == 10
    assert r["classes"] == {"derived_aggregator": 1, "flaky_same_commit": 1, "pre_existing": 1, "fixed_by_later_commit": 1,
                            "persistent": 2, "unrelated_to_change": 1, "unclassified": 2}
    assert r["failed_jobs"] == 9 and r["independent_failed_jobs"] == 8
    assert set(r["classes"]) == set(CLASSES)


def test_red_runs_needing_no_code_fix_ignore_derived_aggregator_failures(db):
    r = triage_repo(db, "o/r")
    # red runs: P1 R1 R3 R5 R6 R7 R8 R9 = 8; no fix needed: R3 (flaky), R5 (pre-existing), R9 (unrelated) = 3
    assert r["failing_runs"] == 8 and r["non_actionable_runs"] == 3 and r["non_actionable_failures"] == 3


def test_failing_steps_buckets_and_billable_time(db):
    r = triage_repo(db, "o/r")
    assert r["buckets"] == {"test": 8, "other": 1}                            # 'Run false' is bucketed as other
    assert r["top_failing_steps"][0] == {"job": "test", "step": "Run pytest", "count": 8}
    assert r["billable_seconds_by_class"]["persistent"] == 120.0 and r["billable_seconds_by_class"]["flaky_same_commit"] == 60.0


def test_step_buckets_by_name():
    cases = {"Set up job": "runner_infra", "Install dependencies": "setup_install", "Run ruff check": "lint_typecheck",
             "Run mypy": "lint_typecheck", "Run pytest -v": "test", "Build wheel": "build", "Do something odd": "other",
             None: "unknown"}
    for name, bucket in cases.items():
        assert step_bucket(name) == bucket, name


def test_report_and_per_job_profile(db):
    r = triage_repo(db, "o/r")
    text = render_triage([r, {"repo": "nobody/none", "runs": 0, "note": "no ingested runs with jobs"}])
    assert "o/r" in text and "derived_aggregator" in text and "under-detected" in text and "Run pytest" in text
    profile = job_failure_profile(r, min_failures=3)
    assert profile["test"]["failures"] == 8 and profile["test"]["share_non_actionable"] == pytest.approx(3 / 8)
    assert "`test`: 8 failures" in render_profile(profile)
    assert "No job has enough" in render_profile({})


def test_unknown_repository_has_a_note(db):
    assert "note" in triage_repo(db, "nobody/none")
