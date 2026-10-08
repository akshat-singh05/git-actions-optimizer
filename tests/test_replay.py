"""Counterfactual replay of the graph-based skip policy, checked against numbers worked out by hand."""
from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.graph.dependency_graph import GRAPH_FEATURE_VERSION
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text
from gha_optimizer.replay import (
    JobOutcome,
    descendants_map,
    opaque_job_keys,
    render_replay,
    replay_repo,
    skipped_jobs,
)
from gha_optimizer.server.db import models as m
from gha_optimizer.server.db.session import Base

WORKFLOW = """\
name: CI
on: push
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


def J(executed=True, failed=False, seconds=10.0, impacted=0.0, opaque=False):
    return JobOutcome(executed=executed, failed=failed, seconds=seconds, impacted=impacted, opaque=opaque)


# ---- pure policy ----------------------------------------------------------------------------------------

def test_descendants_are_transitive():
    d = descendants_map([("a", "b"), ("b", "c"), ("a", "d")])
    assert d["a"] == {"b", "c", "d"} and d["b"] == {"c"} and "c" not in d


def test_naive_policy_skips_only_executed_unreached_jobs():
    jobs = {"reached": J(impacted=1.0), "unreached": J(impacted=0.0), "unknown": J(impacted=None),
            "never_ran": J(executed=False, impacted=0.0)}
    assert skipped_jobs(jobs, {}, dag_safe=False) == {"unreached"}        # unknown and not-run jobs are never skipped


def test_dag_safe_policy_keeps_a_job_whose_dependents_are_reached_or_unknown():
    desc = {"a": {"b", "c"}}
    base = {"a": J(impacted=0.0), "b": J(impacted=0.0), "c": J(impacted=0.0)}
    assert skipped_jobs(base, desc, dag_safe=True) == {"a", "b", "c"}                       # whole cone unreached
    assert "a" not in skipped_jobs({**base, "c": J(impacted=1.0)}, desc, dag_safe=True)     # a reached dependent needs a
    assert "a" not in skipped_jobs({**base, "c": J(impacted=None)}, desc, dag_safe=True)    # an unknown dependent too
    assert "a" in skipped_jobs({**base, "c": J(executed=False, impacted=1.0)}, desc, dag_safe=True)  # not running anyway
    only_a = {"a": J(impacted=0.0), "b": J(impacted=0.0)}                                   # dependent 'c' has no record
    assert "a" not in skipped_jobs(only_a, desc, dag_safe=True)
    assert skipped_jobs(base, None, dag_safe=True) == set()                                 # unknown DAG: nothing provable


def test_readable_policy_never_skips_what_the_graph_cannot_read():
    jobs = {"a": J(), "b": J(opaque=True)}
    assert skipped_jobs(jobs, {}, dag_safe=False, require_readable=True) == {"a"}
    assert skipped_jobs(jobs, {}, dag_safe=False) == {"a", "b"}                       # the opacity guard is the only difference
    desc = {"a": {"b"}}
    assert skipped_jobs(jobs, desc, dag_safe=True) == {"a", "b"}
    assert skipped_jobs(jobs, desc, dag_safe=True, require_readable=True) == set()   # a's dependent b is opaque, so a stays


def test_opaque_jobs_are_make_remote_composite_and_reusable_calls():
    wf = parse_workflow_text("""
name: x
on: push
jobs:
  readable:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
      - run: go test ./...
  uses_make:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi && make test
  remote_action:
    runs-on: ubuntu-latest
    steps:
      - uses: golangci/golangci-lint-action@v6
  composite:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/bootstrap
  called:
    uses: ./.github/workflows/other.yml
  not_make:
    runs-on: ubuntu-latest
    steps:
      - run: echo "cmake is not our make" && ./cmakefile.sh
""")
    assert opaque_job_keys(wf) == {"uses_make", "remote_action", "composite", "called"}


# ---- end to end on a tiny database --------------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False)()
    s.add(m.WorkflowBlob(repo="o/r", blob_sha="b1", path=".github/workflows/ci.yml", content=WORKFLOW))
    gid = itertools.count(1000)

    def run(rid, jobs):
        s.add(m.WorkflowRun(run_id=rid, repo="o/r", status="completed", jobs_fetched_at=datetime(2026, 10, 1),
                            diff_source="commit", workflow_blob_sha="b1", workflow_path=".github/workflows/ci.yml"))
        for key, (impacted, cells) in jobs.items():
            for n, (conclusion, secs) in enumerate(cells):                # one row per matrix cell, distinct job ids
                s.add(m.WorkflowJob(run_id=rid, job_id=f"{key} (cell {n})", job_key=key, conclusion=conclusion,
                                    duration_seconds=secs, github_job_id=next(gid)))
            s.add(m.RunGraphFeatures(run_id=rid, job_key=key, job_structurally_impacted=impacted,
                                     feature_version=GRAPH_FEATURE_VERSION))

    run("A", {"lint": (0, [("success", 100)]), "unit": (0, [("success", 120), ("success", 80)]),   # two cells: 200 s
              "docs": (0, [("success", 20)])})
    run("B", {"lint": (0, [("success", 100)]), "unit": (1, [("failure", 200)]), "docs": (0, [("failure", 20)])})
    run("C", {"lint": (1, [("success", 100)]), "unit": (None, [("skipped", None)]), "docs": (None, [("success", 20)])})
    # E: every job unreached and the only failure is in `docs` - the case a skip policy would turn green
    run("E", {"lint": (0, [("success", 10)]), "unit": (0, [("success", 10)]), "docs": (0, [("failure", 10)])})
    # a feature row from an older definition, and a run with no feature rows at all, must not count
    s.add(m.RunGraphFeatures(run_id="A", job_key="ghost", job_structurally_impacted=0, feature_version=GRAPH_FEATURE_VERSION - 1))
    s.add(m.WorkflowRun(run_id="D", repo="o/r", status="completed", jobs_fetched_at=datetime(2026, 10, 2),
                        diff_source="commit", workflow_blob_sha="b1", workflow_path=".github/workflows/ci.yml"))
    s.add(m.WorkflowJob(run_id="D", job_id="lint", job_key="lint", conclusion="failure", duration_seconds=999.0, github_job_id=1))
    s.commit()
    return s


def test_replay_matches_the_hand_computed_figures(db):
    r = replay_repo(db, "o/r", n_boot=50)
    assert r["runs"] == 4                                               # run D has no current feature rows
    # executed: A 3 jobs (320 s), B 3 jobs (320 s, 2 failures), C 2 jobs (120 s), E 3 jobs (30 s, 1 failure)
    #           -> 11 rows, 790 s, 3 failures; failing runs: B and E
    naive, safe, readable = r["naive"], r["dag_safe"], r["dag_safe_readable"]
    for b in (naive, safe, readable):
        assert (b["executed_rows"], b["executed_failures"], b["executed_seconds"]) == (11, 3, 790.0)
        assert b["failing_runs"] == 2
    # naive: A {lint, unit, docs} 320 s; B {lint, docs} 120 s (docs failed); C none (lint reached, docs unknown);
    #        E {lint, unit, docs} 30 s (docs failed)
    assert (naive["skipped_rows"], naive["skipped_failures"], naive["skipped_seconds"]) == (8, 2, 470.0)
    assert naive["saved_seconds_share"] == pytest.approx(470 / 790)
    assert naive["missed_failure_share"] == pytest.approx(2 / 3)
    assert naive["lift_vs_random"] == pytest.approx((2 / 3) / (8 / 11))
    # dag_safe: A all three (cone unreached); B only docs (lint is needed by the reached `unit`); C none; E all three
    assert (safe["skipped_rows"], safe["skipped_failures"], safe["skipped_seconds"]) == (7, 2, 370.0)
    assert safe["lift_vs_random"] == pytest.approx((2 / 3) / (7 / 11))
    # run level: B keeps a failing job that is NOT skipped (unit), so B still shows red; E's only failure (docs) is
    # skipped, so E would look green
    assert naive["masked_runs"] == 1 and safe["masked_runs"] == 1
    assert safe["masked_run_share"] == pytest.approx(1 / 2)
    # readable: `docs` runs `make docs` (opaque) so it is never skipped; lint / unit stay skippable in A and E, not in B
    assert (readable["skipped_rows"], readable["skipped_failures"], readable["skipped_seconds"]) == (4, 0, 320.0)
    assert readable["masked_runs"] == 0 and readable["masked_run_share"] == pytest.approx(0.0)


def test_replay_is_deterministic_and_reports_intervals_and_top_jobs(db):
    a, b = replay_repo(db, "o/r", n_boot=200), replay_repo(db, "o/r", n_boot=200)
    assert a == b
    lo, hi = a["dag_safe"]["saved_seconds_ci95"]
    assert 0.0 <= lo <= hi <= 1.0
    assert {j["job"] for j in a["dag_safe"]["top_jobs"]} == {"lint", "unit", "docs"}
    assert {j["job"] for j in a["dag_safe_readable"]["top_jobs"]} == {"lint", "unit"}   # docs is opaque


def test_unknown_repo_and_markdown_rendering(db):
    assert "note" in replay_repo(db, "nobody/none", n_boot=10)
    text = render_replay([replay_repo(db, "o/r", n_boot=50)], 50)
    assert "MEASURED on history" in text and "DAG-safe policy" in text and "lift vs random" in text
    assert "failing runs that would look green" in text and "never skips what the graph cannot read" in text
    assert "not a held-out test of the rules" in text
