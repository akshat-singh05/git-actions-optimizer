"""Protocol-v2 features (docs/IMPROVEMENT_PLAN.md): completion-aware visibility, lineage, change content, bounds.

Records are small hand-written test inputs with fully known outcomes; no number here is a result.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import networkx as nx
import pytest

from gha_optimizer.features.feature_extraction import (
    FAILURE_V2_COLUMNS,
    FEATURE_COLUMNS,
    V2_FAILURE_COLUMNS,
    build_inference_frame,
    build_training_frame,
    change_content_features,
    run_context_features,
)
from gha_optimizer.ingestion.github_client import WorkflowRunRecord

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rec(run, start, finish, job="a", conclusion="success", pr=None, branch="feat", author="dev", event="pull_request",
        files=("src/x.py",), attempt=1):
    return WorkflowRunRecord(
        run_id=run, commit_sha="s" + run, files_changed=list(files), job_id=job, duration_seconds=10.0,
        conclusion=conclusion, cache_hit=None, author=author, timestamp=iso(start), branch=branch,
        workflow_path="ci.yml", event=event, pr_number=pr, run_attempt=attempt,
        finished_at=None if finish is None else iso(finish),
    )


def frame(history, **kw):
    df = build_training_frame(nx.DiGraph(), history, **kw)
    return df.set_index("run_id")


def isnan(x):
    return isinstance(x, float) and math.isnan(x)


# ---- completion-aware visibility --------------------------------------------------------------------------

def test_a_run_still_running_is_not_visible_to_a_run_that_starts_before_it_finishes():
    history = [rec("1", 0, 1000, conclusion="failure", pr=7),
               rec("2", 100, 1100, pr=7),           # starts while run 1 is still running
               rec("3", 2000, 2100, pr=7)]          # starts after both finished
    df = frame(history)
    assert isnan(df.loc["2", "historical_failure_rate"]) and isnan(df.loc["2", "lineage_prev_job_failed"])
    assert df.loc["3", "lineage_prev_job_failed"] == 0.0           # run 2 (success) is the latest visible outcome
    assert df.loc["3", "lineage_job_fail_count"] == 1.0            # run 1's failure became visible once it finished
    # protocol v1 (every earlier-started run is visible) is still available and does leak run 1's outcome into run 2
    v1 = frame(history, completion_aware=False)
    assert v1.loc["2", "lineage_prev_job_failed"] == 1.0 and v1.loc["2", "historical_failure_rate"] == 1.0


def test_unknown_finish_time_falls_back_to_visible_after_start():
    df = frame([rec("1", 0, None, conclusion="failure", pr=3), rec("2", 50, None, pr=3)])
    assert df.loc["2", "lineage_prev_job_failed"] == 1.0


def test_hours_since_last_failure_counts_from_when_the_failure_became_known():
    df = frame([rec("1", 0, 600, conclusion="failure"), rec("2", 600 + 3600, 4300)])
    assert df.loc["2", "hours_since_last_failure"] == pytest.approx(1.0)
    assert isnan(df.loc["1", "hours_since_last_failure"])


# ---- lineage -----------------------------------------------------------------------------------------------

def test_lineage_is_the_pull_request_and_other_prs_do_not_share_it():
    history = [rec("1", 0, 10, conclusion="failure", pr=7), rec("2", 100, 110, pr=8), rec("3", 200, 210, pr=7)]
    df = frame(history)
    assert df.loc["3", "lineage_prev_job_failed"] == 1.0 and df.loc["3", "lineage_prev_run_any_failure"] == 1.0
    assert isnan(df.loc["2", "lineage_prev_job_failed"]) and df.loc["2", "lineage_runs_so_far"] == 0.0


def test_without_a_pr_number_the_lineage_is_branch_plus_author_and_pushes_use_the_branch():
    history = [rec("1", 0, 10, conclusion="failure", branch="patch-1", author="alice"),
               rec("2", 100, 110, branch="patch-1", author="bob"),      # same branch name, other author
               rec("3", 200, 210, branch="patch-1", author="alice"),
               rec("4", 300, 310, event="push", branch="main", author="x", conclusion="failure"),
               rec("5", 400, 410, event="push", branch="main", author="y")]
    df = frame(history)
    assert isnan(df.loc["2", "lineage_prev_job_failed"])
    assert df.loc["3", "lineage_prev_job_failed"] == 1.0
    assert df.loc["5", "lineage_prev_job_failed"] == 1.0                    # pushes: previous run on the branch


def test_lineage_features_are_bounded_so_they_cannot_act_as_a_clock():
    history = [rec(str(i), i * 100, i * 100 + 10, conclusion="failure" if i % 2 else "success", event="push", branch="main")
               for i in range(60)]
    df = frame(history)
    assert df["lineage_runs_so_far"].max() <= 20 and df["lineage_job_fail_count"].max() <= 5


def test_features_never_use_the_outcome_of_the_row_itself_or_its_siblings():
    history = [rec("1", 0, 10, job="a", conclusion="failure", pr=1), rec("1", 0, 10, job="b", conclusion="failure", pr=1)]
    df = build_training_frame(nx.DiGraph(), history)
    assert df["lineage_prev_job_failed"].isna().all() and df["lineage_prev_run_any_failure"].isna().all()


# ---- change content and run context ------------------------------------------------------------------------

def test_change_content_features():
    f = change_content_features(["tests/unit/test_a.py", "requirements-dev.txt", ".github/workflows/ci.yml", "src/a.py"])
    assert f == {"touches_tests": 1.0, "touches_dependencies": 1.0, "touches_ci_config": 1.0, "docs_only": 0.0,
                 "n_top_level_dirs": 4.0}
    docs = change_content_features(["docs/index.md", "README.md"])
    assert docs["docs_only"] == 1.0 and docs["touches_tests"] == 0.0
    assert change_content_features(["src/app_test.go"])["touches_tests"] == 1.0
    assert change_content_features(["src/contest.py"])["touches_tests"] == 0.0         # 'test' inside a word is not a test file
    assert change_content_features([])["docs_only"] == 0.0


def test_run_context_features():
    bot = run_context_features(rec("1", 0, 1, author="dependabot[bot]", attempt=2))
    assert bot == {"event_pull_request": 1.0, "actor_is_bot": 1.0, "run_attempt": 2.0}
    human = run_context_features(rec("1", 0, 1, author="alice", event="push"))
    assert human["event_pull_request"] == 0.0 and human["actor_is_bot"] == 0.0
    unknown = run_context_features(WorkflowRunRecord(run_id="1", commit_sha="x", files_changed=[], job_id="a",
                                                     duration_seconds=1.0, conclusion="success", cache_hit=None,
                                                     author=None, timestamp=iso(0)))
    assert isnan(unknown["event_pull_request"]) and isnan(unknown["actor_is_bot"]) and isnan(unknown["run_attempt"])


# ---- inference frame ---------------------------------------------------------------------------------------

def test_inference_frame_has_every_v2_column_and_leaves_unknown_context_as_nan():
    df = build_inference_frame(nx.DiGraph(), ["a"], ["tests/test_x.py"], {"a": {"failure_rate": 0.2, "job_prior_runs": 40.0}})
    assert set(FAILURE_V2_COLUMNS) <= set(df.columns)
    row = df.iloc[0]
    assert row["touches_tests"] == 1.0 and row["job_prior_runs"] == 40.0
    for unknown in ("event_pull_request", "lineage_prev_job_failed", "lineage_runs_so_far", "hours_since_last_failure"):
        assert isnan(row[unknown])
    live = build_inference_frame(nx.DiGraph(), ["a"], [], {}, context={"event_pull_request": 1.0, "lineage_runs_so_far": 3.0},
                                 lineage_stats={"a": {"lineage_prev_job_failed": 1.0}})
    assert live.iloc[0]["event_pull_request"] == 1.0 and live.iloc[0]["lineage_prev_job_failed"] == 1.0


def test_v2_column_list_has_no_duplicates_and_extends_the_old_contract():
    assert len(set(FAILURE_V2_COLUMNS)) == len(FAILURE_V2_COLUMNS)
    assert len(V2_FAILURE_COLUMNS) == 15 and FAILURE_V2_COLUMNS[:len(FEATURE_COLUMNS)] == FEATURE_COLUMNS
