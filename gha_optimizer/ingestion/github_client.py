"""
Thin client around the GitHub REST API for pulling workflow run history,
job timings, and commit diffs.

In the demo/tests we never hit the network — we use
``synthetic_history.py``-style generators instead.  In a real deployment
``GithubClient`` is backed by ``requests`` authenticated as a GitHub App
or via a personal access token.

Every other module only depends on the ``WorkflowRunRecord`` interface,
not on GitHub's actual API shape, so this client can be swapped, mocked,
or stubbed freely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class WorkflowRunRecord:
    """One (run × job) observation — the atomic unit the ML models train on."""

    run_id: str
    commit_sha: str
    files_changed: list[str]
    job_id: str
    duration_seconds: float
    conclusion: str  # "success" | "failure" | "cancelled" | "skipped"
    cache_hit: bool
    author: str
    timestamp: str
    branch: str = "main"
    runner_label: str = "ubuntu-latest"
    loc_added: int = 0
    loc_deleted: int = 0


# ---------------------------------------------------------------------------
# Real GitHub REST client
# ---------------------------------------------------------------------------

class GithubClient:
    """
    Production-ready GitHub REST API client.

    Requires ``requests`` (listed in requirements.txt).  If no token is
    provided the client still works for public repos but is subject to
    GitHub's unauthenticated rate limit (60 req/h).
    """

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        api_base: str = "https://api.github.com",
        per_page: int = 100,
        max_pages: int = 10,
        timeout: int = 30,
    ):
        self.token = token
        self.repo = repo  # "owner/repo"
        self.api_base = api_base.rstrip("/")
        self.per_page = per_page
        self.max_pages = max_pages
        self.timeout = timeout

        # Lazy import so the rest of the package works without ``requests``
        # when using only synthetic data.
        try:
            import requests as _requests

            self._session = _requests.Session()
            if self.token:
                self._session.headers["Authorization"] = f"Bearer {self.token}"
            self._session.headers["Accept"] = "application/vnd.github+json"
            self._session.headers["X-GitHub-Api-Version"] = "2022-11-28"
            self._requests = _requests
        except ImportError:
            self._session = None  # type: ignore[assignment]
            self._requests = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_session(self) -> None:
        if self._session is None:
            raise ImportError(
                "The 'requests' package is required for live GitHub API "
                "access.  Install it with:  pip install requests"
            )

    def _require_repo(self) -> str:
        if not self.repo:
            raise ValueError(
                "No repository configured.  Set the 'repo' parameter "
                "(e.g. 'owner/repo') or the GITHUB_REPO env-var."
            )
        return self.repo

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with pagination awareness and error handling."""
        self._require_session()
        url = f"{self.api_base}{path}"
        resp = self._session.get(url, params=params, timeout=self.timeout)

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = resp.headers.get("X-RateLimit-Reset", "unknown")
            raise RuntimeError(
                f"GitHub API rate limit exceeded.  Resets at {reset}.  "
                "Provide a GITHUB_TOKEN for a higher limit."
            )
        resp.raise_for_status()
        return resp.json()

    def _get_paginated(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Fetch all pages up to ``max_pages``."""
        params = dict(params or {})
        params.setdefault("per_page", self.per_page)

        results: list[Any] = []
        for page in range(1, self.max_pages + 1):
            params["page"] = page
            data = self._get(path, params)
            items = data.get(key, []) if isinstance(data, dict) else data
            results.extend(items)
            if len(items) < self.per_page:
                break
        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_recent_runs(
        self, limit: int = 200, workflow_id: str | None = None
    ) -> list[WorkflowRunRecord]:
        """
        Fetch recent workflow-run records, one per (run, job) pair.

        Calls:
            GET /repos/{owner}/{repo}/actions/runs
            GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs
            GET /repos/{owner}/{repo}/commits/{sha}
        """
        repo = self._require_repo()
        path = f"/repos/{repo}/actions/runs"
        params: dict[str, Any] = {}
        if workflow_id:
            params["workflow_id"] = workflow_id

        runs_raw = self._get_paginated(path, "workflow_runs", params)
        records: list[WorkflowRunRecord] = []

        for run in runs_raw[:limit]:
            run_id = str(run["id"])
            sha = run.get("head_sha", "")
            branch = run.get("head_branch", "main")
            author = (
                run.get("actor", {}).get("login", "unknown")
                if run.get("actor")
                else "unknown"
            )
            timestamp = run.get("created_at", "")

            # Fetch changed files for this commit
            files_changed = self._fetch_commit_files(repo, sha)

            # LOC stats
            loc_added, loc_deleted = self._fetch_commit_loc(repo, sha)

            # Fetch per-job details
            jobs_path = f"/repos/{repo}/actions/runs/{run_id}/jobs"
            try:
                jobs_data = self._get(jobs_path)
                jobs_list = jobs_data.get("jobs", [])
            except Exception as exc:
                logger.warning("Failed to fetch jobs for run %s: %s", run_id, exc)
                continue

            for job in jobs_list:
                job_name = job.get("name", "unknown")
                conclusion = job.get("conclusion") or "unknown"
                runner_label = job.get("labels", ["ubuntu-latest"])[0] if job.get("labels") else "ubuntu-latest"

                # Duration from started_at / completed_at
                duration = self._compute_job_duration(job)

                # Cache hit heuristic: check if any step name contains
                # "cache" and its conclusion is "success" with short duration
                cache_hit = self._detect_cache_hit(job)

                records.append(
                    WorkflowRunRecord(
                        run_id=run_id,
                        commit_sha=sha,
                        files_changed=files_changed,
                        job_id=job_name,
                        duration_seconds=duration,
                        conclusion=conclusion,
                        cache_hit=cache_hit,
                        author=author,
                        timestamp=timestamp,
                        branch=branch,
                        runner_label=runner_label,
                        loc_added=loc_added,
                        loc_deleted=loc_deleted,
                    )
                )

        logger.info("Fetched %d run-job records from %s", len(records), repo)
        return records

    def fetch_commit_diff(self, sha: str) -> list[str]:
        """Return the list of file paths changed in a given commit."""
        repo = self._require_repo()
        return self._fetch_commit_files(repo, sha)

    def fetch_workflow_files(self) -> list[dict[str, Any]]:
        """
        List ``.github/workflows/*.yml`` files from the repo's default branch.
        Returns list of dicts with keys: ``name``, ``path``, ``download_url``.
        """
        repo = self._require_repo()
        path = f"/repos/{repo}/contents/.github/workflows"
        try:
            items = self._get(path)
        except Exception:
            return []

        return [
            {
                "name": item["name"],
                "path": item["path"],
                "download_url": item.get("download_url"),
            }
            for item in items
            if item.get("name", "").endswith((".yml", ".yaml"))
        ]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_commit_files(self, repo: str, sha: str) -> list[str]:
        """Get the list of files changed in a commit."""
        if not sha:
            return []
        try:
            data = self._get(f"/repos/{repo}/commits/{sha}")
            return [f["filename"] for f in data.get("files", [])]
        except Exception as exc:
            logger.debug("Could not fetch files for %s: %s", sha, exc)
            return []

    def _fetch_commit_loc(self, repo: str, sha: str) -> tuple[int, int]:
        """Get lines added/deleted stats for a commit."""
        if not sha:
            return 0, 0
        try:
            data = self._get(f"/repos/{repo}/commits/{sha}")
            stats = data.get("stats", {})
            return stats.get("additions", 0), stats.get("deletions", 0)
        except Exception:
            return 0, 0

    @staticmethod
    def _compute_job_duration(job: dict[str, Any]) -> float:
        """Compute job duration in seconds from API timestamps."""
        started = job.get("started_at")
        completed = job.get("completed_at")
        if not started or not completed:
            return 0.0

        from datetime import datetime

        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            start_dt = datetime.strptime(started, fmt)
            end_dt = datetime.strptime(completed, fmt)
            return max((end_dt - start_dt).total_seconds(), 0.0)
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _detect_cache_hit(job: dict[str, Any]) -> bool:
        """
        Heuristic cache-hit detection from job step data.

        Looks for steps using ``actions/cache`` and checks if the
        "Post" step (cache save) was skipped, which indicates the
        cache was already present (hit).
        """
        steps = job.get("steps", [])
        for step in steps:
            name = (step.get("name") or "").lower()
            if "cache" in name and "post" not in name:
                # The restore step completing quickly suggests a hit
                conclusion = step.get("conclusion", "")
                if conclusion == "success":
                    # Check if a corresponding "Post" step was skipped
                    post_skipped = any(
                        ("post" in (s.get("name") or "").lower())
                        and s.get("conclusion") == "skipped"
                        for s in steps
                    )
                    if post_skipped:
                        return True

        # Fallback: if there's a "Run actions/cache" step that succeeded
        # and took < 5 seconds, likely a cache hit
        for step in steps:
            name = (step.get("name") or "").lower()
            if "cache" in name and step.get("conclusion") == "success":
                started = step.get("started_at")
                completed = step.get("completed_at")
                if started and completed:
                    from datetime import datetime

                    try:
                        fmt = "%Y-%m-%dT%H:%M:%SZ"
                        delta = (
                            datetime.strptime(completed, fmt)
                            - datetime.strptime(started, fmt)
                        ).total_seconds()
                        if delta < 5:
                            return True
                    except (ValueError, TypeError):
                        pass

        return False
