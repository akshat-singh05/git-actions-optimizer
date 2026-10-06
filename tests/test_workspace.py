"""The demo/real-data workspace: a repo snapshot exported from the git mirror plus the real
ingested context (history, the real diffs).  No synthetic data on this path."""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.ingestion.history import InsufficientHistoryError
from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
from gha_optimizer.ingestion.workspace import (
    default_repo_root,
    prepare_repo_snapshot,
    tracked_source_files,
    workflow_repo_path,
)
from gha_optimizer.server.db.session import Base
from tests.test_ingest import WORKFLOW_YAML, FakeIngestClient

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   capture_output=True, text=True, check=True)


@pytest.fixture()
def env(tmp_path):
    work = tmp_path / "work"
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / "src").mkdir()
    (work / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW_YAML, encoding="utf-8")
    (work / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (work / "README.md").write_text("not source\n", encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    mirror_root = tmp_path / "mirrors"
    mirror_root.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(mirror_root / "o__r.git")], check=True, capture_output=True)

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    opts = IngestOptions(since=datetime(2026, 10, 1), until=datetime(2026, 10, 3, 23, 59, 59), use_mirror=False, log_workers=1)
    RepoIngestor("o/r", FakeIngestClient(), db, opts).run()
    return db, mirror_root, tmp_path / "snap"


def test_snapshot_exports_the_tree_and_carries_real_diffs(env):
    db, mirror_root, snap_root = env
    snap = prepare_repo_snapshot(db, "o/r", snapshot_root=snap_root, mirror_root=mirror_root)
    assert snap.workflow_repo_path == ".github/workflows/ci.yml" and snap.workflow_path.exists()
    assert snap.tracked_files == ["src/a.py"]                              # README.md is not a parseable source file
    assert (snap.root / "src" / "a.py").read_text(encoding="utf-8").startswith("def f")
    assert snap.changed_files == ["requirements.txt", "src/a.py"]          # real diff rows of the newest run (PR compare)
    assert snap.alt_changed_files == ["src/a.py"]                          # previous, different run (commit diff)
    assert len(snap.history) == 6 * 3                                       # real ingested (run, job-key) records


def test_snapshot_refuses_an_uningested_repo(env):
    db, mirror_root, snap_root = env
    with pytest.raises(InsufficientHistoryError):
        prepare_repo_snapshot(db, "nobody/never-ingested", snapshot_root=snap_root, mirror_root=mirror_root)
    with pytest.raises(LookupError):
        prepare_repo_snapshot(db, "o/r", ".github/workflows/missing.yml", snapshot_root=snap_root, mirror_root=mirror_root)


def test_workspace_path_helpers(tmp_path):
    wf = tmp_path / "repo" / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("name: x\n", encoding="utf-8")
    root = default_repo_root(wf)
    assert root == tmp_path / "repo"
    assert workflow_repo_path(wf, root) == ".github/workflows/ci.yml"
    assert workflow_repo_path(wf, root, "custom/path.yml") == "custom/path.yml"
    (root / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "notes.txt").write_text("x\n", encoding="utf-8")
    assert tracked_source_files(Path(root)) == ["m.py"]
