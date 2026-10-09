"""Ingestion service: idempotency, resumability, NULL-label semantics, log ledger.

The GitHub transport is replaced by ``FakeIngestClient`` (a scripted stand-in for
the API); everything downstream — mapping, persistence, ledger, resume logic —
is the production code running on an in-memory SQLite database.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.ingestion.github_client import GithubAPIError, LogUnavailable, RunWindow
from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor, ingestion_status
from gha_optimizer.server.db import models as m
from gha_optimizer.server.db.session import Base

WORKFLOW_YAML = """\
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: pip-${{ hashFiles('requirements.txt') }}
  test:
    needs: lint
    strategy:
      matrix:
        os: [ubuntu, windows]
    steps:
      - uses: actions/setup-python@v5
  build:
    needs: test
    steps:
      - run: make
"""

HIT_LOG = [
    "2026-10-01T10:00:01.0000000Z ##[group]Run actions/cache@v4",
    "2026-10-01T10:00:01.0000001Z   key: pip-abc",
    "2026-10-01T10:00:01.0000002Z ##[endgroup]",
    "2026-10-01T10:00:02.0000000Z Cache restored from key: pip-abc",
]
MISS_LOG = [
    "2026-10-01T10:00:01.0000000Z ##[group]Run actions/cache@v4",
    "2026-10-01T10:00:01.0000001Z   key: pip-abc",
    "2026-10-01T10:00:01.0000002Z ##[endgroup]",
    "2026-10-01T10:00:02.0000000Z Cache not found for input keys: pip-abc",
]
NO_CACHE_LOG = ["2026-10-01T10:00:01.0000000Z ##[group]Run pytest", "2026-10-01T10:00:09.0000000Z 12 passed"]


class _Stream:
    def __init__(self, lines):
        self._lines = lines
        self.bytes = sum(len(x) + 1 for x in lines)

    def __iter__(self):
        return iter(self._lines)


def _ts(base: datetime, secs: int) -> str:
    return (base + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeIngestClient:
    """Scripted API: 6 completed runs (2/day, Oct 1-3) of one workflow with 4 jobs each."""

    def __init__(self, expired_before: datetime | None = None, crash_on_jobs_call: int | None = None,
                 fork_prs: bool = False, pr_lookup_finds: bool = True, head_shas: dict[int, str] | None = None):
        self.stats = Counter()
        self.calls = Counter()
        self.fork_prs = fork_prs                 # GitHub leaves pull_requests[] empty for fork PRs
        self.pr_lookup_finds = pr_lookup_finds
        self.expired_before = expired_before
        self.crash_on_jobs_call = crash_on_jobs_call
        self.attempt_override: dict[int, int] = {}
        self.runs = []
        fork_prs = self.fork_prs
        for i in range(6):
            created = datetime(2026, 10, 1 + i // 2, 8 + 6 * (i % 2), 0, 0)
            self.runs.append({
                "id": 1000 + i, "head_sha": (head_shas or {}).get(i, f"sha{i:040d}"[-40:]), "conclusion": "success" if i != 3 else "failure",
                "created_at": _ts(created, 0), "run_started_at": _ts(created, 1), "updated_at": _ts(created, 600),
                "workflow_id": 11, "name": "CI", "event": "pull_request" if i % 2 else "push", "status": "completed",
                "head_branch": "main", "run_number": 50 + i, "run_attempt": 1,
                "actor": {"login": f"dev{i % 3}"}, "triggering_actor": {"login": f"dev{i % 3}"},
                "pull_requests": [{"number": 7, "base": {"sha": "b" * 40}}] if (i % 2 and not fork_prs) else [],
            })

    # --- API surface used by RepoIngestor -------------------------------
    def rate_limit_status(self):
        return {"limit": 5000, "remaining": 5000, "reset": 0}

    def get_repo(self):
        return {"default_branch": "main", "language": "Python", "license": {"spdx_id": "MIT"}}

    def list_workflows(self):
        return [{"id": 11, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]

    def collect_runs(self, lo, hi, workflow_id=None, **_):
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        hits = [r for r in self.runs if lo <= datetime.strptime(r["created_at"], fmt) <= hi]
        out = []
        for r in hits:
            r = dict(r)
            r["run_attempt"] = self.attempt_override.get(r["id"], 1)
            out.append(r)
        return RunWindow(lo, hi, out, len(hits), True)

    def get_run(self, run_id):
        return next(r for r in self.runs if r["id"] == int(run_id))

    def get_file(self, path, ref):
        self.calls["get_file"] += 1
        return "blobsha" + "0" * 33, WORKFLOW_YAML

    def list_jobs(self, run_id):
        self.calls["list_jobs"] += 1
        if self.crash_on_jobs_call is not None and self.calls["list_jobs"] == self.crash_on_jobs_call:
            raise GithubAPIError(0, "https://api.github.com/x", "ConnectionError: network down")
        run = self.get_run(run_id)
        attempt = self.attempt_override.get(int(run_id), 1)
        base = datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        spec = [("lint", 60, "success"), ("test (ubuntu)", 120, "success"), ("test (windows)", 180, "success"),
                ("build", 30, run["conclusion"])]
        jobs = []
        for n, (name, dur, concl) in enumerate(spec):
            jid = int(run_id) * 100 + n + (50 if attempt > 1 else 0)
            jobs.append({
                "id": jid, "name": name, "status": "completed", "conclusion": concl,
                "created_at": _ts(base, 2), "started_at": _ts(base, 12), "completed_at": _ts(base, 12 + dur),
                "runner_name": "GitHub Actions 1", "runner_group_name": "GitHub Actions", "labels": ["ubuntu-latest"],
                "run_attempt": attempt,
                "steps": [{"number": 1, "name": "Set up job", "status": "completed", "conclusion": "success",
                           "started_at": _ts(base, 12), "completed_at": _ts(base, 14)}],
            })
        return jobs

    def pulls_for_commit(self, sha):
        self.calls["pulls_for_commit"] += 1
        if not self.pr_lookup_finds:
            return []
        return [{"number": 7, "state": "open", "base": {"sha": "b" * 40, "repo": {"full_name": "o/r"}}}]

    def get_commit(self, sha):
        self.calls["get_commit"] += 1
        return {"files": [{"filename": "src/a.py", "status": "modified", "additions": 3, "deletions": 1}],
                "parents": [{"sha": "p" * 40}]}

    def compare(self, base, head):
        self.calls["compare"] += 1
        return {"files": [{"filename": "src/a.py", "status": "modified", "additions": 5, "deletions": 2},
                          {"filename": "requirements.txt", "status": "modified", "additions": 1, "deletions": 1}]}

    def open_job_log(self, job_id):
        self.calls["open_job_log"] += 1
        run_id = job_id // 100
        run = self.get_run(run_id)
        created = datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        if self.expired_before and created < self.expired_before:
            raise LogUnavailable(410)
        slot = job_id % 100 % 50
        if slot == 0:                       # lint: alternate hit / miss by run
            return _Stream(HIT_LOG if run_id % 2 == 0 else MISS_LOG)
        return _Stream(NO_CACHE_LOG)        # test cells: cache-capable but no cache line


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def make(session_factory, client=None, **opts):
    client = client or FakeIngestClient()
    opts.setdefault("log_workers", 1)  # deterministic call counts in tests
    options = IngestOptions(
        since=datetime(2026, 10, 1), until=datetime(2026, 10, 3, 23, 59, 59), use_mirror=False, **opts
    )
    session = session_factory()
    return RepoIngestor("o/r", client, session, options), client, session


def snapshot(session):
    return {
        "runs": session.query(m.WorkflowRun).count(),
        "jobs": session.query(m.WorkflowJob).count(),
        "steps": session.query(m.WorkflowStep).count(),
        "files": session.query(m.RunChangedFile).count(),
        "events": session.query(m.JobCacheEvent).count(),
        "ledger": session.query(m.JobLogFetch).count(),
        "done": session.query(m.IngestionProgress).filter_by(stage="run_done").count(),
    }


def test_full_ingest_persists_real_fields_and_labels(session_factory):
    ing, client, s = make(session_factory)
    ing.run()
    snap = snapshot(s)
    assert snap["runs"] == 6 and snap["jobs"] == 24 and snap["done"] == 6
    # API job names were mapped to YAML keys via the workflow blob at the head sha
    keys = {k for (k,) in s.query(m.WorkflowJob.job_key).all()}
    assert keys == {"lint", "test", "build"}
    cells = s.query(m.WorkflowJob).filter(m.WorkflowJob.job_name.like("test (%")).all()
    assert len(cells) == 12 and {c.job_key for c in cells} == {"test"}      # matrix cells -> one YAML key
    # duration is derived from the API timestamps, queue time too
    lint = s.query(m.WorkflowJob).filter_by(job_key="lint").first()
    assert lint.duration_seconds == 60.0 and lint.queue_seconds == 10.0
    # diffs: PR runs use the compare endpoint, push runs the commit endpoint
    sources = dict(s.query(m.WorkflowRun.diff_source, func.count()).group_by(m.WorkflowRun.diff_source).all())
    assert sources == {"commit": 3, "compare_pr": 3}
    pr_run = s.query(m.WorkflowRun).filter_by(diff_source="compare_pr").first()
    assert (pr_run.loc_added, pr_run.loc_deleted) == (6, 3)
    assert pr_run.pr_number == 7 and pr_run.workflow_blob_sha.startswith("blobsha")


def test_cache_labels_are_null_unless_a_log_proves_them(session_factory):
    ing, client, s = make(session_factory)
    ing.run()
    jobs = s.query(m.WorkflowJob).all()
    by_key = {}
    for j in jobs:
        by_key.setdefault(j.job_key, []).append(j)
    assert {j.cache_hit for j in by_key["lint"]} == {True, False}        # real hit/miss from logs
    assert all(j.cache_hit is None for j in by_key["test"])              # log had no cache line -> NULL
    assert {j.cache_label_status for j in by_key["test"]} == {"parsed_no_events"}
    assert all(j.cache_hit is None for j in by_key["build"])             # not cache-capable -> NULL
    assert {j.cache_label_status for j in by_key["build"]} == {"skipped_not_cache_capable"}
    # NULL is never coerced to False
    assert s.query(m.WorkflowJob).filter(m.WorkflowJob.cache_hit.is_(False)).count() == 3
    assert s.query(m.WorkflowJob).filter(m.WorkflowJob.cache_hit.is_(None)).count() == 24 - 6


def test_rerunning_ingestion_is_idempotent_and_makes_no_new_requests(session_factory):
    ing, client, s = make(session_factory)
    ing.run()
    before = snapshot(s)
    calls_before = Counter(client.calls)
    ing2, _, _ = make(session_factory, client)
    ing2.db = s
    ing2.run()
    assert snapshot(s) == before
    after = Counter(client.calls)
    setup_reads = after.pop("get_file", 0) - calls_before.pop("get_file", 0)
    assert setup_reads <= 1                                              # the one setup read of the HEAD workflow
    assert after == calls_before                                         # no job/diff/log re-fetched


def test_log_is_never_downloaded_twice_even_if_run_progress_is_lost(session_factory):
    ing, client, s = make(session_factory)
    ing.run()
    downloads = client.calls["open_job_log"]
    s.query(m.IngestionProgress).filter_by(stage="run_done").delete()
    s.commit()
    ing2, _, _ = make(session_factory, client)
    ing2.db = s
    ing2.run()
    assert client.calls["open_job_log"] == downloads                     # the ledger prevented re-download
    assert snapshot(s)["done"] == 6


def test_interrupted_ingestion_resumes_without_duplicates(session_factory):
    # reference: uninterrupted
    ref_ing, _, ref_s = make(session_factory)
    ref_ing.run()
    reference = snapshot(ref_s)

    sf2 = sessionmaker(bind=create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}))
    Base.metadata.create_all(sf2.kw["bind"])
    crashing = FakeIngestClient(crash_on_jobs_call=3)
    ing, _, s = make(sf2, crashing)
    with pytest.raises(GithubAPIError):
        ing.run()
    partial = snapshot(s)
    assert 0 < partial["done"] < 6                                       # real progress was persisted

    resumed_client = FakeIngestClient()
    ing2, _, _ = make(sf2, resumed_client)
    ing2.db = s
    ing2.run()
    assert snapshot(s) == reference                                      # no duplicates, nothing lost
    assert resumed_client.calls["list_jobs"] == 6 - partial["done"]      # only the unfinished runs were re-fetched
    status = s.query(m.IngestionRun).order_by(m.IngestionRun.id).all()
    assert [r.status for r in status] == ["failed", "complete"]


def test_expired_logs_are_recorded_and_learned_from(session_factory):
    cutoff = datetime(2026, 10, 2, 12, 0, 0)
    client = FakeIngestClient(expired_before=cutoff)
    ing, _, s = make(session_factory, client)
    ing.run()
    statuses = dict(s.query(m.JobLogFetch.status, func.count()).group_by(m.JobLogFetch.status).all())
    assert statuses.get("expired", 0) >= 1
    expired_jobs = s.query(m.WorkflowJob).filter_by(cache_label_status="expired").all()
    assert expired_jobs and all(j.cache_hit is None for j in expired_jobs)
    # once a 410 was seen, older runs are not even requested
    older_requests = client.calls["open_job_log"]
    assert older_requests < 6 * 3


def test_rerun_attempt_replaces_stale_jobs(session_factory):
    ing, client, s = make(session_factory)
    ing.run()
    n_jobs, n_events = s.query(m.WorkflowJob).count(), s.query(m.JobCacheEvent).count()
    old_ids = {j for (j,) in s.query(m.WorkflowJob.github_job_id).filter(m.WorkflowJob.run_id == "1000")}
    client.attempt_override[1000] = 2                                    # run 1000 was re-run
    ing2, _, _ = make(session_factory, client)
    ing2.db = s
    ing2.run()
    new_ids = {j for (j,) in s.query(m.WorkflowJob.github_job_id).filter(m.WorkflowJob.run_id == "1000")}
    assert new_ids and not (new_ids & old_ids)
    assert s.query(m.WorkflowJob).count() == n_jobs                      # replaced, not appended
    assert s.query(m.JobCacheEvent).count() == n_events                  # old attempt's events purged
    assert s.query(m.WorkflowRun).filter_by(run_id="1000").one().run_attempt == 2


def test_max_runs_keeps_the_newest_runs(session_factory):
    ing, client, s = make(session_factory, max_runs=2)
    ing.run()
    done = {k for (k,) in s.query(m.IngestionProgress.key).filter_by(stage="run_done")}
    newest = {str(r["id"]) for r in sorted(client.runs, key=lambda r: r["created_at"], reverse=True)[:2]}
    assert done == newest


def test_max_runs_counts_only_this_invocations_runs(session_factory):
    ing, client, s = make(session_factory, max_runs=2)
    ing.run()
    first = {k for (k,) in s.query(m.IngestionProgress.key).filter_by(stage="run_done")}
    ing2, _, _ = make(session_factory, client, max_runs=2)
    ing2.db = s
    ing2.run()                                                           # earlier runs must not use up the new cap
    second = {k for (k,) in s.query(m.IngestionProgress.key).filter_by(stage="run_done")}
    assert len(first) == 2 and len(second) == 4 and first < second


def test_no_logs_option_leaves_every_cache_label_null(session_factory):
    ing, client, s = make(session_factory, fetch_logs=False)
    ing.run()
    assert client.calls["open_job_log"] == 0
    assert s.query(m.WorkflowJob).filter(m.WorkflowJob.cache_hit.isnot(None)).count() == 0
    assert s.query(m.JobLogFetch).count() == 0


def test_single_run_ingestion_for_webhooks(session_factory):
    ing, client, s = make(session_factory)
    counters = ing.ingest_single_run(1002)
    assert counters["runs_processed"] == 1
    assert s.query(m.WorkflowRun).count() == 1 and s.query(m.WorkflowJob).count() == 4
    assert s.query(m.IngestionProgress).filter_by(stage="run_done", key="1002").count() == 1
    assert [r.status for r in s.query(m.IngestionRun).order_by(m.IngestionRun.id)] == ["complete"]


def test_status_reports_real_counts(session_factory):
    ing, client, s = make(session_factory)
    ing.run()
    st = ingestion_status(s, "o/r")
    assert st["runs_total"] == 6 and st["jobs_total"] == 24 and st["runs_fully_processed"] == 6
    assert st["cache_label_values"]["hit"] == 3 and st["cache_label_values"]["miss"] == 3
    assert st["cache_label_values"]["NULL"] == 18
    assert st["diff_sources"] == {"commit": 3, "compare_pr": 3}


def test_fork_pr_runs_resolve_their_pr_through_the_commit_lookup(session_factory):
    """pull_requests[] is empty for fork PRs; the whole-PR diff is still obtained via commits/{sha}/pulls."""
    ing, client, s = make(session_factory, FakeIngestClient(fork_prs=True))
    ing.run()
    sources = dict(s.query(m.WorkflowRun.diff_source, func.count()).group_by(m.WorkflowRun.diff_source).all())
    assert sources == {"commit": 3, "compare_pr": 3}
    assert client.calls["pulls_for_commit"] == 3 and client.calls["compare"] == 3
    pr_run = s.query(m.WorkflowRun).filter_by(diff_source="compare_pr").first()
    assert pr_run.pr_number == 7 and pr_run.pr_base_sha == "b" * 40


def test_pr_event_run_with_no_pull_request_is_marked_not_guessed(session_factory):
    ing, client, s = make(session_factory, FakeIngestClient(fork_prs=True, pr_lookup_finds=False))
    ing.run()
    sources = dict(s.query(m.WorkflowRun.diff_source, func.count()).group_by(m.WorkflowRun.diff_source).all())
    assert sources == {"commit": 3, "commit_only": 3}                    # head-commit diff, explicitly labelled as such
    assert client.calls["compare"] == 0


def test_legacy_head_commit_diffs_of_pr_runs_are_upgraded_and_derived_rows_dropped(session_factory):
    ing, client, s = make(session_factory, FakeIngestClient(fork_prs=True))
    ing.run()
    run = s.query(m.WorkflowRun).filter_by(diff_source="compare_pr").first()
    # simulate a row ingested before the PR lookup existed
    run.diff_source, run.pr_number, run.pr_base_sha = "commit", None, None
    s.query(m.RunChangedFile).filter_by(run_id=run.run_id).delete()
    s.add(m.RunChangedFile(run_id=run.run_id, filename="only/head-commit.py", status="modified", additions=1, deletions=0))
    s.add(m.RunGraphFeatures(run_id=run.run_id, job_key="lint", job_structurally_impacted=1, cache_key_touched=0,
                             dep_graph_distance=-1.0, any_changed_file_in_graph=0))
    s.commit()
    ing2, _, _ = make(session_factory, client)
    ing2.db = s
    ing2.run()
    s.refresh(run)
    assert run.diff_source == "compare_pr" and run.pr_number == 7
    assert {f for (f,) in s.query(m.RunChangedFile.filename).filter_by(run_id=run.run_id)} == {"src/a.py", "requirements.txt"}
    assert s.query(m.RunGraphFeatures).filter_by(run_id=run.run_id).count() == 0   # stale graph features removed
