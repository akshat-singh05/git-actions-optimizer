"""Celery tasks executed in-process (eager mode, no broker): routing, refusals and decisions.

The webhook handler must dispatch exactly the right downstream task for each event type, never invent a result
row, refuse work it has no credentials for, and never report a graph for a commit it cannot read.
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


# ---- graph tasks read the real commit graph from the git mirror ------------------------------------------

WORKFLOW = """\
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ruff check src
  unit:
    needs: lint
    runs-on: ubuntu-latest
    steps:
      - run: pytest tests
"""


@pytest.fixture()
def mirror_of_a_real_repo(tmp_path, monkeypatch):
    """A bare mirror of a tiny local git repository, in the layout RepoMirror expects; no network is touched."""
    import subprocess

    from gha_optimizer.ingestion import mirror as mirror_module

    work = tmp_path / "work"
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / "src").mkdir()
    (work / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    (work / "src" / "app.py").write_text("import os\n\ndef main():\n    return os.getcwd()\n", encoding="utf-8")
    (work / "README.md").write_text("hello\n", encoding="utf-8")

    def git(*args, cwd=work):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    sha = git("rev-parse", "HEAD")
    root = tmp_path / "mirrors"
    root.mkdir()
    git("clone", "-q", "--bare", str(work), str(root / "o__r.git"), cwd=tmp_path)

    real = mirror_module.RepoMirror
    monkeypatch.setattr(mirror_module, "RepoMirror",
                        lambda repo, root_=None, token=None, **kw: real(repo, root, offline=True))
    return sha


def test_graph_task_builds_the_real_graph_of_the_commit(eager, mirror_of_a_real_repo):
    out = tasks.generate_dependency_graph.apply(args=("o/r", mirror_of_a_real_repo, ["src/app.py"])).get()
    assert out["status"] == "complete" and out["changed_files"] == 1
    assert out["source_nodes"] >= 3                                    # at least the three tree paths are File nodes
    wf = out["workflows"][".github/workflows/ci.yml"]
    assert wf["nodes"] > out["source_nodes"] - 1 and wf["edges"] > 0   # the CI graph is fused into the source graph


def test_graph_task_is_blocked_for_a_commit_the_mirror_does_not_have(eager, mirror_of_a_real_repo):
    out = tasks.generate_dependency_graph.apply(args=("o/r", "f" * 40)).get()
    assert out == {"status": "blocked", "reason": "commit_not_in_mirror", "repo": "o/r", "commit_sha": "f" * 40}


def test_benchmark_measures_the_real_graph_and_refuses_an_unknown_commit(eager, mirror_of_a_real_repo):
    out = tasks.run_benchmark.apply(args=("o/r", mirror_of_a_real_repo)).get()
    report = out["benchmark"]
    assert out["status"] == "complete" and report["node_count"] > 0 and report["edge_count"] > 0
    assert report["graph_build_time_ms"] > 0 and report["memory_usage_bytes"] > 0 and report["snapshot_storage_bytes"] > 0
    assert report["incremental_update_time_ms"] is None and report["traversal_time_ms"] is None   # not measured: None, never 0
    assert tasks.run_benchmark.apply(args=("o/r", "e" * 40)).get()["reason"] == "commit_not_in_mirror"
