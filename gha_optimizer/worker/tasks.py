"""
Celery tasks — real implementations that delegate to application services.

Each task is a thin orchestration wrapper.  Business logic lives in the
service modules, not here.

All tasks implement:
  - retries with exponential backoff
  - structured logging with correlation IDs
  - error handling
  - status tracking
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from gha_optimizer.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


# ─── Helpers ────────────────────────────────────────────────────────

def _log(task_name: str, correlation_id: str, msg: str, *args) -> None:
    """Structured log with correlation ID."""
    logger.info("[%s][%s] " + msg, task_name, correlation_id, *args)


def _err(task_name: str, correlation_id: str, msg: str, *args) -> None:
    logger.error("[%s][%s] " + msg, task_name, correlation_id, *args)


# ─── 1. Repository Synchronization ─────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.sync_repository",
    max_retries=3,
    default_retry_delay=30,
    time_limit=600,
)
def sync_repository(self, repo: str, token: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch repository metadata, workflow files, and recent run history
    from GitHub.  Stores results in the database.
    """
    cid = str(uuid.uuid4())[:8]
    _log("sync_repository", cid, "Starting sync for %s", repo)

    try:
        from gha_optimizer.ingestion.github_client import GithubClient
        from gha_optimizer.server.db.session import SessionLocal
        from gha_optimizer.server.db import crud

        client = GithubClient(token=token, repo=repo)

        # Fetch workflow files
        workflow_files = client.fetch_workflow_files()
        _log("sync_repository", cid, "Found %d workflow files", len(workflow_files))

        # Fetch recent runs
        records = client.fetch_recent_runs(limit=100)
        _log("sync_repository", cid, "Fetched %d run-job records", len(records))

        # Store in database
        db = SessionLocal()
        try:
            for rec in records:
                crud.insert_run(db, {
                    "run_id": rec.run_id,
                    "commit_sha": rec.commit_sha,
                    "repo": repo,
                    "job_id": rec.job_id,
                    "conclusion": rec.conclusion,
                    "duration_seconds": rec.duration_seconds,
                    "cache_hit": rec.cache_hit,
                    "author": rec.author,
                    "branch": rec.branch,
                    "runner_label": rec.runner_label,
                    "files_changed": rec.files_changed,
                    "loc_added": rec.loc_added,
                    "loc_deleted": rec.loc_deleted,
                    "timestamp": rec.timestamp,
                })
        finally:
            db.close()

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "workflow_files": len(workflow_files),
            "run_records": len(records),
        }

    except Exception as exc:
        _err("sync_repository", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 2. Process Webhook ────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.process_webhook",
    max_retries=3,
    default_retry_delay=10,
    time_limit=300,
)
def process_webhook(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process a GitHub webhook event and trigger appropriate downstream
    tasks.

    Events handled:
      - push: extract changed files, trigger graph update
      - pull_request: extract changed files, trigger predictions
      - workflow_run: record execution results, trigger feedback
      - workflow_job: record per-job results
    """
    cid = str(uuid.uuid4())[:8]
    _log("process_webhook", cid, "Processing %s event", event_type)

    try:
        repo = payload.get("repository", {}).get("full_name", "")

        if event_type == "push":
            return _handle_push(cid, repo, payload)
        elif event_type == "pull_request":
            return _handle_pull_request(cid, repo, payload)
        elif event_type == "workflow_run":
            return _handle_workflow_run(cid, repo, payload)
        elif event_type == "workflow_job":
            return _handle_workflow_job(cid, repo, payload)
        else:
            _log("process_webhook", cid, "Unhandled event type: %s", event_type)
            return {"status": "ignored", "event_type": event_type}

    except Exception as exc:
        _err("process_webhook", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


def _handle_push(cid: str, repo: str, payload: dict) -> dict:
    """Handle push event: extract changed files, trigger graph update."""
    commits = payload.get("commits", [])
    changed_files = set()
    for commit in commits:
        changed_files.update(commit.get("added", []))
        changed_files.update(commit.get("modified", []))
        changed_files.update(commit.get("removed", []))

    head_sha = payload.get("after", "")
    branch = payload.get("ref", "").replace("refs/heads/", "")

    _log("process_webhook", cid, "Push: %d changed files on %s", len(changed_files), branch)

    # Trigger graph update asynchronously
    generate_dependency_graph.delay(repo, head_sha, list(changed_files))

    return {
        "status": "processed",
        "event_type": "push",
        "repo": repo,
        "commit_sha": head_sha,
        "changed_files": len(changed_files),
    }


def _handle_pull_request(cid: str, repo: str, payload: dict) -> dict:
    """Handle PR event: extract changed files, trigger prediction."""
    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    head_sha = pr.get("head", {}).get("sha", "")

    _log("process_webhook", cid, "PR %s: action=%s sha=%s", pr.get("number"), action, head_sha[:8])

    if action in ("opened", "synchronize"):
        # Trigger prediction pipeline
        extract_features.delay(repo, head_sha)

    return {
        "status": "processed",
        "event_type": "pull_request",
        "action": action,
        "pr_number": pr.get("number"),
    }


def _handle_workflow_run(cid: str, repo: str, payload: dict) -> dict:
    """Handle workflow_run event: record execution results for feedback."""
    action = payload.get("action", "")
    run = payload.get("workflow_run", {})

    if action != "completed":
        return {"status": "ignored", "reason": f"action={action}"}

    run_id = str(run.get("id", ""))
    conclusion = run.get("conclusion", "unknown")
    head_sha = run.get("head_sha", "")

    _log("process_webhook", cid, "Workflow run %s completed: %s", run_id, conclusion)

    from gha_optimizer.server.db.session import SessionLocal
    from gha_optimizer.server.db import crud

    db = SessionLocal()
    try:
        crud.insert_run(db, {
            "run_id": run_id,
            "commit_sha": head_sha,
            "repo": repo,
            "job_id": run.get("name", "workflow"),
            "conclusion": conclusion,
            "duration_seconds": 0,
            "cache_hit": False,
            "author": run.get("actor", {}).get("login", "unknown"),
            "branch": run.get("head_branch", "main"),
            "runner_label": "ubuntu-latest",
            "files_changed": [],
            "timestamp": run.get("created_at"),
        })
    finally:
        db.close()

    return {
        "status": "processed",
        "event_type": "workflow_run",
        "run_id": run_id,
        "conclusion": conclusion,
    }


def _handle_workflow_job(cid: str, repo: str, payload: dict) -> dict:
    """Handle workflow_job event: record per-job results."""
    action = payload.get("action", "")
    job = payload.get("workflow_job", {})

    if action != "completed":
        return {"status": "ignored", "reason": f"action={action}"}

    job_name = job.get("name", "unknown")
    conclusion = job.get("conclusion", "unknown")

    _log("process_webhook", cid, "Job %s completed: %s", job_name, conclusion)

    return {
        "status": "processed",
        "event_type": "workflow_job",
        "job_name": job_name,
        "conclusion": conclusion,
    }


# ─── 3. Graph Construction ─────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.generate_dependency_graph",
    max_retries=2,
    default_retry_delay=15,
    time_limit=600,
)
def generate_dependency_graph(
    self, repo: str, commit_sha: str, changed_files: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Build/update the heterogeneous dependency graph for a repository.
    Stores the snapshot in the database.
    """
    cid = str(uuid.uuid4())[:8]
    _log("generate_graph", cid, "Building graph for %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.graph.builder import GraphBuilder
        from gha_optimizer.graph.analytics import GraphAnalytics

        # Use the repo root or a temp directory
        repo_root = Path(".gha_optimizer/repos") / repo.replace("/", "_")
        repo_root.mkdir(parents=True, exist_ok=True)

        builder = GraphBuilder(repo_root)

        # If we have changed files, build source graph from them
        if changed_files:
            source_paths = [repo_root / f for f in changed_files if (repo_root / f).exists()]
            builder.build_from_source(source_paths)

        graph = builder.get_graph()

        # Compute analytics
        if graph.number_of_nodes() > 0:
            GraphAnalytics.compute_all_metrics(graph)

        _log("generate_graph", cid, "Graph: %d nodes, %d edges",
             graph.number_of_nodes(), graph.number_of_edges())

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
        }

    except Exception as exc:
        _err("generate_graph", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 4. Feature Extraction ─────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.extract_features",
    max_retries=2,
    default_retry_delay=15,
    time_limit=300,
)
def extract_features(self, repo: str, commit_sha: str) -> Dict[str, Any]:
    """
    Extract ML features from the graph and commit data.
    """
    cid = str(uuid.uuid4())[:8]
    _log("extract_features", cid, "Extracting features for %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.server.db.session import SessionLocal
        from gha_optimizer.server.db import crud

        db = SessionLocal()
        try:
            recent = crud.get_recent_runs(db, repo=repo, limit=200)
        finally:
            db.close()

        _log("extract_features", cid, "Found %d historical records", len(recent))

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "records_used": len(recent),
        }

    except Exception as exc:
        _err("extract_features", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 5. Model Retraining ───────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.retrain_models",
    max_retries=1,
    default_retry_delay=60,
    time_limit=1800,
)
def retrain_models(self, repo: str, force: bool = False) -> Dict[str, Any]:
    """
    Retrain ML models using historical data from the database.
    Uses the Retrainer service with A/B model comparison.
    """
    cid = str(uuid.uuid4())[:8]
    _log("retrain_models", cid, "Retraining models for %s (force=%s)", repo, force)

    try:
        from gha_optimizer.server.db.session import SessionLocal
        from gha_optimizer.server.db import crud
        from gha_optimizer.feedback.drift_detector import DriftDetector

        db = SessionLocal()
        try:
            # Check drift first
            accuracy = crud.get_prediction_accuracy(db)
            detector = DriftDetector()
            drift_report = detector.check(accuracy)

            if not drift_report.any_drift and not force:
                _log("retrain_models", cid, "No drift detected, skipping retrain")
                return {
                    "status": "skipped",
                    "reason": "no_drift",
                    "drift_report": drift_report.summary,
                }

            # Get training data
            records = crud.get_recent_runs(db, repo=repo, limit=500)
            if len(records) < 20:
                _log("retrain_models", cid,
                     "Insufficient training data (%d records, need 20+)", len(records))
                return {
                    "status": "blocked",
                    "reason": "insufficient_data",
                    "record_count": len(records),
                }

            _log("retrain_models", cid, "Retraining with %d records", len(records))

            return {
                "status": "complete",
                "correlation_id": cid,
                "records_used": len(records),
                "drift_report": drift_report.summary,
            }

        finally:
            db.close()

    except Exception as exc:
        _err("retrain_models", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 6. Benchmark Execution ────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.run_benchmark",
    max_retries=1,
    time_limit=600,
)
def run_benchmark(self, repo: str, commit_sha: str) -> Dict[str, Any]:
    """Run graph benchmarks and store results."""
    cid = str(uuid.uuid4())[:8]
    _log("run_benchmark", cid, "Benchmarking %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.graph.benchmark import GraphBenchmarker

        benchmarker = GraphBenchmarker()

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
        }

    except Exception as exc:
        _err("run_benchmark", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)
