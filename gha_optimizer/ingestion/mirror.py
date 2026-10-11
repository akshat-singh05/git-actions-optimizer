"""
Local bare-git mirror of a GitHub repository.

Used for two things the REST API makes expensive:

* the workflow YAML *as it was at a run's head sha* (to map API job names to YAML
  job keys and to build the dependency graph of that commit), and
* the source tree at that sha (parsed by the tree-sitter parsers).

Reading objects from the mirror costs no API requests.  Commits that are not on
any branch of the clone (fork pull-request heads) are fetched on demand by sha.
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MIRROR_ROOT = Path(".gha_optimizer") / "mirrors"


class MirrorError(RuntimeError):
    pass


class BlobReader:
    """Persistent ``git cat-file --batch`` process: read many blobs cheaply."""

    def __init__(self, git_dir: Path, git: str = "git"):
        self._proc = subprocess.Popen(
            [git, "--git-dir", str(git_dir), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self, blob_sha: str) -> bytes | None:
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(blob_sha.encode() + b"\n")
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().split()
        if len(header) < 3:  # "<sha> missing"
            return None
        size = int(header[2])
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # trailing newline
        return data

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except Exception:  # pragma: no cover - best effort
            self._proc.kill()

    def __enter__(self) -> "BlobReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never block on a credentials prompt


def git_env(token: str | None = None) -> dict[str, str]:
    """Environment for git subprocesses.  With a token, github.com requests carry an ``Authorization`` header supplied
    through git's environment-based config, so the token appears in neither the URL, the command line nor the clone's
    stored remote URL (needed to mirror private repositories)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {cred}",
        })
    return env


class RepoMirror:
    def __init__(self, repo: str, root: str | Path = DEFAULT_MIRROR_ROOT, git: str = "git", offline: bool = False,
                 token: str | None = None):
        """``offline=True`` never touches the network (tests, air-gapped use): no clone, fetch or sha-fetch.
        ``token`` authenticates clone / fetch (private repositories); without it requests are anonymous."""
        if "/" not in repo:
            raise ValueError("repo must be 'owner/name'")
        owner, name = repo.split("/", 1)
        self.repo = repo
        self.git = git
        self.offline = offline
        self._env = git_env(token)
        self.path = Path(root).resolve() / f"{owner}__{name}.git"

    # ------------------------------------------------------------------
    def _run(self, *args: str, check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.git, "--git-dir", str(self.path), *args],
            capture_output=True,
            timeout=timeout,
            env=self._env,
        )
        if check and proc.returncode != 0:
            raise MirrorError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
        return proc

    def exists(self) -> bool:
        return (self.path / "HEAD").exists()

    def ensure(self, url: str | None = None, refresh: bool = True) -> None:
        """Clone (bare, no tags) if missing, otherwise fetch the branch heads."""
        url = url or f"https://github.com/{self.repo}.git"
        if self.offline:
            if not self.exists():
                raise MirrorError(f"offline mirror {self.path} does not exist")
            return
        if not self.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("cloning %s into %s (one-off; this can take minutes)", url, self.path)
            proc = subprocess.run(
                [self.git, "clone", "--bare", "--no-tags", "--quiet", url, str(self.path)],
                capture_output=True,
                env=self._env,
            )
            if proc.returncode != 0:
                raise MirrorError(f"git clone failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
            return
        if refresh:
            logger.info("refreshing mirror %s", self.path)
            self._run("fetch", "--no-tags", "--prune", url, "+refs/heads/*:refs/heads/*", check=False)

    # ------------------------------------------------------------------
    def head_sha(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.decode().strip()

    def has_commit(self, sha: str) -> bool:
        return self._run("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def fetch_sha(self, sha: str, url: str | None = None) -> bool:
        """Fetch a commit by sha (e.g. a fork PR head).  Returns whether it is now present."""
        if self.has_commit(sha):
            return True
        if self.offline:
            return False
        url = url or f"https://github.com/{self.repo}.git"
        self._run("fetch", "--no-tags", url, sha, check=False, timeout=600)
        return self.has_commit(sha)

    # ---- history queries (PR diffs without API calls) ----------------------

    def commit_before(self, ref: str, iso_timestamp: str) -> str | None:
        """Newest commit reachable from ``ref`` committed at or before ``iso_timestamp`` (UTC)."""
        proc = self._run("rev-list", "-1", f"--before={iso_timestamp}", ref, check=False)
        out = proc.stdout.decode().strip()
        return out if proc.returncode == 0 and out else None

    def merge_base(self, a: str, b: str) -> str | None:
        proc = self._run("merge-base", a, b, check=False)
        out = proc.stdout.decode().strip()
        return out if proc.returncode == 0 and out else None

    def diff_files(self, base: str, head: str) -> list[dict]:
        """Files changed between two commits as ``[{filename, status, additions, deletions}]``
        (no rename detection: a rename is a delete + an add)."""
        names = self._run("diff", "--name-status", "-z", "--no-renames", base, head).stdout.split(b"\0")
        nums = self._run("diff", "--numstat", "-z", "--no-renames", base, head).stdout.split(b"\0")
        status_of: dict[str, str] = {}
        it = iter(n for n in names if n != b"")
        for code, path in zip(it, it):
            status_of[path.decode("utf-8", "replace")] = {"A": "added", "D": "removed", "M": "modified"}.get(code.decode()[:1], "changed")
        out: list[dict] = []
        for rec in nums:
            if not rec:
                continue
            add, dele, path = rec.decode("utf-8", "replace").split("\t", 2)
            out.append({
                "filename": path,
                "status": status_of.get(path, "modified"),
                "additions": int(add) if add.isdigit() else 0,   # '-' for binary files
                "deletions": int(dele) if dele.isdigit() else 0,
            })
        return out

    def blob_sha(self, commit: str, path: str) -> str | None:
        proc = self._run("rev-parse", "--verify", "-q", f"{commit}:{path}", check=False)
        out = proc.stdout.decode().strip()
        return out if proc.returncode == 0 and out else None

    def read_blob(self, blob_sha: str) -> bytes | None:
        proc = self._run("cat-file", "blob", blob_sha, check=False)
        return proc.stdout if proc.returncode == 0 else None

    def read_file(self, commit: str, path: str) -> tuple[str, bytes] | None:
        """``(blob_sha, content)`` of ``path`` at ``commit``; None if absent."""
        sha = self.blob_sha(commit, path)
        if sha is None:
            return None
        data = self.read_blob(sha)
        return (sha, data) if data is not None else None

    def list_tree(self, commit: str) -> list[tuple[str, str]]:
        """``[(path, blob_sha)]`` for every blob in the tree at ``commit`` (posix paths)."""
        proc = self._run("ls-tree", "-r", "-z", "--full-tree", commit)
        out: list[tuple[str, str]] = []
        for entry in proc.stdout.split(b"\0"):
            if not entry:
                continue
            meta, _, path = entry.partition(b"\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == b"blob":
                out.append((path.decode("utf-8", "replace"), parts[2].decode()))
        return out

    def blob_reader(self) -> BlobReader:
        return BlobReader(self.path, self.git)
