"""Leakage tests for the training-frame builder.

The records below are small, hand-written *test inputs* with fully known outcomes
(not a stand-in for data): they let us assert the exact point-in-time contract —
every historical feature of a row is a function of runs strictly before that run.
"""
from __future__ import annotations

import copy
import math
import random
from datetime import datetime, timedelta

import networkx as nx
import pandas as pd
import pytest

from gha_optimizer.features.feature_extraction import (
    FEATURE_COLUMNS,
    HistoryState,
    _exponential_decay_mean,
    _streak_count,
    build_inference_frame,
    build_training_frame,
    compute_history_stats,
)
from gha_optimizer.ingestion.github_client import WorkflowRunRecord

HIST_FEATURES = [
    "historical_failure_rate", "historical_mean_duration", "recent_failure_streak",
    "recent_success_streak", "author_failure_rate", "cache_key_stability",
]
BASE = datetime(2026, 9, 1, 8, 0, 0)
JOBS = ["lint", "test", "build"]
AUTHORS = ["ann", "bob", "cy"]


def make_history(n_runs: int = 14, seed: int = 5) -> list[WorkflowRunRecord]:
    rng = random.Random(seed)
    recs = []
    for i in range(n_runs):
        ts = (BASE + timedelta(hours=7 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for job in JOBS:
            recs.append(WorkflowRunRecord(
                run_id=str(1000 + i), commit_sha=f"{i:040d}", files_changed=["a.py"] * (1 + i % 3),
                job_id=job, duration_seconds=float(rng.randint(20, 300)),
                conclusion=rng.choice(["success", "success", "success", "failure", "cancelled"]),
                cache_hit=rng.choice([True, False, None]), author=AUTHORS[i % 3], timestamp=ts,
                workflow_path=".github/workflows/ci.yml", event="push", loc_added=i, loc_deleted=1,
            ))
    return recs


EMPTY_GRAPH = nx.DiGraph()


def frame(history):
    return build_training_frame(EMPTY_GRAPH, history)


def row(df, run_id, job):
    return df[(df.run_id == str(run_id)) & (df.job_id == job)].iloc[0]


def same(a, b):
    return (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b)) or a == b


def perturb(history, from_run_index):
    """Rewrite outcomes (label-bearing fields) of run ``from_run_index`` and every later run."""
    out = copy.deepcopy(history)
    cutoff = str(1000 + from_run_index)
    for r in out:
        if r.run_id >= cutoff:
            r.conclusion = "failure" if r.conclusion != "failure" else "success"
            r.duration_seconds = (r.duration_seconds or 0) + 9999.0
            r.cache_hit = (not r.cache_hit) if r.cache_hit is not None else True
    return out


@pytest.mark.parametrize("k", [0, 1, 5, 9, 13])
def test_features_of_run_k_do_not_depend_on_run_k_or_later_outcomes(k):
    history = make_history()
    before, after = frame(history), frame(perturb(history, k))
    target = before[before.run_id == str(1000 + k)]
    target_after = after[after.run_id == str(1000 + k)]
    assert len(target) == len(JOBS)
    for col in FEATURE_COLUMNS:
        for a, b in zip(target[col].tolist(), target_after[col].tolist()):
            assert same(a, b), f"{col} of run {1000 + k} changed when its own/future outcomes changed"
    # and earlier runs are completely untouched
    earlier = before[before.run_id < str(1000 + k)]
    earlier_after = after[after.run_id < str(1000 + k)]
    pd.testing.assert_frame_equal(earlier[FEATURE_COLUMNS].reset_index(drop=True),
                                  earlier_after[FEATURE_COLUMNS].reset_index(drop=True))


def test_historical_features_equal_a_brute_force_recomputation_from_earlier_runs_only():
    history = make_history()
    df = frame(history)
    by_run: dict[str, list[WorkflowRunRecord]] = {}
    for r in sorted(history, key=lambda x: x.job_id):  # within a run, job-id order (the documented tie-break)
        by_run.setdefault(r.run_id, []).append(r)
    run_ids = sorted(by_run)
    for idx, rid in enumerate(run_ids):
        prior = [r for p in run_ids[:idx] for r in by_run[p]]
        for rec in by_run[rid]:
            mine = [p for p in prior if p.job_id == rec.job_id]
            fails = [1 if p.conclusion == "failure" else 0 for p in mine if p.conclusion in ("success", "failure")]
            durs = [p.duration_seconds for p in mine if p.conclusion in ("success", "failure")]
            caches = [int(p.cache_hit) for p in mine if p.cache_hit is not None]
            auth = [1 if p.conclusion == "failure" else 0 for p in prior
                    if p.author == rec.author and p.conclusion in ("success", "failure")]
            got = row(df, rid, rec.job_id)
            exp = {
                "historical_failure_rate": _exponential_decay_mean([float(x) for x in fails]) if fails else math.nan,
                "historical_mean_duration": _exponential_decay_mean(durs) if durs else math.nan,
                "recent_failure_streak": float(_streak_count(fails, 1)) if fails else math.nan,
                "recent_success_streak": float(_streak_count(fails, 0)) if fails else math.nan,
                "author_failure_rate": _exponential_decay_mean([float(x) for x in auth]) if auth else math.nan,
                "cache_key_stability": _exponential_decay_mean([float(x) for x in caches]) if caches else math.nan,
            }
            for col, want in exp.items():
                assert same(float(got[col]), float(want)) or abs(float(got[col]) - float(want)) < 1e-9, (rid, rec.job_id, col)


def test_jobs_of_the_same_run_do_not_see_each_other():
    history = make_history()
    df = frame(history)
    # same author, same run: author_failure_rate must be identical for all jobs (it is built from prior runs only)
    for rid in range(1000, 1014):
        vals = df[df.run_id == str(rid)]["author_failure_rate"].tolist()
        assert all(same(vals[0], v) for v in vals)


def test_first_run_has_no_history_and_gets_nan_not_a_default():
    df = frame(make_history())
    first = df[df.run_id == "1000"]
    for col in HIST_FEATURES:
        assert first[col].isna().all(), f"{col} must be NaN when there is no prior run"


def test_input_order_does_not_matter():
    history = make_history()
    shuffled = history[:]
    random.Random(1).shuffle(shuffled)
    a = frame(history).sort_values(["run_id", "job_id"]).reset_index(drop=True)
    b = frame(shuffled).sort_values(["run_id", "job_id"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a[FEATURE_COLUMNS], b[FEATURE_COLUMNS])


def test_label_semantics_null_is_not_zero():
    history = make_history()
    df = frame(history)
    for r in history:
        got = row(df, r.run_id, r.job_id)
        if r.conclusion == "failure":
            assert got.label_failed == 1.0
        elif r.conclusion == "success":
            assert got.label_failed == 0.0
        else:
            assert math.isnan(got.label_failed)             # cancelled: no failure label
        if r.cache_hit is None:
            assert math.isnan(got.label_cache_hit)          # NULL cache label stays NULL
        else:
            assert got.label_cache_hit == float(r.cache_hit)
        if r.conclusion in ("success", "failure"):
            assert got.label_duration == r.duration_seconds
        else:
            assert math.isnan(got.label_duration)           # no duration label for cancelled/skipped


def test_history_state_updates_only_after_the_run():
    state = HistoryState()
    h = make_history(3)
    run0 = [r for r in h if r.run_id == "1000"]
    assert math.isnan(state.job_features(run0[0].workflow_path, "lint")["historical_failure_rate"])
    state.update_run(run0)
    assert not math.isnan(state.job_features(run0[0].workflow_path, "lint")["historical_failure_rate"]) or \
        run0[0].conclusion not in ("success", "failure")


def test_inference_stats_cover_the_full_history_and_unknown_jobs_are_nan():
    history = make_history()
    job_stats, author_stats = compute_history_stats(history)
    assert set(job_stats) == set(JOBS)
    df = build_inference_frame(EMPTY_GRAPH, JOBS + ["brand-new-job"], ["a.py"], job_stats, author="ann", author_stats=author_stats)
    new = df[df.job_id == "brand-new-job"].iloc[0]
    for col in ("historical_failure_rate", "historical_mean_duration", "recent_failure_streak", "cache_key_stability"):
        assert math.isnan(new[col])                          # cold-start job: unknown, not "0.1" / "60s"
    assert not math.isnan(df[df.job_id == "lint"].iloc[0]["historical_mean_duration"])


def test_inference_frame_without_author_or_loc_is_nan_not_default():
    df = build_inference_frame(EMPTY_GRAPH, ["lint"], ["a.py"], {})
    r = df.iloc[0]
    assert math.isnan(r.author_failure_rate) and math.isnan(r.loc_added) and math.isnan(r.loc_deleted)
