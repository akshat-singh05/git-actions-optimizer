"""PR diffs from the git mirror when the PR cannot be resolved through the API.

Real tiny repository: main has A then C; branch `feature` has P1, P2 on top of A.  For a PR-event run of
head P2 started after C, the PR diff is merge-base(main-at-run-time, P2)..P2 = A..P2 = {f1.py, base.py};
the head commit's own diff would be only {base.py}, and `main..P2` would wrongly include other.py."""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
from gha_optimizer.ingestion.mirror import RepoMirror
from gha_optimizer.server.db import models as m
from gha_optimizer.server.db.session import Base
from tests.test_ingest import WORKFLOW_YAML, FakeIngestClient

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def git(cwd, when, *args):
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


@pytest.fixture()
def pr_repo(tmp_path):
    w = tmp_path / "work"
    (w / ".github" / "workflows").mkdir(parents=True)
    (w / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW_YAML, encoding="utf-8")
    (w / "base.py").write_text("x = 1\n", encoding="utf-8")
    git(w, "2026-10-01T00:00:00Z", "init", "-q", "-b", "main")
    git(w, "2026-10-01T00:00:00Z", "add", "-A")
    git(w, "2026-10-01T00:00:00Z", "commit", "-q", "-m", "A")
    git(w, "2026-10-01T00:00:00Z", "checkout", "-q", "-b", "feature")
    (w / "f1.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    git(w, "2026-10-01T12:00:00Z", "add", "-A")
    git(w, "2026-10-01T12:00:00Z", "commit", "-q", "-m", "P1")
    (w / "base.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    git(w, "2026-10-01T12:30:00Z", "add", "-A")
    git(w, "2026-10-01T12:30:00Z", "commit", "-q", "-m", "P2")
    p2 = git(w, "2026-10-01T12:30:00Z", "rev-parse", "HEAD")
    git(w, "2026-10-01T18:00:00Z", "checkout", "-q", "main")
    (w / "other.py").write_text("o = 1\n", encoding="utf-8")
    git(w, "2026-10-01T18:00:00Z", "add", "-A")
    git(w, "2026-10-01T18:00:00Z", "commit", "-q", "-m", "C")
    root = tmp_path / "mirrors"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(w), str(root / "o__r.git")], check=True, capture_output=True)
    return p2, RepoMirror("o/r", root=root, offline=True)


def test_pr_diff_comes_from_merge_base_not_from_the_head_commit_or_main_tip(pr_repo):
    p2, mirror = pr_repo
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    client = FakeIngestClient(fork_prs=True, pr_lookup_finds=False, head_shas={3: p2})   # run 1003: PR event, head P2
    opts = IngestOptions(since=datetime(2026, 10, 1), until=datetime(2026, 10, 3, 23, 59, 59), use_mirror=True,
                         mirror_refresh=False, log_workers=1)
    RepoIngestor("o/r", client, db, opts, mirror=mirror).run()

    run = db.query(m.WorkflowRun).filter_by(run_id="1003").one()
    assert run.event == "pull_request" and run.diff_source == "git_merge_base"
    files = {f.filename: f for f in db.query(m.RunChangedFile).filter_by(run_id="1003")}
    assert set(files) == {"f1.py", "base.py"}                      # the PR's files — not other.py (main moved on), not just base.py
    assert files["f1.py"].status == "added" and files["f1.py"].additions == 2
    assert files["base.py"].status == "modified" and (files["base.py"].additions, files["base.py"].deletions) == (2, 0)
    assert (run.loc_added, run.loc_deleted) == (4, 0)
    assert run.diff_base_sha == mirror.merge_base("main", p2)
    # every other PR-event run has a head sha the mirror does not contain: honest last-resort label
    others = db.query(m.WorkflowRun).filter(m.WorkflowRun.event == "pull_request", m.WorkflowRun.run_id != "1003").all()
    assert {r.diff_source for r in others} == {"commit_only"}
    assert client.calls["compare"] == 0


def test_mirror_diff_helpers(pr_repo):
    p2, mirror = pr_repo
    base = mirror.merge_base("main", p2)
    assert mirror.commit_before("main", "2026-10-01T14:00:00Z") == base            # main before C is A
    tip_after_c = mirror.commit_before("main", "2026-10-02T00:00:00Z")
    assert tip_after_c != base and mirror.merge_base(tip_after_c, p2) == base
    names = {f["filename"] for f in mirror.diff_files(base, p2)}
    assert names == {"f1.py", "base.py"}
    assert mirror.diff_files(tip_after_c, p2) != mirror.diff_files(base, p2)         # a naive tip..head diff would differ
