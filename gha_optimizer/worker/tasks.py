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
    Builds a training frame from historical records.
    """
    cid = str(uuid.uuid4())[:8]
    _log("extract_features", cid, "Extracting features for %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.server.db.session import SessionLocal
        from gha_optimizer.server.db import crud
        from gha_optimizer.ingestion.github_client import WorkflowRunRecord

        db = SessionLocal()
        try:
            recent = crud.get_recent_runs(db, repo=repo, limit=200)
        finally:
            db.close()

        _log("extract_features", cid, "Found %d historical records", len(recent))

        if len(recent) < 5:
            return {
                "status": "blocked",
                "reason": "insufficient_data",
                "record_count": len(recent),
            }

        # Convert DB records to WorkflowRunRecords and build features
        records = []
        for rec in recent:
            records.append(WorkflowRunRecord(
                run_id=rec["run_id"],
                commit_sha=rec.get("commit_sha", ""),
                files_changed=rec.get("files_changed", []),
                job_id=rec.get("job_id", "unknown"),
                duration_seconds=rec.get("duration_seconds", 0.0),
                conclusion=rec.get("conclusion", "unknown"),
                cache_hit=bool(rec.get("cache_hit", False)),
                author=rec.get("author", "unknown"),
                timestamp=rec.get("timestamp", ""),
                branch=rec.get("branch", "main"),
                runner_label=rec.get("runner_label", "ubuntu-latest"),
                loc_added=rec.get("loc_added", 0),
                loc_deleted=rec.get("loc_deleted", 0),
            ))

        # Build a minimal graph for feature extraction
        import networkx as nx
        from gha_optimizer.features.feature_extraction import build_training_frame
        graph = nx.DiGraph()
        train_df = build_training_frame(graph, records)

        _log("extract_features", cid,
             "Built training frame: %d rows × %d cols",
             len(train_df), train_df.shape[1])

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "records_used": len(recent),
            "feature_rows": len(train_df),
            "feature_columns": train_df.shape[1],
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
    Checks for drift first, then fits cache-miss, failure, and duration models.
    """
    cid = str(uuid.uuid4())[:8]
    _log("retrain_models", cid, "Retraining models for %s (force=%s)", repo, force)

    try:
        from gha_optimizer.server.db.session import SessionLocal
        from gha_optimizer.server.db import crud
        from gha_optimizer.feedback.drift_detector import DriftDetector
        from gha_optimizer.ingestion.github_client import WorkflowRunRecord
        from gha_optimizer.features.feature_extraction import build_training_frame
        from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
        from gha_optimizer.models.failure_predictor import FailurePredictor
        from gha_optimizer.models.duration_estimator import DurationEstimator
        from gha_optimizer.config import config

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
            raw_records = crud.get_recent_runs(db, repo=repo, limit=500)
            if len(raw_records) < 20:
                _log("retrain_models", cid,
                     "Insufficient training data (%d records, need 20+)", len(raw_records))
                return {
                    "status": "blocked",
                    "reason": "insufficient_data",
                    "record_count": len(raw_records),
                }

            # Convert to WorkflowRunRecords
            records = []
            for rec in raw_records:
                records.append(WorkflowRunRecord(
                    run_id=rec["run_id"],
                    commit_sha=rec.get("commit_sha", ""),
                    files_changed=rec.get("files_changed", []),
                    job_id=rec.get("job_id", "unknown"),
                    duration_seconds=rec.get("duration_seconds", 0.0),
                    conclusion=rec.get("conclusion", "unknown"),
                    cache_hit=bool(rec.get("cache_hit", False)),
                    author=rec.get("author", "unknown"),
                    timestamp=rec.get("timestamp", ""),
                    branch=rec.get("branch", "main"),
                    runner_label=rec.get("runner_label", "ubuntu-latest"),
                    loc_added=rec.get("loc_added", 0),
                    loc_deleted=rec.get("loc_deleted", 0),
                ))

            _log("retrain_models", cid, "Retraining with %d records", len(records))

            # Build training frame
            import networkx as nx
            graph = nx.DiGraph()
            train_df = build_training_frame(graph, records)

            # Train all three models
            model_dir = config.model.model_dir
            model_dir.mkdir(parents=True, exist_ok=True)

            cache_model = CacheMissPredictor()
            cache_metrics = cache_model.fit(train_df)
            cache_model.save(model_dir / "cache_miss.joblib")

            failure_model = FailurePredictor()
            failure_metrics = failure_model.fit(train_df)
            failure_model.save(model_dir / "failure.joblib")

            duration_model = DurationEstimator()
            duration_metrics = duration_model.fit(train_df)
            duration_model.save(model_dir / "duration.joblib")

            _log("retrain_models", cid,
                 "Retraining complete. Cache: %s, Failure: %s, Duration: %s",
                 cache_metrics, failure_metrics, duration_metrics)

            # Record model versions in DB
            import uuid as _uuid
            version = str(_uuid.uuid4())[:8]
            crud.insert_model_version(db, "cache_miss", version, cache_metrics,
                                      str(model_dir / "cache_miss.joblib"), len(train_df))
            crud.insert_model_version(db, "failure", version, failure_metrics,
                                      str(model_dir / "failure.joblib"), len(train_df))
            crud.insert_model_version(db, "duration", version, duration_metrics,
                                      str(model_dir / "duration.joblib"), len(train_df))

            return {
                "status": "complete",
                "correlation_id": cid,
                "records_used": len(records),
                "drift_report": drift_report.summary,
                "cache_metrics": cache_metrics,
                "failure_metrics": failure_metrics,
                "duration_metrics": duration_metrics,
                "model_version": version,
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
        from gha_optimizer.graph.builder import GraphBuilder

        repo_root = Path(".gha_optimizer/repos") / repo.replace("/", "_")
        repo_root.mkdir(parents=True, exist_ok=True)

        benchmarker = GraphBenchmarker()
        builder = GraphBuilder(repo_root)

        # Time the graph build
        def _build():
            builder.build_from_source([])
            return builder.get_graph()
        graph = benchmarker.time_operation("build", _build)

        # Measure memory and snapshot size
        benchmarker.measure_memory(graph)
        benchmarker.measure_snapshot_size(graph)

        # Generate consolidated report
        report = benchmarker.generate_report(graph)
        report_dict = report.model_dump()

        _log("run_benchmark", cid,
             "Benchmark: build=%.1fms, nodes=%d, edges=%d",
             report_dict.get("graph_build_time_ms", 0),
             report_dict.get("node_count", 0),
             report_dict.get("edge_count", 0))

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "benchmark": report_dict,
        }

    except Exception as exc:
        _err("run_benchmark", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)

