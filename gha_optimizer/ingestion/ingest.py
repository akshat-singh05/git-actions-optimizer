"""
Resumable, idempotent ingestion of real GitHub Actions history into the database.

Pipeline per repository::

    repo + workflows ──► runs (day windows, bisected past the 1000 cap)
        └─ per completed run, each step idempotent and committed on its own:
             workflow YAML blob @ head sha ─► jobs + steps ─► changed files
             ─► API-job-name -> YAML-job-key mapping ─► job logs -> cache events

Guarantees
----------
* **Natural keys everywhere** (GitHub run id, job id, (job, step number), (run,
  filename), (job, event seq)) -> re-running never duplicates rows.
* **Incremental persistence**: every sub-step commits; a crash loses at most the
  step in flight.  Finished runs are recorded in ``ingestion_progress``
  (stage ``run_done``) and skipped on restart.
* **Logs are downloaded at most once**: ``job_log_fetches`` is a ledger.  Terminal
  statuses are never retried; ``error`` / ``skipped_budget`` are.  A run finished by an
  earlier ``--no-logs`` ingest is revisited (its logs back-filled, newest runs first,
  within ``--log-budget``) when ingestion is repeated with logs enabled.
* **Missing is NULL**: no log / no cache line / unmapped job leaves the cache
  label NULL and records *why* in ``cache_label_status``.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func
from sqlalchemy.orm import Session

from gha_optimizer.ingestion.github_client import (
    GithubAPIError,
    GithubClient,
    LogUnavailable,
    parse_ts,
    seconds_between,
)
from gha_optimizer.ingestion.job_mapping import (
    JobMatcher,
    build_matchers,
    job_is_cache_capable,
    match_job_key,
)
from gha_optimizer.ingestion.log_parser import PARSER_VERSION, derive_job_label, parse_log_lines
from gha_optimizer.ingestion.mirror import DEFAULT_MIRROR_ROOT, MirrorError, RepoMirror
from gha_optimizer.server.db import models as m

logger = logging.getLogger(__name__)

# ledger statuses
LOG_TERMINAL = {
    "parsed_events",
    "parsed_no_events",
    "expired",
    "unavailable",
    "skipped_not_cache_capable",
    "skipped_conclusion",
}
LOG_RETRY = {"error", "skipped_budget"}
LOG_CONCLUSIONS = ("success", "failure")
PR_EVENTS = ("pull_request", "pull_request_target")


def _parsed_by_older_parser(ledger: m.JobLogFetch) -> bool:
    """A log that yielded no cache line under an older parser is read again: the parser may recognise more now."""
    return ledger.status == "parsed_no_events" and ledger.parser_version != PARSER_VERSION


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class IngestOptions:
    since: datetime                      # naive UTC, inclusive
    until: datetime | None = None        # naive UTC, default = now
    workflows: list[str] | None = None   # paths / basenames / names; None = auto-select multi-job workflows
    max_runs: int | None = None          # stop once this many runs are fully processed (newest first)
    fetch_logs: bool = True
    log_budget: int | None = None        # max log downloads in this invocation
    log_policy: str = "cache-capable"    # or "all"
    use_mirror: bool = True
    mirror_root: Path = field(default_factory=lambda: DEFAULT_MIRROR_ROOT)
    progress_every: int = 10
    log_workers: int = 6                 # concurrent log downloads within a run (network-bound)
    mirror_refresh: bool = True          # fetch new commits into an existing mirror at start
    fetch_diffs: bool = True             # False = outcomes only (live-evaluate): jobs and their conclusions, no diff; such a run is NOT marked done, so a normal ingest completes it later


class RepoIngestor:
    def __init__(self, repo: str, client: GithubClient, session: Session, options: IngestOptions,
                 mirror: RepoMirror | None = None):
        self.repo = repo
        self.client = client
        self.db = session
        self.opts = options
        self.mirror = mirror
        self.counters: Counter[str] = Counter()
        self.workflows: dict[str, dict[str, Any]] = {}   # path -> API workflow dict (selected)
        self._yaml_cache: dict[str, dict[str, Any] | None] = {}
        self._matcher_cache: dict[str, list[JobMatcher]] = {}
        self._expired_age_days: float | None = None      # smallest run age (days) seen with an expired log
        self._log_downloads = 0
        self._done_at_start: int | None = None           # runs already finished before this invocation (see _cap_reached)
        self._started = time.time()
        self._last_report = 0.0
        self._ingestion_run: m.IngestionRun | None = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def _mark_dead_invocations(self) -> None:
        """A previous invocation still marked ``running`` was killed (it cannot have been
        finished by a process that no longer exists): record it as interrupted."""
        n = (
            self.db.query(m.IngestionRun)
            .filter_by(repo=self.repo, status="running")
            .update({"status": "interrupted", "finished_at": utcnow()})
        )
        if n:
            logger.info("marked %d earlier unfinished ingestion invocation(s) as interrupted", n)
        self.db.commit()

    def run(self) -> dict[str, Any]:
        self._mark_dead_invocations()
        self._ingestion_run = m.IngestionRun(
            repo=self.repo,
            status="running",
            args={
                "since": self.opts.since.isoformat(),
                "until": (self.opts.until or utcnow()).isoformat(),
                "workflows": self.opts.workflows,
                "max_runs": self.opts.max_runs,
                "fetch_logs": self.opts.fetch_logs,
                "log_budget": self.opts.log_budget,
                "log_policy": self.opts.log_policy,
                "use_mirror": self.opts.use_mirror,
            },
        )
        self.db.add(self._ingestion_run)
        self.db.commit()
        status = "complete"
        try:
            self._setup()
            self._ingest_windows()
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            self.db.rollback()
            self._finish(status)
        return dict(self.counters)

    def ingest_single_run(self, run_id: str | int, run_json: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ingest one run (e.g. from a ``workflow_run`` webhook): same idempotent steps as a window scan.

        ``run_json``: the run object when the caller already fetched it (saves one request).  The repository / workflow / mirror setup is done once
        per ingestor, so a loop over many runs with one ingestor does not repeat it."""
        self._ingestion_run = m.IngestionRun(repo=self.repo, status="running", args={"run_id": str(run_id)})
        self.db.add(self._ingestion_run)
        self.db.commit()
        status = "complete"
        try:
            if not self.workflows:
                self._setup()
            run_json = run_json or self.client.get_run(run_id)
            wf = next((w for w in self.workflows.values() if w["id"] == run_json.get("workflow_id")), None)
            if wf is None:
                self.counters["runs_ignored_workflow_not_selected"] += 1
                return dict(self.counters)
            repo_row = self.db.query(m.Repository).filter_by(full_name=self.repo).one()
            wf_row = self.db.query(m.Workflow).filter_by(repo_id=repo_row.id, file_path=wf["path"]).one_or_none()
            row = self._upsert_run(run_json, wf, wf_row)
            self.db.commit()
            self._process_run(row)
            return dict(self.counters)
        except Exception:
            status = "failed"
            raise
        finally:
            self.db.rollback()
            self._finish(status)

    def _finish(self, status: str) -> None:
        run = self._ingestion_run
        if run is None:
            return
        counters = dict(self.counters)
        counters.update({f"client_{k}": v for k, v in self.client.stats.items()})
        run.status = status
        run.finished_at = utcnow()
        run.counters = counters
        repo_row = self.db.query(m.Repository).filter_by(full_name=self.repo).one_or_none()
        if repo_row is not None:
            repo_row.last_ingested_at = utcnow()
        self.db.commit()
        logger.info("ingestion %s for %s: %s", status, self.repo, counters)

    # ------------------------------------------------------------------
    # Setup: repo row, workflows, mirror
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        budget = self.client.rate_limit_status()
        logger.info("GitHub API budget: %s/%s requests remaining, resets at %s",
                    budget.get("remaining"), budget.get("limit"), budget.get("reset"))
        meta = self.client.get_repo()
        repo_row = self.db.query(m.Repository).filter_by(full_name=self.repo).one_or_none()
        if repo_row is None:
            repo_row = m.Repository(full_name=self.repo)
            self.db.add(repo_row)
        repo_row.default_branch = meta.get("default_branch")
        repo_row.language = meta.get("language")
        repo_row.license_spdx = (meta.get("license") or {}).get("spdx_id")
        if self.opts.use_mirror and self.mirror is None:
            self.mirror = RepoMirror(self.repo, self.opts.mirror_root, token=getattr(self.client, "token", None))
        if self.mirror is not None:
            self.mirror.ensure(refresh=self.opts.mirror_refresh)
            repo_row.mirror_path = str(self.mirror.path)
        self.db.commit()
        logger.info("repo %s: license=%s language=%s default_branch=%s",
                    self.repo, repo_row.license_spdx, repo_row.language, repo_row.default_branch)

        all_wfs = [w for w in self.client.list_workflows() if w.get("state") == "active"]
        selected = self._select_workflows(all_wfs)
        self.workflows = {w["path"]: w for w in selected}
        for w in selected:
            row = self.db.query(m.Workflow).filter_by(repo_id=repo_row.id, file_path=w["path"]).one_or_none()
            if row is None:
                self.db.add(m.Workflow(repo_id=repo_row.id, name=w["name"], file_path=w["path"]))
        self.db.commit()
        self.counters["workflows_selected"] = len(selected)
        logger.info("selected workflows: %s", [w["path"] for w in selected])
        self._upgrade_legacy_diffs()
        self._remap_unmapped_jobs()

    def _head_yaml_jobs(self, path: str) -> dict[str, Any] | None:
        """``jobs:`` mapping of the workflow at the default branch tip (mirror first, API fallback)."""
        text: str | None = None
        if self.mirror is not None and self.mirror.exists():
            got = self.mirror.read_file("HEAD", path)
            text = got[1].decode("utf-8", "replace") if got else None
        if text is None:
            repo_row = self.db.query(m.Repository).filter_by(full_name=self.repo).one_or_none()
            ref = (repo_row.default_branch if repo_row else None) or "HEAD"
            got_api = self.client.get_file(path, ref)
            text = got_api[1] if got_api else None
        if text is None:
            return None
        return _jobs_of(text)

    def _select_workflows(self, wfs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.opts.workflows:
            wanted = {w.lower() for w in self.opts.workflows}
            sel = [
                w for w in wfs
                if w["path"].lower() in wanted
                or Path(w["path"]).name.lower() in wanted
                or w["name"].lower() in wanted
            ]
            found = {x for w in sel for x in (w["path"].lower(), Path(w["path"]).name.lower(), w["name"].lower())}
            for miss in sorted(wanted - found):
                logger.warning("requested workflow %r not found among active workflows", miss)
            return sel
        selected = []
        for w in wfs:
            jobs = self._head_yaml_jobs(w["path"])
            if not jobs:
                continue
            needs = sum(
                len([n] if isinstance(n, str) else list(n or []))
                for body in jobs.values() if isinstance(body, dict)
                for n in [body.get("needs")] if n
            )
            if len(jobs) >= 2 and needs >= 1:
                selected.append(w)
        return selected

    # ------------------------------------------------------------------
    # Run enumeration (newest day first so --max-runs keeps the most recent window)
    # ------------------------------------------------------------------

    def _windows(self) -> list[tuple[datetime, datetime]]:
        until = self.opts.until or utcnow()
        day = datetime(until.year, until.month, until.day)
        out: list[tuple[datetime, datetime]] = []
        while day + timedelta(days=1) > self.opts.since:
            lo = max(day, self.opts.since)
            hi = min(day + timedelta(days=1) - timedelta(seconds=1), until)
            if lo <= hi:
                out.append((lo, hi))
            day -= timedelta(days=1)
        return out

    def _done_count(self) -> int:
        return (
            self.db.query(func.count(m.IngestionProgress.id))
            .join(m.WorkflowRun, m.WorkflowRun.run_id == m.IngestionProgress.key)
            .filter(
                m.IngestionProgress.repo == self.repo,
                m.IngestionProgress.stage == "run_done",
                m.WorkflowRun.repo == self.repo,
            )
            .scalar()
            or 0
        )

    def _ingest_windows(self) -> None:
        repo_row = self.db.query(m.Repository).filter_by(full_name=self.repo).one()
        for lo, hi in self._windows():
            for path, wf in self.workflows.items():
                if self._cap_reached():
                    logger.info("max_runs=%s reached; stopping", self.opts.max_runs)
                    return
                self._scan_window(repo_row, wf, lo, hi)
                self._process_window_runs(wf, lo, hi)

    def _cap_reached(self) -> bool:
        """``max_runs`` bounds the runs fully processed by THIS invocation (runs finished by earlier invocations do not
        count; counting them made ``--max-runs`` a no-op on any repository that already held that many runs)."""
        if self.opts.max_runs is None:
            return False
        if self._done_at_start is None:
            self._done_at_start = self._done_count()
        return self._done_count() - self._done_at_start >= self.opts.max_runs

    def _scan_window(self, repo_row: m.Repository, wf: dict[str, Any], lo: datetime, hi: datetime) -> None:
        key = f"{wf['id']}:{lo:%Y-%m-%d}"
        prog = self.db.query(m.IngestionProgress).filter_by(repo=self.repo, stage="runs", key=key).one_or_none()
        if prog is not None and prog.status == "done":
            self.counters["windows_skipped_done"] += 1
            return
        window = self.client.collect_runs(lo, hi, wf["id"])
        self.counters["windows_scanned"] += 1
        wf_row = self.db.query(m.Workflow).filter_by(repo_id=repo_row.id, file_path=wf["path"]).one_or_none()
        for r in window.runs:
            self._upsert_run(r, wf, wf_row)
        # A run can be re-run (new attempt, new jobs) up to ~30 days later, so a window is
        # only final once it is older than that; younger windows are cheaply re-listed.
        final = window.complete and hi < utcnow() - timedelta(days=30)
        status = "done" if final else ("incomplete" if not window.complete else "open")
        if prog is None:
            prog = m.IngestionProgress(repo=self.repo, stage="runs", key=key, status=status)
            self.db.add(prog)
        prog.status = status
        prog.detail = {"total_count": window.total_count, "collected": len(window.runs), "complete": window.complete}
        prog.updated_at = utcnow()
        self.db.commit()
        logger.info("window %s %s: %d runs (total_count=%d, complete=%s)",
                    wf["path"], key.split(":")[1], len(window.runs), window.total_count, window.complete)

    def _upsert_run(self, r: dict[str, Any], wf: dict[str, Any], wf_row: m.Workflow | None) -> m.WorkflowRun:
        run_id = str(r["id"])
        row = self.db.query(m.WorkflowRun).filter_by(run_id=run_id).one_or_none()
        new = row is None
        if new:
            row = m.WorkflowRun(run_id=run_id)
            self.db.add(row)
            self.counters["runs_new"] += 1
        self.counters["runs_seen"] += 1
        prs = r.get("pull_requests") or []
        old_attempt = row.run_attempt
        row.repo = self.repo
        row.workflow_id = wf_row.id if wf_row else None
        row.commit_sha = r.get("head_sha")
        row.conclusion = r.get("conclusion")
        row.timestamp = r.get("created_at")
        row.github_workflow_id = r.get("workflow_id")
        row.workflow_path = wf["path"]
        row.workflow_name = r.get("name")
        row.event = r.get("event")
        row.status = r.get("status")
        row.head_branch = r.get("head_branch")
        row.run_number = r.get("run_number")
        row.run_attempt = r.get("run_attempt")
        row.actor_login = (r.get("actor") or {}).get("login")
        row.triggering_actor_login = (r.get("triggering_actor") or {}).get("login")
        row.gh_created_at = parse_ts(r.get("created_at"))
        row.gh_run_started_at = parse_ts(r.get("run_started_at"))
        row.gh_updated_at = parse_ts(r.get("updated_at"))
        if prs:
            row.pr_number = prs[0].get("number")
            row.pr_base_sha = (prs[0].get("base") or {}).get("sha")
        if not new and old_attempt is not None and row.run_attempt != old_attempt:
            self._reset_run(row, old_attempt)  # the run was re-run: previous attempt's jobs are stale
        return row

    def _reset_run(self, run: m.WorkflowRun, old_attempt: int) -> None:
        logger.info("run %s was re-run (attempt %s -> %s): resetting job data", run.run_id, old_attempt, run.run_attempt)
        self._purge_jobs(run.run_id)
        run.jobs_fetched_at = None
        self.db.query(m.IngestionProgress).filter_by(repo=self.repo, stage="run_done", key=run.run_id).delete()

    def _purge_jobs(self, run_id: str) -> None:
        ids = [j[0] for j in self.db.query(m.WorkflowJob.github_job_id).filter_by(run_id=run_id).all()]
        if ids:
            self.db.query(m.JobCacheEvent).filter(m.JobCacheEvent.github_job_id.in_(ids)).delete(synchronize_session=False)
            self.db.query(m.JobLogFetch).filter(m.JobLogFetch.github_job_id.in_(ids)).delete(synchronize_session=False)
            self.db.query(m.WorkflowStep).filter(m.WorkflowStep.github_job_id.in_(ids)).delete(synchronize_session=False)
        self.db.query(m.WorkflowJob).filter_by(run_id=run_id).delete(synchronize_session=False)

    # ------------------------------------------------------------------
    # Per-run processing
    # ------------------------------------------------------------------

    def _process_window_runs(self, wf: dict[str, Any], lo: datetime, hi: datetime) -> None:
        runs = (
            self.db.query(m.WorkflowRun)
            .filter(
                m.WorkflowRun.repo == self.repo,
                m.WorkflowRun.workflow_path == wf["path"],
                m.WorkflowRun.gh_created_at >= lo,
                m.WorkflowRun.gh_created_at <= hi,
            )
            .order_by(m.WorkflowRun.gh_created_at.desc())
            .all()
        )
        for run in runs:
            if self._cap_reached():
                return
            try:
                self._process_run(run)
            except GithubAPIError as exc:
                if exc.status in (0, 401):  # network down / bad token: stop; a restart resumes
                    raise
                self.db.rollback()
                self.counters["failures"] += 1
                logger.error("run %s failed: %s", run.run_id, exc)

    def _is_done(self, run_id: str) -> bool:
        return (
            self.db.query(m.IngestionProgress.id)
            .filter_by(repo=self.repo, stage="run_done", key=run_id)
            .first()
            is not None
        )

    def _logs_incomplete(self, run: m.WorkflowRun) -> bool:
        """True if a job whose log could give a cache label has no terminal ledger entry.

        A run finished by an earlier ``--no-logs`` ingest (or one whose logs were ``skipped_budget`` /
        ``error``) is therefore revisited when logs are requested, and only then.
        """
        for job in self.db.query(m.WorkflowJob).filter_by(run_id=run.run_id).all():
            if job.conclusion not in LOG_CONCLUSIONS:
                continue
            ledger = self.db.get(m.JobLogFetch, job.github_job_id)
            if ledger is None or ledger.status in LOG_RETRY or _parsed_by_older_parser(ledger):
                return True
        return False

    def _process_run(self, run: m.WorkflowRun) -> None:
        if self._is_done(run.run_id) and not (self.opts.fetch_logs and self._logs_incomplete(run)):
            return
        if run.status != "completed":
            if run.gh_created_at and run.gh_created_at < utcnow() - timedelta(hours=2):
                fresh = self.client.get_run(run.run_id)  # may have finished since the listing
                run.status = fresh.get("status")
                run.conclusion = fresh.get("conclusion")
                run.gh_updated_at = parse_ts(fresh.get("updated_at"))
                self.db.commit()
            if run.status != "completed":
                self.counters["runs_skipped_incomplete"] += 1
                return
        jobs_yaml = self._ensure_workflow_blob(run)
        self._ensure_jobs(run)
        if self.opts.fetch_diffs:
            self._ensure_diff(run)
        self._map_job_keys(run, jobs_yaml)
        if self.opts.fetch_logs:
            self._ensure_logs(run, jobs_yaml)
        if self._finalize(run):
            self.counters["runs_processed"] += 1
        self._report()

    # --- workflow blob -------------------------------------------------

    def _ensure_workflow_blob(self, run: m.WorkflowRun) -> dict[str, Any] | None:
        if run.workflow_blob_sha and run.workflow_blob_sha != "unavailable":
            return self._load_blob_jobs(run.workflow_blob_sha)
        if run.workflow_blob_sha == "unavailable":
            return None
        sha, text = None, None
        if self.mirror is not None:
            try:
                if self.mirror.fetch_sha(run.commit_sha):
                    got = self.mirror.read_file(run.commit_sha, run.workflow_path)
                    if got:
                        sha, text = got[0], got[1].decode("utf-8", "replace")
            except MirrorError as exc:
                logger.warning("mirror read failed for %s: %s", run.commit_sha, exc)
        if sha is None:
            got2 = self.client.get_file(run.workflow_path, run.commit_sha)
            if got2:
                sha, text = got2
        if sha is None or text is None:
            run.workflow_blob_sha = "unavailable"
            self.db.commit()
            self.counters["blobs_unavailable"] += 1
            return None
        if self.db.query(m.WorkflowBlob.id).filter_by(repo=self.repo, blob_sha=sha).first() is None:
            self.db.add(m.WorkflowBlob(repo=self.repo, blob_sha=sha, path=run.workflow_path, content=text))
            self.counters["blobs_stored"] += 1
        run.workflow_blob_sha = sha
        self.db.commit()
        self._yaml_cache[sha] = _jobs_of(text)
        return self._yaml_cache[sha]

    def _load_blob_jobs(self, blob_sha: str) -> dict[str, Any] | None:
        if blob_sha not in self._yaml_cache:
            blob = self.db.query(m.WorkflowBlob).filter_by(repo=self.repo, blob_sha=blob_sha).one_or_none()
            self._yaml_cache[blob_sha] = _jobs_of(blob.content) if blob and blob.content else None
        return self._yaml_cache[blob_sha]

    # --- jobs + steps --------------------------------------------------

    def _ensure_jobs(self, run: m.WorkflowRun) -> None:
        if run.jobs_fetched_at is not None:
            return
        api_jobs = self.client.list_jobs(run.run_id)
        new_ids = {j["id"] for j in api_jobs}
        stale = [
            j[0] for j in self.db.query(m.WorkflowJob.github_job_id).filter_by(run_id=run.run_id).all()
            if j[0] not in new_ids
        ]
        if stale:  # a newer attempt replaced these jobs
            self._purge_jobs(run.run_id)
            self.db.flush()
        names: set[str] = set()
        for j in api_jobs:
            row = self.db.query(m.WorkflowJob).filter_by(github_job_id=j["id"]).one_or_none()
            if row is None:
                row = m.WorkflowJob(github_job_id=j["id"], run_id=run.run_id)
                self.db.add(row)
            name = j.get("name") or f"job-{j['id']}"
            legacy = name if name not in names else f"{name}#{j['id']}"
            names.add(legacy)
            row.job_id = legacy
            row.job_name = name
            row.status = j.get("status")
            row.conclusion = j.get("conclusion")
            row.gh_created_at = parse_ts(j.get("created_at"))
            row.started_at = parse_ts(j.get("started_at"))
            row.completed_at = parse_ts(j.get("completed_at"))
            row.duration_seconds = seconds_between(j.get("started_at"), j.get("completed_at"))
            row.queue_seconds = seconds_between(j.get("created_at"), j.get("started_at"))
            row.runner_name = j.get("runner_name")
            row.runner_group_name = j.get("runner_group_name")
            row.labels = j.get("labels")
            row.runner_label = (j.get("labels") or [None])[0]
            row.run_attempt = j.get("run_attempt")
            self.counters["jobs_upserted"] += 1
            self.db.flush()
            for s in j.get("steps") or []:
                srow = self.db.query(m.WorkflowStep).filter_by(github_job_id=j["id"], number=s["number"]).one_or_none()
                if srow is None:
                    srow = m.WorkflowStep(github_job_id=j["id"], number=s["number"])
                    self.db.add(srow)
                srow.name = s.get("name")
                srow.status = s.get("status")
                srow.conclusion = s.get("conclusion")
                srow.started_at = parse_ts(s.get("started_at"))
                srow.completed_at = parse_ts(s.get("completed_at"))
                srow.duration_seconds = seconds_between(s.get("started_at"), s.get("completed_at"))
                self.counters["steps_upserted"] += 1
        run.jobs_total = len(api_jobs)
        run.jobs_fetched_at = utcnow()
        self.db.commit()

    # --- diff ----------------------------------------------------------

    def _resolve_pr(self, run: m.WorkflowRun) -> None:
        """Find the PR of a PR-event run whose ``pull_requests[]`` was empty (fork PRs).

        ``commits/{sha}/pulls`` only returns *open* PRs for commits that are not on the default
        branch, so runs of already merged/closed PRs usually stay unresolved (-> git fallback)."""
        donor = (
            self.db.query(m.WorkflowRun)
            .filter(m.WorkflowRun.repo == self.repo, m.WorkflowRun.commit_sha == run.commit_sha,
                    m.WorkflowRun.pr_base_sha.isnot(None))
            .first()
        )
        if donor is not None:
            run.pr_number, run.pr_base_sha = donor.pr_number, donor.pr_base_sha
            return
        prs = [
            p for p in self.client.pulls_for_commit(run.commit_sha)
            if ((p.get("base") or {}).get("repo") or {}).get("full_name", self.repo) == self.repo
        ]
        self.counters["pr_lookups"] += 1
        if prs:
            prs.sort(key=lambda p: (p.get("state") != "open", p.get("number", 0)))
            run.pr_number = prs[0].get("number")
            run.pr_base_sha = (prs[0].get("base") or {}).get("sha")

    def _git_pr_diff(self, run: m.WorkflowRun) -> list[dict[str, Any]] | None:
        """PR diff from the git mirror, no API call: ``merge-base(default-branch tip at run time, head)..head``.

        This is what "files changed in the PR" meant when the run started, even if the PR has
        since been merged or closed.  ``None`` when the commit is not in the mirror."""
        if self.mirror is None or not self.mirror.exists() or not run.commit_sha or run.gh_created_at is None:
            return None
        try:
            if not self.mirror.fetch_sha(run.commit_sha):
                return None
            repo_row = self.db.query(m.Repository).filter_by(full_name=self.repo).one_or_none()
            branch = (repo_row.default_branch if repo_row else None) or "HEAD"
            tip = self.mirror.commit_before(branch, run.gh_created_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
            base = self.mirror.merge_base(tip, run.commit_sha) if tip else None
            if base is None:
                return None
            run.diff_base_sha = base
            return self.mirror.diff_files(base, run.commit_sha)
        except MirrorError as exc:
            logger.warning("git PR diff failed for %s: %s", run.commit_sha[:8], exc)
            return None

    def _upgrade_legacy_diffs(self) -> None:
        """Reopen PR-event runs whose diff is only the head commit's (``commit`` from before the PR
        lookup existed, or ``commit_no_pr`` from before the git fallback existed)."""
        ids = [r for (r,) in self.db.query(m.WorkflowRun.run_id).filter(
            m.WorkflowRun.repo == self.repo, m.WorkflowRun.event.in_(PR_EVENTS),
            m.WorkflowRun.diff_source.in_(("commit", "commit_no_pr")))]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self.db.query(m.IngestionProgress).filter(
                m.IngestionProgress.repo == self.repo, m.IngestionProgress.stage == "run_done",
                m.IngestionProgress.key.in_(chunk)).delete(synchronize_session=False)
        if ids:
            logger.info("reopened %d PR-event runs for the PR-diff upgrade", len(ids))
        self.db.commit()

    def _remap_unmapped_jobs(self) -> None:
        """Re-run the job-name -> YAML-key mapping for already-ingested jobs that have no key
        (the mapping rules improve over time).  Database-only: no API calls, ledger untouched."""
        runs = (
            self.db.query(m.WorkflowRun)
            .join(m.WorkflowJob, m.WorkflowJob.run_id == m.WorkflowRun.run_id)
            .filter(m.WorkflowRun.repo == self.repo, m.WorkflowJob.job_key.is_(None))
            .distinct()
            .all()
        )
        remapped = 0
        for run in runs:
            if not run.workflow_blob_sha or run.workflow_blob_sha == "unavailable":
                continue
            before = self.db.query(m.WorkflowJob).filter_by(run_id=run.run_id, job_key=None).count()
            self._map_job_keys(run, self._load_blob_jobs(run.workflow_blob_sha))
            remapped += before - self.db.query(m.WorkflowJob).filter_by(run_id=run.run_id, job_key=None).count()
        if remapped:
            logger.info("remapped %d previously unmapped job cells to YAML job keys", remapped)
            self.counters["jobs_remapped"] += remapped

    def _store_diff(self, run: m.WorkflowRun, files: list[dict[str, Any]], source: str, base: str | None) -> None:
        for f in files:
            self.db.add(m.RunChangedFile(
                run_id=run.run_id, filename=f["filename"], status=f.get("status"),
                additions=f.get("additions"), deletions=f.get("deletions"),
                previous_filename=f.get("previous_filename"),
            ))
        run.diff_source = source
        run.diff_base_sha = base
        run.diff_files_total = len(files)
        run.loc_added = sum(int(f.get("additions") or 0) for f in files)
        run.loc_deleted = sum(int(f.get("deletions") or 0) for f in files)
        self.db.commit()

    def _ensure_diff(self, run: m.WorkflowRun) -> None:
        """What the run changed.  ``diff_source`` records how it was obtained:

        * ``commit``         non-PR event: the head commit's diff
        * ``compare_pr``     PR known (``pull_requests[]`` or the commit->PR lookup): API compare base...head
        * ``git_merge_base`` PR event, PR not resolvable: ``merge-base(default tip at run time, head)..head`` from the mirror
        * ``commit_only``    PR event, neither worked: head-commit diff only (an approximation)
        * ``unavailable``    nothing could be fetched
        """
        is_pr_event = run.event in PR_EVENTS
        if run.diff_source is not None and not (is_pr_event and run.diff_source in ("commit", "commit_no_pr")):
            return
        if is_pr_event and not run.pr_base_sha:
            self._resolve_pr(run)
        use_compare = is_pr_event and bool(run.pr_base_sha)
        # (re-)diff: drop anything derived from a previous, less accurate diff of this run
        self.db.query(m.RunChangedFile).filter_by(run_id=run.run_id).delete(synchronize_session=False)
        self.db.query(m.RunGraphFeatures).filter_by(run_id=run.run_id).delete(synchronize_session=False)

        if use_compare:
            donor_source = "compare_pr"
        elif is_pr_event:
            donor_source = None  # decided below (git first, head commit as the last resort)
        else:
            donor_source = "commit"
        if donor_source is not None:
            donor = (
                self.db.query(m.WorkflowRun)
                .filter(m.WorkflowRun.repo == self.repo, m.WorkflowRun.commit_sha == run.commit_sha,
                        m.WorkflowRun.diff_source == donor_source, m.WorkflowRun.run_id != run.run_id,
                        (m.WorkflowRun.pr_base_sha == run.pr_base_sha) if use_compare else True)
                .first()
            )
            if donor is not None:
                files = [
                    {"filename": f.filename, "status": f.status, "additions": f.additions, "deletions": f.deletions,
                     "previous_filename": f.previous_filename}
                    for f in self.db.query(m.RunChangedFile).filter_by(run_id=donor.run_id).all()
                ]
                self._store_diff(run, files, donor_source, donor.diff_base_sha)
                self.counters["diffs_reused"] += 1
                return

        if use_compare:
            data = self.client.compare(run.pr_base_sha, run.commit_sha)
            self.counters["diffs_fetched"] += 1
            if data is not None:
                self._store_diff(run, data.get("files", []), "compare_pr", run.pr_base_sha)
                return
        elif is_pr_event:
            files = self._git_pr_diff(run)
            if files is not None:
                self._store_diff(run, files, "git_merge_base", run.diff_base_sha)
                self.counters["diffs_from_git"] += 1
                return

        data = self.client.get_commit(run.commit_sha)  # non-PR events, or the last resort for PR events
        self.counters["diffs_fetched"] += 1
        if data is None:
            run.diff_source = "unavailable"
            self.db.commit()
            return
        parent = (data.get("parents") or [{}])[0].get("sha")
        self._store_diff(run, data.get("files", []), "commit_only" if is_pr_event else "commit", parent)

    # --- job key mapping ----------------------------------------------

    def _matchers(self, run: m.WorkflowRun, jobs_yaml: dict[str, Any] | None) -> list[JobMatcher]:
        sha = run.workflow_blob_sha or ""
        if sha not in self._matcher_cache:
            self._matcher_cache[sha] = build_matchers(jobs_yaml) if jobs_yaml else []
        return self._matcher_cache[sha]

    def _map_job_keys(self, run: m.WorkflowRun, jobs_yaml: dict[str, Any] | None) -> None:
        matchers = self._matchers(run, jobs_yaml)
        changed = False
        for job in self.db.query(m.WorkflowJob).filter_by(run_id=run.run_id).all():
            key = match_job_key(job.job_name or job.job_id, matchers) if matchers else None
            new_source = "blob_at_sha" if key else "unmapped"
            if job.job_key != key or job.mapping_source != new_source:
                job.job_key, job.mapping_source = key, new_source
                changed = True
        if changed:
            self.db.commit()

    # --- logs ----------------------------------------------------------

    def _run_age_days(self, run: m.WorkflowRun) -> float:
        return (utcnow() - (run.gh_created_at or utcnow())).total_seconds() / 86400.0

    def _ensure_logs(self, run: m.WorkflowRun, jobs_yaml: dict[str, Any] | None) -> None:
        pending: list[m.WorkflowJob] = []
        for job in self.db.query(m.WorkflowJob).filter_by(run_id=run.run_id).order_by(m.WorkflowJob.github_job_id).all():
            ledger = self.db.get(m.JobLogFetch, job.github_job_id)
            if ledger is not None and ledger.status in LOG_TERMINAL and not _parsed_by_older_parser(ledger):
                continue
            reason = self._skip_reason(run, job, jobs_yaml)
            if reason is not None:
                self._write_ledger(job, run, reason)
                continue
            if self.opts.log_budget is not None and self._log_downloads + len(pending) >= self.opts.log_budget:
                self._write_ledger(job, run, "skipped_budget")
                self.counters["logs_skipped_budget"] += 1
                continue
            pending.append(job)
        if not pending:
            return

        ids = [j.github_job_id for j in pending]
        workers = max(1, min(self.opts.log_workers, len(ids)))
        if workers == 1:
            results = [self._fetch_log(i) for i in ids]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:  # network-bound; DB writes stay on this thread
                results = list(pool.map(self._fetch_log, ids))
        network_down = False
        for job, res in zip(pending, results):
            if res.status == "network":
                network_down = True
                continue
            self._apply_log_result(job, run, res)
        if network_down:
            raise GithubAPIError(0, "job logs", "network unavailable while downloading logs; restart to resume")

    def _skip_reason(self, run: m.WorkflowRun, job: m.WorkflowJob, jobs_yaml: dict[str, Any] | None) -> str | None:
        if job.conclusion not in LOG_CONCLUSIONS:
            return "skipped_conclusion"
        if self._expired_age_days is not None and self._run_age_days(run) >= self._expired_age_days:
            return "expired"
        if self.opts.log_policy == "all":
            return None
        body = (jobs_yaml or {}).get(job.job_key) if job.job_key else None
        api_steps = [
            {"name": s.name}
            for s in self.db.query(m.WorkflowStep).filter_by(github_job_id=job.github_job_id).all()
        ]
        capable = job_is_cache_capable(body if isinstance(body, dict) else None, api_steps)
        return None if capable else "skipped_not_cache_capable"

    def _write_ledger(self, job: m.WorkflowJob, run: m.WorkflowRun, status: str, **kw: Any) -> None:
        row = self.db.get(m.JobLogFetch, job.github_job_id)
        if row is None:
            row = m.JobLogFetch(github_job_id=job.github_job_id, run_id=run.run_id, status=status)
            self.db.add(row)
        row.status = status
        row.parser_version = PARSER_VERSION
        row.fetched_at = utcnow()
        for k, v in kw.items():
            setattr(row, k, v)
        if job.cache_hit is None:
            job.cache_label_status = status
        self.db.commit()

    def _fetch_log(self, job_id: int) -> "_LogResult":
        """Download + parse one job log.  Runs on worker threads: no DB access here."""
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                stream = self.client.open_job_log(job_id)
                parsed = parse_log_lines(stream)
                return _LogResult(job_id, "ok", 200, parsed, stream.bytes)
            except LogUnavailable as exc:
                return _LogResult(job_id, "expired" if exc.status == 410 else "unavailable", exc.status)
            except GithubAPIError as exc:
                if exc.status in (401, 403):
                    return _LogResult(job_id, "unavailable", exc.status)
                if exc.status == 0:  # the client already retried for minutes: the network is down
                    return _LogResult(job_id, "network", 0)
                last_err = exc
            except Exception as exc:  # error while streaming the body
                last_err = exc
            time.sleep(min(2 ** attempt, 10))
        logger.error("job %s log failed after retries: %s", job_id, last_err)
        return _LogResult(job_id, "error", 0)

    def _apply_log_result(self, job: m.WorkflowJob, run: m.WorkflowRun, res: "_LogResult") -> None:
        if res.status == "ok":
            self._store_events(job, run, res.parsed, res.nbytes)
        elif res.status == "expired":
            age = self._run_age_days(run)
            self._expired_age_days = age if self._expired_age_days is None else min(self._expired_age_days, age)
            self.counters["logs_expired"] += 1
            self._write_ledger(job, run, "expired", http_status=410)
        elif res.status == "unavailable":
            self.counters["logs_unavailable"] += 1
            self._write_ledger(job, run, "unavailable", http_status=res.http_status)
        else:
            self.counters["failures"] += 1
            self._write_ledger(job, run, "error", http_status=0)

    def _store_events(self, job: m.WorkflowJob, run: m.WorkflowRun, parsed: Any, nbytes: int) -> None:
        self.db.query(m.JobCacheEvent).filter_by(github_job_id=job.github_job_id).delete(synchronize_session=False)
        for e in parsed.events:
            self.db.add(m.JobCacheEvent(
                github_job_id=job.github_job_id, seq=e.seq, line_no=e.line_no, step_name=e.step_name,
                action=e.action, kind=e.kind, cache_key=e.cache_key, primary_key=e.primary_key,
                restore_kind=e.restore_kind, raw_line=e.raw_line,
            ))
        label = derive_job_label(parsed.events)
        job.cache_hit = label.hit
        job.cache_outcome = label.outcome
        job.cache_label_status = "labelled" if label.hit is not None else "parsed_no_events"
        status = "parsed_events" if label.hit is not None else "parsed_no_events"
        self._log_downloads += 1
        self.counters["logs_downloaded"] += 1
        self.counters["cache_events"] += len(parsed.events)
        self.counters["logs_with_events" if label.hit is not None else "logs_no_events"] += 1
        row = self.db.get(m.JobLogFetch, job.github_job_id)
        if row is None:
            row = m.JobLogFetch(github_job_id=job.github_job_id, run_id=run.run_id, status=status)
            self.db.add(row)
        row.status = status
        row.http_status = 200
        row.bytes_downloaded = nbytes
        row.n_lines = parsed.n_lines
        row.n_cache_events = len(parsed.events)
        row.parser_version = PARSER_VERSION
        row.fetched_at = utcnow()
        self.db.commit()

    # --- completion + reporting ---------------------------------------

    def _finalize(self, run: m.WorkflowRun) -> bool:
        if run.jobs_fetched_at is None or run.diff_source is None or run.workflow_blob_sha is None:
            return False
        if self.opts.fetch_logs:
            for job in self.db.query(m.WorkflowJob).filter_by(run_id=run.run_id).all():
                if job.conclusion not in LOG_CONCLUSIONS:
                    continue
                ledger = self.db.get(m.JobLogFetch, job.github_job_id)
                if ledger is None or ledger.status in LOG_RETRY:
                    return False
        if not self._is_done(run.run_id):
            self.db.add(m.IngestionProgress(repo=self.repo, stage="run_done", key=run.run_id, status="done"))
            self.db.commit()
        return True

    def _report(self) -> None:
        now = time.time()
        n = self.counters["runs_processed"]
        if n and (n % self.opts.progress_every == 0 or now - self._last_report > 60):
            self._last_report = now
            logger.info(
                "progress %s | runs_processed=%d jobs=%d diffs=%d logs=%d (events=%d expired=%d) "
                "requests=%d rate_limit_waits=%d failures=%d elapsed=%.0fs",
                self.repo, n, self.counters["jobs_upserted"], self.counters["diffs_fetched"],
                self.counters["logs_downloaded"], self.counters["cache_events"], self.counters["logs_expired"],
                self.client.stats["requests"], self.client.stats["rate_limit_waits"],
                self.counters["failures"], now - self._started,
            )


@dataclass
class _LogResult:
    job_id: int
    status: str            # ok | expired | unavailable | error | network
    http_status: int | None = None
    parsed: Any = None
    nbytes: int = 0


def _jobs_of(text: str) -> dict[str, Any] | None:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return jobs if isinstance(jobs, dict) else None


# ---------------------------------------------------------------------------
# Status (real SQL counts, used by `gha-optimizer ingest-status`)
# ---------------------------------------------------------------------------

def ingestion_status(db: Session, repo: str) -> dict[str, Any]:
    def count(q: Any) -> int:
        return int(q.scalar() or 0)

    runs = m.WorkflowRun
    jobs = m.WorkflowJob
    out: dict[str, Any] = {"repo": repo}
    out["runs_total"] = count(db.query(func.count(runs.id)).filter(runs.repo == repo))
    out["runs_completed"] = count(db.query(func.count(runs.id)).filter(runs.repo == repo, runs.status == "completed"))
    out["runs_fully_processed"] = count(
        db.query(func.count(m.IngestionProgress.id)).filter_by(repo=repo, stage="run_done")
    )
    out["date_range"] = list(
        db.query(func.min(runs.gh_created_at), func.max(runs.gh_created_at)).filter(runs.repo == repo).one()
    )
    jq = db.query(func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id).filter(runs.repo == repo)
    out["jobs_total"] = count(jq)
    out["jobs_with_job_key"] = count(jq.filter(jobs.job_key.isnot(None)))
    out["steps_total"] = count(
        db.query(func.count(m.WorkflowStep.id)).join(jobs, jobs.github_job_id == m.WorkflowStep.github_job_id)
        .join(runs, runs.run_id == jobs.run_id).filter(runs.repo == repo)
    )
    out["diff_sources"] = dict(
        db.query(runs.diff_source, func.count(runs.id)).filter(runs.repo == repo).group_by(runs.diff_source).all()
    )
    out["changed_file_rows"] = count(
        db.query(func.count(m.RunChangedFile.id)).join(runs, runs.run_id == m.RunChangedFile.run_id).filter(runs.repo == repo)
    )
    out["cache_label_status"] = dict(
        db.query(jobs.cache_label_status, func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo).group_by(jobs.cache_label_status).all()
    )
    out["cache_label_values"] = {
        {None: "NULL", True: "hit", False: "miss"}[k]: v
        for k, v in db.query(jobs.cache_hit, func.count(jobs.id)).join(runs, runs.run_id == jobs.run_id)
        .filter(runs.repo == repo).group_by(jobs.cache_hit).all()
    }
    out["cache_events"] = count(
        db.query(func.count(m.JobCacheEvent.id)).join(jobs, jobs.github_job_id == m.JobCacheEvent.github_job_id)
        .join(runs, runs.run_id == jobs.run_id).filter(runs.repo == repo)
    )
    out["log_ledger"] = dict(
        db.query(m.JobLogFetch.status, func.count(m.JobLogFetch.github_job_id))
        .join(runs, runs.run_id == m.JobLogFetch.run_id).filter(runs.repo == repo)
        .group_by(m.JobLogFetch.status).all()
    )
    last = (
        db.query(m.IngestionRun).filter_by(repo=repo).order_by(m.IngestionRun.id.desc()).limit(3).all()
    )
    out["recent_invocations"] = [
        {"id": r.id, "status": r.status, "started": str(r.started_at), "finished": str(r.finished_at), "counters": r.counters}
        for r in last
    ]
    return out
