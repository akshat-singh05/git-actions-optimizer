"""
Helpers for a local checkout of a repository: which source files are tracked, which
files the working tree changes, and where a workflow lives relative to the repo root.
Shared by the CLI, the API server and the demos so there is one implementation.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func

SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go")


def git_lines(repo_root: Path, *git_args: str) -> list[str] | None:
    """Output lines of ``git -C repo_root <args>``; ``None`` if git/the repo is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), *git_args], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def tracked_source_files(repo_root: Path) -> list[str]:
    """Posix-relative paths of the parseable source files of the checkout."""
    files = git_lines(repo_root, "ls-files")
    if files is None:
        files = [p.relative_to(repo_root).as_posix() for p in repo_root.rglob("*") if p.is_file()]
    return [f for f in files if f.endswith(SOURCE_SUFFIXES)]


def working_tree_changes(repo_root: Path) -> list[str]:
    """Files differing from HEAD in the working tree (the "current diff"), or [] if unknown."""
    return git_lines(repo_root, "diff", "--name-only", "HEAD") or []


@dataclass
class RepoSnapshot:
    """A real repository tree (exported from the git mirror) plus its real ingested context."""

    repo: str
    sha: str
    root: Path
    workflow_path: Path
    workflow_repo_path: str
    tracked_files: list[str]
    changed_files: list[str]            # the real diff of the most recent ingested run of the workflow
    changed_from_run: str
    alt_changed_files: list[str]        # the real diff of the previous, different run (for comparison)
    history: list[Any]


def prepare_repo_snapshot(
    db: Any,
    repo: str,
    workflow_repo_path: str | None = None,
    snapshot_root: Path = Path(".gha_optimizer") / "snapshots",
    mirror_root: Path | None = None,
) -> RepoSnapshot:
    """Export the default-branch tip of an ingested repo and load its real context.

    Used by the demos.  Raises :class:`InsufficientHistoryError` (never invents data) when the
    repository has not been ingested, and ``LookupError`` if it has no ingested run of the workflow.
    """
    from gha_optimizer.ingestion.history import InsufficientHistoryError, load_history
    from gha_optimizer.ingestion.mirror import DEFAULT_MIRROR_ROOT, RepoMirror
    from gha_optimizer.server.db import models as m

    q = db.query(m.WorkflowRun).filter(m.WorkflowRun.repo == repo, m.WorkflowRun.status == "completed",
                                       m.WorkflowRun.diff_source.isnot(None))
    if workflow_repo_path is None:  # default: the workflow with the most ingested runs
        row = (
            db.query(m.WorkflowRun.workflow_path, func.count(m.WorkflowRun.id))
            .filter(m.WorkflowRun.repo == repo).group_by(m.WorkflowRun.workflow_path)
            .order_by(func.count(m.WorkflowRun.id).desc()).first()
        )
        if row is None:
            raise InsufficientHistoryError("repository has not been ingested", repo=repo)
        workflow_repo_path = row[0]
    runs = q.filter(m.WorkflowRun.workflow_path == workflow_repo_path).order_by(m.WorkflowRun.gh_created_at.desc()).all()
    if not runs:
        raise LookupError(f"no ingested run of {workflow_repo_path} in {repo}")

    def diff_of(run: Any) -> list[str]:
        return sorted(f for (f,) in db.query(m.RunChangedFile.filename).filter_by(run_id=run.run_id))

    changed = diff_of(runs[0])
    alt: list[str] = []
    for run in runs[1:]:
        cand = diff_of(run)
        if cand and cand != changed:
            alt = cand
            break

    mirror = RepoMirror(repo, mirror_root or DEFAULT_MIRROR_ROOT)
    if not mirror.exists():
        raise InsufficientHistoryError("no local git mirror; run `gha-optimizer ingest` first", repo=repo)
    sha = mirror.head_sha()
    dest = Path(snapshot_root) / repo.replace("/", "__")
    wanted = [p for p, _ in mirror.list_tree(sha) if p.endswith(SOURCE_SUFFIXES) or p == workflow_repo_path]
    blobs = dict(mirror.list_tree(sha))
    with mirror.blob_reader() as reader:
        for p in wanted:
            data = reader.read(blobs[p])
            if data is None:
                continue
            target = dest / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    wf_local = dest / workflow_repo_path
    if not wf_local.exists():
        raise LookupError(f"{workflow_repo_path} does not exist at the default-branch tip of {repo}")
    return RepoSnapshot(
        repo=repo, sha=sha, root=dest, workflow_path=wf_local, workflow_repo_path=workflow_repo_path,
        tracked_files=sorted(p for p in wanted if p.endswith(SOURCE_SUFFIXES)),
        changed_files=changed, changed_from_run=runs[0].run_id, alt_changed_files=alt,
        history=load_history(db, repo, workflow_repo_path),
    )


def default_repo_root(workflow_path: Path) -> Path:
    """Directory containing ``.github`` if the workflow lives under it, else the cwd."""
    parts = workflow_path.resolve().parts
    if ".github" in parts:
        return Path(*parts[: parts.index(".github")])
    return Path.cwd()


def workflow_repo_path(workflow_path: Path, repo_root: Path, override: str | None = None) -> str:
    """Repo-relative posix path of the workflow (the key ingested history is stored under)."""
    if override:
        return override
    try:
        return workflow_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return f".github/workflows/{workflow_path.name}"
