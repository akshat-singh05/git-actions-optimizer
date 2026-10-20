"""
GitHub REST client for real GitHub Actions history.

What it does (and, deliberately, does not do):

* Authenticates with a bearer token and reads ``X-RateLimit-Remaining`` /
  ``X-RateLimit-Reset`` from every response.  When the primary budget is
  exhausted it **sleeps until the reset** instead of failing; 403/429
  *secondary* rate limits (``Retry-After`` / abuse text) back off
  exponentially; 5xx and network errors are retried with backoff.
* Follows ``Link: rel="next"`` for pagination (never ``len(page) < per_page``).
* Enumerates workflow runs through ``created=<window>`` queries.  Measured
  against the live API: filtered listings return at most 1000 results per query
  and page order is not reliable, so windows are **bisected** whenever
  ``total_count`` reaches the cap and the number of unique ids collected is
  verified against ``total_count``.
* Never invents a value.  A failed request raises; a missing timestamp is
  ``None``.  There is no cache-hit heuristic here: GitHub's REST API does not
  expose cache outcomes, they are parsed from job logs by
  :mod:`gha_optimizer.ingestion.log_parser`.

Every other module depends on :class:`WorkflowRunRecord` only, never on the
shape of GitHub's responses.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
LISTING_CAP = 1000  # measured: filtered run listings return at most this many results
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class WorkflowRunRecord:
    """One (run × job) observation — the atomic unit the ML models train on.

    ``job_id`` is the *workflow job key* (the YAML key, e.g. ``"lint"``), the
    identifier the dependency graph and the scheduler use.  Unobserved values
    are ``None``; nothing is defaulted to a plausible number.
    """

    run_id: str
    commit_sha: str
    files_changed: list[str]
    job_id: str
    duration_seconds: float | None
    conclusion: str | None  # "success" | "failure" | "cancelled" | "skipped" | ...
    cache_hit: bool | None  # None = no log-derived label (excluded from cache training)
    author: str | None
    timestamp: str
    branch: str | None = None
    runner_label: str | None = None
    loc_added: int | None = None
    loc_deleted: int | None = None
    workflow_path: str | None = None
    event: str | None = None
    cache_outcome: str | None = None  # miss | partial_hit | exact_hit | hit_unresolved | None
    # Graph features computed on the repo tree at ``commit_sha`` (see
    # features/graph_features.py).  None => derive from the supplied static graph.
    graph_features: dict[str, float] | None = None
    # Run context known at queue time / when the run finished (None = unknown; never defaulted)
    pr_number: int | None = None
    run_attempt: int | None = None
    finished_at: str | None = None     # when the run last updated, i.e. when its outcome became known


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GithubAPIError(RuntimeError):
    """A GitHub API call failed in a way retrying will not fix."""

    def __init__(self, status: int, url: str, message: str = ""):
        super().__init__(f"GitHub API {status} for {url}: {message[:300]}")
        self.status = status
        self.url = url


class LogUnavailable(Exception):
    """The job log cannot be downloaded (404 not found / 410 expired)."""

    def __init__(self, status: int):
        super().__init__(f"job log unavailable (HTTP {status})")
        self.status = status


@dataclass
class RunWindow:
    """Result of enumerating the runs created inside ``[lo, hi]``."""

    lo: datetime
    hi: datetime
    runs: list[dict[str, Any]]
    total_count: int
    complete: bool  # unique ids collected == total_count for every (sub)window


def _fmt(ts: datetime) -> str:
    """API timestamp for ``ts``.  Naive datetimes in this module are UTC (see ``parse_ts``): they must never be
    reinterpreted as local time, which shifted every run-listing window by the machine's UTC offset."""
    ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
    return ts.strftime(_TS_FMT)


def parse_ts(value: str | None) -> datetime | None:
    """Parse GitHub's ``YYYY-MM-DDTHH:MM:SSZ`` into a naive-UTC datetime (None if absent)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FMT)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            return None


def seconds_between(start: str | None, end: str | None) -> float | None:
    """Duration derived from two API timestamps; ``None`` when either is missing."""
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        return None
    return max((b - a).total_seconds(), 0.0)


class JobLogStream:
    """Streaming job log: iterate decoded lines; ``bytes`` counts what was read."""

    def __init__(self, resp: Any):
        self._resp = resp
        self.bytes = 0

    def __iter__(self) -> Iterator[str]:
        try:
            for raw in self._resp.iter_lines(chunk_size=1 << 16):
                self.bytes += len(raw) + 1
                yield raw.decode("utf-8", "replace")
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:  # pragma: no cover - best effort
            pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GithubClient:
    """GitHub REST API client with rate-limit handling, retries and pagination."""

    def __init__(
        self,
        token: str | None = None,
        repo: str | None = None,
        api_base: str = "https://api.github.com",
        per_page: int = 100,
        timeout: int = 60,
        session: Any = None,
        max_retries: int = 8,  # 2+4+...+120 s of backoff ≈ 6 minutes of tolerated outage
        backoff_base: float = 2.0,
        rate_limit_floor: int = 25,
        secondary_backoff: float = 60.0,
        network_patience_s: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.token = token
        self.repo = repo  # "owner/repo"
        self.api_base = api_base.rstrip("/")
        self.per_page = per_page
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.rate_limit_floor = rate_limit_floor
        self.secondary_backoff = secondary_backoff
        # how long to keep waiting, after the retries above are used up, for the network (DNS) to come back before raising; 0 = raise at once (the default for library
        # use and tests); the CLI passes GHA_NETWORK_PATIENCE_S (default 6 h) so that a long outage pauses a multi-hour ingest / live scorer instead of killing it
        self.network_patience_s = float(os.environ.get("GHA_NETWORK_PATIENCE_S", "0") or 0) if network_patience_s is None else network_patience_s
        self._sleep = sleep_fn
        self._clock = clock
        self.stats: Counter[str] = Counter()
        self._rl_remaining: int | None = None
        self._rl_reset: float | None = None

        if session is not None:
            self._session = session
        else:
            import requests  # lazy: only live access needs it

            self._session = requests.Session()
            from requests.adapters import HTTPAdapter

            adapter = HTTPAdapter(pool_connections=8, pool_maxsize=32)  # concurrent log downloads
            self._session.mount("https://", adapter)
            if token:
                self._session.headers["Authorization"] = f"Bearer {token}"
            self._session.headers["Accept"] = "application/vnd.github+json"
            self._session.headers["X-GitHub-Api-Version"] = API_VERSION
            self._session.headers["User-Agent"] = "gha-optimizer-ingestion"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _require_repo(self) -> str:
        if not self.repo:
            raise ValueError("No repository configured (expected 'owner/repo').")
        return self.repo

    def _respect_primary_limit(self) -> None:
        if (
            self._rl_remaining is not None
            and self._rl_remaining <= self.rate_limit_floor
            and self._rl_reset is not None
        ):
            wait = self._rl_reset - self._clock() + 1.0
            if wait > 0:
                self._note_wait("primary", wait)
                self._sleep(wait)
            self._rl_remaining = None

    def _note_wait(self, kind: str, seconds: float) -> None:
        self.stats["rate_limit_waits"] += 1
        self.stats[f"rate_limit_waits_{kind}"] += 1
        self.stats["rate_limit_wait_seconds"] += int(seconds)
        logger.warning("rate limit (%s): sleeping %.0fs", kind, seconds)

    def _update_rate_state(self, resp: Any) -> None:
        h = resp.headers
        try:
            if h.get("X-RateLimit-Remaining") is not None:
                self._rl_remaining = int(h["X-RateLimit-Remaining"])
            if h.get("X-RateLimit-Reset") is not None:
                self._rl_reset = float(h["X-RateLimit-Reset"])
        except (TypeError, ValueError):
            pass

    def _classify_403(self, resp: Any, rate_waits: int) -> tuple[str, float]:
        """Return ``(kind, wait_seconds)``; kind is primary | secondary | forbidden."""
        h = resp.headers
        try:
            body = (resp.text or "")[:2000].lower()
        except Exception:
            body = ""
        remaining = h.get("X-RateLimit-Remaining")
        if remaining is not None and str(remaining) == "0":
            reset = float(h.get("X-RateLimit-Reset", self._clock() + 60))
            return "primary", max(reset - self._clock() + 1.0, 1.0)
        if h.get("Retry-After"):
            try:
                return "secondary", float(h["Retry-After"]) + 1.0
            except ValueError:
                pass
        if "secondary rate limit" in body or "abuse" in body:
            return "secondary", self.secondary_backoff * (2 ** min(rate_waits, 4))
        if "rate limit" in body:
            return "primary", 60.0
        if resp.status_code == 429:
            return "secondary", self.secondary_backoff * (2 ** min(rate_waits, 4))
        return "forbidden", 0.0

    def _wait_for_network(self) -> bool:
        """After the retries are used up: poll DNS once a minute for up to ``network_patience_s``; True when the API host resolves again."""
        if self.network_patience_s <= 0:
            return False
        import socket
        from urllib.parse import urlparse

        host = urlparse(self.api_base).hostname or "api.github.com"
        start = self._clock()
        logger.warning("network unreachable: waiting up to %.0f s for %s to resolve again", self.network_patience_s, host)
        while self._clock() - start < self.network_patience_s:
            self._sleep(60.0)
            try:
                socket.getaddrinfo(host, 443)
            except OSError:
                continue
            self.stats["network_waits"] += 1
            logger.warning("network is back after %.0f s", self._clock() - start)
            return True
        return False

    def _backoff(self, attempt: int, why: str) -> None:
        delay = min(self.backoff_base ** attempt, 120.0)
        self.stats["retries"] += 1
        logger.warning("retrying after %s (attempt %d) in %.0fs", why, attempt, delay)
        self._sleep(delay)

    def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        stream: bool = False,
        ok_statuses: tuple[int, ...] = (),
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        attempt = 0
        rate_waits = 0
        while True:
            self._respect_primary_limit()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout, stream=stream)
            except Exception as exc:  # requests.ConnectionError / Timeout / chunked errors
                name = type(exc).__name__
                if name not in {"ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout",
                                "ChunkedEncodingError", "SSLError", "ProxyError"}:
                    raise
                attempt += 1
                if attempt > self.max_retries:
                    if self._wait_for_network():
                        attempt = 0
                        continue
                    raise GithubAPIError(0, url, f"{name}: {exc}") from exc
                self._backoff(attempt, name)
                continue

            self.stats["requests"] += 1
            self._update_rate_state(resp)
            status = resp.status_code
            if 200 <= status < 300 or status in ok_statuses:
                return resp

            if status in (403, 429):
                kind, wait = self._classify_403(resp, rate_waits)
                if kind != "forbidden":
                    rate_waits += 1
                    resp.close()
                    if rate_waits > 12:
                        raise GithubAPIError(status, url, f"rate limited ({kind}) too many times")
                    self._note_wait(kind, wait)
                    self._sleep(wait)
                    self._rl_remaining = None
                    continue
            if status >= 500:
                attempt += 1
                resp.close()
                if attempt > self.max_retries:
                    raise GithubAPIError(status, url, "server error after retries")
                self._backoff(attempt, f"HTTP {status}")
                continue

            try:
                message = resp.text
            except Exception:
                message = ""
            resp.close()
            raise GithubAPIError(status, url, message)

    def get_json(
        self, path: str, params: dict[str, Any] | None = None, ok_statuses: tuple[int, ...] = ()
    ) -> Any:
        """GET and decode JSON; returns ``None`` when the status is in ``ok_statuses``."""
        resp = self._request(path, params, ok_statuses=ok_statuses)
        if resp.status_code in ok_statuses and not (200 <= resp.status_code < 300):
            return None
        return resp.json()

    def paginate(
        self, path: str, key: str | None, params: dict[str, Any] | None = None, max_pages: int = 100
    ) -> Iterator[Any]:
        """Yield items across pages, following ``Link: rel="next"``."""
        params = dict(params or {})
        params.setdefault("per_page", self.per_page)
        url: str | None = path
        first = True
        pages = 0
        while url and pages < max_pages:
            resp = self._request(url, params if first else None)
            first = False
            pages += 1
            data = resp.json()
            items = data.get(key, []) if (key and isinstance(data, dict)) else data
            yield from items
            url = (resp.links or {}).get("next", {}).get("url")

    def rate_limit_status(self) -> dict[str, Any]:
        return self.get_json("/rate_limit")["resources"]["core"]

    # ------------------------------------------------------------------
    # Repository / workflows
    # ------------------------------------------------------------------

    def get_repo(self) -> dict[str, Any]:
        return self.get_json(f"/repos/{self._require_repo()}")

    def list_workflows(self) -> list[dict[str, Any]]:
        """``GET /repos/{o}/{r}/actions/workflows`` — id, name, path, state."""
        return list(self.paginate(f"/repos/{self._require_repo()}/actions/workflows", "workflows"))

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def collect_runs(
        self,
        lo: datetime,
        hi: datetime,
        workflow_id: int | None = None,
        min_span: timedelta = timedelta(minutes=1),
    ) -> RunWindow:
        """All runs created in ``[lo, hi]`` (UTC), bisecting past the listing cap.

        ``complete`` is True only if, for every (sub)window, the number of unique
        run ids collected equals the API's ``total_count``.
        """
        repo = self._require_repo()
        path = (
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
            if workflow_id is not None
            else f"/repos/{repo}/actions/runs"
        )
        created = f"{_fmt(lo)}..{_fmt(hi)}"
        first = self.get_json(path, {"created": created, "per_page": self.per_page, "page": 1})
        total = int(first.get("total_count", 0))
        if total == 0:
            return RunWindow(lo, hi, [], 0, True)

        if total >= LISTING_CAP and (hi - lo) > min_span:
            mid = lo + (hi - lo) / 2
            left = self.collect_runs(lo, mid, workflow_id, min_span)
            right = self.collect_runs(mid + timedelta(seconds=1), hi, workflow_id, min_span)
            by_id = {r["id"]: r for r in left.runs}
            by_id.update({r["id"]: r for r in right.runs})
            return RunWindow(lo, hi, list(by_id.values()), left.total_count + right.total_count,
                             left.complete and right.complete)

        by_id: dict[int, dict[str, Any]] = {r["id"]: r for r in first.get("workflow_runs", [])}
        for attempt in range(2):  # the second pass guards against an inconsistent listing
            page = 2 if attempt == 0 else 1
            while len(by_id) < total and page <= LISTING_CAP // self.per_page + 1:
                data = self.get_json(path, {"created": created, "per_page": self.per_page, "page": page})
                runs = data.get("workflow_runs", [])
                if not runs:
                    break
                by_id.update({r["id"]: r for r in runs})
                page += 1
            if len(by_id) >= total:
                break
            self.stats["listing_retries"] += 1
        complete = len(by_id) >= total
        if not complete:
            self.stats["incomplete_windows"] += 1
            logger.warning("window %s: collected %d of total_count=%d", created, len(by_id), total)
        return RunWindow(lo, hi, list(by_id.values()), total, complete)

    def get_run(self, run_id: int | str) -> dict[str, Any]:
        return self.get_json(f"/repos/{self._require_repo()}/actions/runs/{run_id}")

    def list_jobs(self, run_id: int | str) -> list[dict[str, Any]]:
        """Jobs of the **latest attempt** of a run, paginated (per_page=100)."""
        return list(
            self.paginate(
                f"/repos/{self._require_repo()}/actions/runs/{run_id}/jobs",
                "jobs",
                {"filter": "latest"},
            )
        )

    # ------------------------------------------------------------------
    # Commits / diffs
    # ------------------------------------------------------------------

    def _files_endpoint(self, path: str, max_pages: int = 30) -> dict[str, Any] | None:
        resp = self._request(path, {"per_page": self.per_page, "page": 1}, ok_statuses=(404, 422))
        if resp.status_code in (404, 422):
            return None
        data = resp.json()
        files = list(data.get("files", []))
        url = (resp.links or {}).get("next", {}).get("url")
        pages = 1
        while url and pages < max_pages:
            resp = self._request(url)
            files.extend(resp.json().get("files", []))
            url = (resp.links or {}).get("next", {}).get("url")
            pages += 1
        data["files"] = files
        data["files_truncated"] = bool(url)
        return data

    def get_commit(self, sha: str) -> dict[str, Any] | None:
        """``GET /commits/{sha}`` with the ``files`` array fully paginated."""
        return self._files_endpoint(f"/repos/{self._require_repo()}/commits/{sha}")

    def compare(self, base: str, head: str) -> dict[str, Any] | None:
        """``GET /compare/{base}...{head}`` — the whole PR diff (merge-base...head)."""
        return self._files_endpoint(f"/repos/{self._require_repo()}/compare/{base}...{head}")

    def pulls_for_commit(self, sha: str) -> list[dict[str, Any]]:
        """``GET /commits/{sha}/pulls`` — pull requests containing the commit.

        Needed because a run's ``pull_requests[]`` is empty for pull requests from forks,
        which would otherwise leave the run with only its head commit's diff."""
        resp = self._request(f"/repos/{self._require_repo()}/commits/{sha}/pulls", {"per_page": self.per_page},
                             ok_statuses=(404, 422))
        return [] if resp.status_code in (404, 422) else list(resp.json())

    def get_file(self, path: str, ref: str) -> tuple[str, str] | None:
        """Contents API fallback: ``(blob_sha, text)`` or None if the file is absent."""
        data = self.get_json(
            f"/repos/{self._require_repo()}/contents/{path}", {"ref": ref}, ok_statuses=(404,)
        )
        if not data or "content" not in data:
            return None
        return data["sha"], base64.b64decode(data["content"]).decode("utf-8", "replace")

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def open_job_log(self, job_id: int | str) -> JobLogStream:
        """Stream a job log (``/jobs/{id}/logs`` -> 302 -> blob).  Raises
        :class:`LogUnavailable` for 404/410 (expired or never stored)."""
        resp = self._request(
            f"/repos/{self._require_repo()}/actions/jobs/{job_id}/logs",
            stream=True,
            ok_statuses=(404, 410),
        )
        if resp.status_code in (404, 410):
            status = resp.status_code
            resp.close()
            raise LogUnavailable(status)
        return JobLogStream(resp)
