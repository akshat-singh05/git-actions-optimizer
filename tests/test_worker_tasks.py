"""Celery tasks executed in-process (eager mode, no broker): routing, refusals and decisions.

The webhook handler must dispatch exactly the right downstream task for each event type, never invent a result
row, refuse work it has no credentials for, and retrain only when drift or ``force`` says so - and never train on
too little data.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.server.db import models  # noqa: F401  (registers the tables)
from gha_optimizer.server.db import session as session_module
from gha_optimizer.server.db.session import Base
from gha_optimizer.worker import tasks
from gha_optimizer.worker.celery_app import celery_app


class _Recorder:
    """Stands in for a Celery task's ``.delay`` and records what was dispatched."""

    def __init__(self):
        self.calls: list[tuple] = []

    def delay(self, *args, **kwargs):
        self.calls.append(args)

        class _Result:
            id = "task-123"

        return _Result()


@pytest.fixture()
def eager(monkeypatch):
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(session_module, "engine", engine)               # init_db() creates tables here
    monkeypatch.setattr(session_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False))
    return engine


def test_push_dispatches_graph_generation_with_the_changed_files(eager, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(tasks, "generate_dependency_graph", rec)
    payload = {"repository": {"full_name": "o/r"}, "ref": "refs/heads/main", "after": "abc123",
               "commits": [{"added": ["a.py"], "modified": ["b.py"], "removed": ["c.py"]}, {"modified": ["b.py"]}]}
    out = tasks.process_webhook.apply(args=("push", payload)).get()
    assert out["status"] == "processed" and out["changed_files"] == 3         # b.py counted once
    (repo, sha, files), = rec.calls
    assert (repo, sha) == ("o/r", "abc123") and sorted(files) == ["a.py", "b.py", "c.py"]


@pytest.mark.parametrize("action,dispatched", [("opened", 1), ("synchronize", 1), ("closed", 0), ("labeled", 0)])
def test_pull_request_triggers_feature_extraction_only_for_new_code(eager, monkeypatch, action, dispatched):
    rec = _Recorder()
    monkeypatch.setattr(tasks, "extract_features", rec)
    payload = {"repository": {"full_name": "o/r"}, "action": action,
               "pull_request": {"number": 7, "head": {"sha": "deadbeefcafe"}}}
    out = tasks.process_webhook.apply(args=("pull_request", payload)).get()
    assert out["status"] == "processed" and len(rec.calls) == dispatched
    if dispatched:
        assert rec.calls[0] == ("o/r", "deadbeefcafe")


def test_completed_workflow_run_is_ingested_for_real_not_recorded_as_a_placeholder(eager, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(tasks, "ingest_run", rec)
    done = {"repository": {"full_name": "o/r"}, "action": "completed",
            "workflow_run": {"id": 99, "conclusion": "failure"}}
    out = tasks.process_webhook.apply(args=("workflow_run", done)).get()
    assert rec.calls == [("o/r", "99")] and out["ingest_task_id"] == "task-123" and out["conclusion"] == "failure"
    started = {**done, "action": "requested"}
    out = tasks.process_webhook.apply(args=("workflow_run", started)).get()
    assert out["status"] == "ignored" and len(rec.calls) == 1                  # nothing to ingest before it finishes


def test_job_events_and_unknown_events(eager):
    job = {"repository": {"full_name": "o/r"}, "action": "completed",
           "workflow_job": {"name": "lint", "conclusion": "success"}}
    assert tasks.process_webhook.apply(args=("workflow_job", job)).get()["job_name"] == "lint"
    assert tasks.process_webhook.apply(args=("workflow_job", {**job, "action": "queued"})).get()["status"] == "ignored"
    assert tasks.process_webhook.apply(args=("star", {"repository": {"full_name": "o/r"}})).get()["status"] == "ignored"


def test_ingestion_tasks_refuse_to_run_without_a_github_token(eager, monkeypatch):
    from gha_optimizer.config import config

    monkeypatch.setattr(config.github, "token", None)
    out = tasks.sync_repository.apply(args=("o/r",)).get()
    assert out == {"status": "blocked", "reason": "no_github_token", "repo": "o/r"}
    out = tasks.ingest_run.apply(args=("o/r", "5")).get()
    assert out["status"] == "blocked" and out["reason"] == "no_github_token"


def test_retrain_skips_without_drift_and_blocks_on_too_little_data(eager):
    skipped = tasks.retrain_models.apply(args=("o/r",)).get()
    assert skipped["status"] == "skipped" and skipped["reason"] == "no_drift"
    blocked = tasks.retrain_models.apply(args=("o/r",), kwargs={"force": True}).get()
    assert blocked["status"] == "blocked" and blocked["reason"] == "insufficient_data" and blocked["runs"] == 0
