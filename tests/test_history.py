"""Loading ingested rows as (run, job-key) records, matrix aggregation, cold-start guard,
dataset construction and report generation — all on rows produced by the real ingestion
service (driven by the scripted transport from test_ingest)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.features.dataset import build_dataset
from gha_optimizer.features.dataset_report import generate_report
from gha_optimizer.ingestion.history import (
    InsufficientHistoryError,
    aggregate_cells,
    load_history,
    require_history,
)
from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
from gha_optimizer.server.db.session import Base
from tests.test_ingest import FakeIngestClient


def cell(conclusion, duration=None, cache_hit=None, outcome=None):
    return SimpleNamespace(conclusion=conclusion, duration_seconds=duration, cache_hit=cache_hit, cache_outcome=outcome)


# --------------------------------------------------------------------------- aggregation

@pytest.mark.parametrize("conclusions, expected", [
    (["success", "failure"], "failure"),
    (["success", "timed_out"], "failure"),
    (["failure", "cancelled"], "failure"),          # fail-fast cancelled the siblings: the job failed
    (["success", "cancelled"], "cancelled"),
    (["skipped", "skipped"], "skipped"),
    (["success", "skipped"], "success"),
    (["success", "success"], "success"),
])
def test_conclusion_aggregation_precedence(conclusions, expected):
    assert aggregate_cells([cell(c) for c in conclusions]).conclusion == expected


def test_duration_is_the_slowest_executed_cell_and_only_for_success_or_failure():
    agg = aggregate_cells([cell("success", 120.0), cell("success", 180.0), cell("skipped", 0.0)])
    assert agg.duration == 180.0
    assert aggregate_cells([cell("cancelled", 50.0)]).duration is None
    assert aggregate_cells([cell("success", None)]).duration is None            # unknown stays unknown


def test_cache_label_aggregation_never_invents_a_label():
    assert aggregate_cells([cell("success", cache_hit=None)]).cache_hit is None
    assert aggregate_cells([cell("success", cache_hit=True), cell("success", cache_hit=False)]).cache_hit is False
    assert aggregate_cells([cell("success", cache_hit=True), cell("success", cache_hit=None)]).cache_hit is True
    out = aggregate_cells([cell("success", cache_hit=True, outcome="exact_hit"), cell("success", cache_hit=True, outcome="partial_hit")])
    assert out.cache_outcome == "partial_hit"                                    # the weaker outcome wins


# --------------------------------------------------------------------------- cold start

def rec_stub(run, job, cache=True):
    from gha_optimizer.ingestion.github_client import WorkflowRunRecord
    return WorkflowRunRecord(run_id=run, commit_sha="x", files_changed=[], job_id=job, duration_seconds=1.0,
                             conclusion="success", cache_hit=cache, author="a", timestamp="2026-01-01T00:00:00Z")


def test_require_history_reasons():
    with pytest.raises(InsufficientHistoryError, match="no ingested history"):
        require_history([], ["a"], 5)
    with pytest.raises(InsufficientHistoryError) as ei:
        require_history([rec_stub("1", "a")], ["a"], 5)
    assert ei.value.details == {"runs": 1, "required": 5}
    with pytest.raises(InsufficientHistoryError) as ei:
        require_history([rec_stub(str(i), "a") for i in range(6)], ["a", "b"], 5)
    assert ei.value.details["jobs"] == ["b"]
    one_class = [rec_stub(str(i), "a", cache=True) for i in range(40)]
    with pytest.raises(InsufficientHistoryError, match="cache-miss model"):
        require_history(one_class, ["a"], 5, need_cache_labels=30)


# --------------------------------------------------------------------------- DB-backed

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    opts = IngestOptions(since=datetime(2026, 10, 1), until=datetime(2026, 10, 3, 23, 59, 59), use_mirror=False, log_workers=1)
    RepoIngestor("o/r", FakeIngestClient(), session, opts).run()
    return session


def test_load_history_returns_one_record_per_run_and_job_key_oldest_first(db):
    hist = load_history(db, "o/r", ".github/workflows/ci.yml")
    assert len(hist) == 6 * 3                                               # 6 runs x {lint, test, build}
    assert [r.timestamp for r in hist] == sorted(r.timestamp for r in hist)
    test_rows = [r for r in hist if r.job_id == "test"]
    assert len(test_rows) == 6 and all(r.duration_seconds == 180.0 for r in test_rows)   # max of the two matrix cells
    assert {r.cache_hit for r in hist if r.job_id == "test"} == {None}      # NULL survives loading
    assert {r.cache_hit for r in hist if r.job_id == "lint"} == {True, False}
    assert all(r.graph_features is None for r in hist)                     # not computed yet: unknown, not invented
    assert load_history(db, "o/r", ".github/workflows/other.yml") == []
    assert len(load_history(db, "o/r", since=datetime(2026, 10, 3))) == 2 * 3


def test_failure_label_comes_from_the_real_conclusion(db):
    hist = load_history(db, "o/r")
    failed = {(r.run_id, r.job_id) for r in hist if r.conclusion == "failure"}
    assert failed == {("1003", "build")}                                    # only run 1003's `build` failed


def test_dataset_has_a_temporal_split_and_null_labels(db):
    df, info = build_dataset(db, "o/r", test_fraction=0.34)
    assert len(df) == 18 and set(df.split) == {"train", "test"}
    last_train = df[df.split == "train"].timestamp.max()
    first_test = df[df.split == "test"].timestamp.min()
    assert last_train < first_test                                          # never random: train is strictly earlier
    assert df[df.split == "test"].run_id.nunique() == info["test_runs"]
    assert df["label_cache_hit"].isna().sum() == 12                         # 6 test + 6 build rows are NULL


def test_dataset_report_is_computed_from_the_database(db):
    text = generate_report(db, ["o/r"])
    assert "# Dataset report" in text and "never random" in text
    assert "| o/r | 18 | 6 | 0 | 3 |" in text                                # rows, runs, processed-not-in-dataset, job keys: computed
    assert "**Failure rate: 5.56%** (1 of 18 labelled rows)" in text           # 1 failure (run 1003 build) / 18 rows
    assert "NULL" in text and "cache_label_status" in text
    assert "Graph-feature quality" in text
