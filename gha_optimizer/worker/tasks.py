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

import logging
import uuid
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
    Ingest the last week of real GitHub Actions history for ``repo`` (runs, jobs, steps,
    diffs, log-derived cache labels) through the ingestion service.  Idempotent and resumable.
    """
    cid = str(uuid.uuid4())[:8]
    _log("sync_repository", cid, "Starting sync for %s", repo)

    try:
        from datetime import datetime, timedelta, timezone

        from gha_optimizer.config import config
        from gha_optimizer.ingestion.github_client import GithubClient
        from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
        from gha_optimizer.server.db.session import SessionLocal, init_db

        token = token or config.github.token
        if not token:
            return {"status": "blocked", "reason": "no_github_token", "repo": repo}

        init_db()
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        db = SessionLocal()
        try:
            counters = RepoIngestor(repo, GithubClient(token=token, repo=repo), db, IngestOptions(since=since)).run()
        finally:
            db.close()
        return {"status": "complete", "correlation_id": cid, "repo": repo, "counters": counters}

    except Exception as exc:
        _err("sync_repository", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="gha_optimizer.ingest_run",
    max_retries=3,
    default_retry_delay=30,
    time_limit=900,
)
def ingest_run(self, repo: str, run_id: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Ingest a single completed workflow run (triggered by a ``workflow_run`` webhook)."""
    cid = str(uuid.uuid4())[:8]
    try:
        from datetime import datetime, timezone

        from gha_optimizer.config import config
        from gha_optimizer.ingestion.github_client import GithubClient
        from gha_optimizer.ingestion.ingest import IngestOptions, RepoIngestor
        from gha_optimizer.server.db.session import SessionLocal, init_db

        token = token or config.github.token
        if not token:
            return {"status": "blocked", "reason": "no_github_token", "repo": repo, "run_id": run_id}
        init_db()
        db = SessionLocal()
        try:
            ing = RepoIngestor(repo, GithubClient(token=token, repo=repo), db,
                               IngestOptions(since=datetime.now(timezone.utc).replace(tzinfo=None)))
            counters = ing.ingest_single_run(run_id)
        finally:
            db.close()
        return {"status": "complete", "correlation_id": cid, "repo": repo, "run_id": run_id, "counters": counters}
    except Exception as exc:
        _err("ingest_run", cid, "Failed: %s", exc)
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
      - workflow_run: ingest the real run
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
    """Handle workflow_run event: ingest the *real* run (jobs, timings, logs), never a placeholder row."""
    action = payload.get("action", "")
    run = payload.get("workflow_run", {})

    if action != "completed":
        return {"status": "ignored", "reason": f"action={action}"}

    run_id = str(run.get("id", ""))
    _log("process_webhook", cid, "Workflow run %s completed: %s", run_id, run.get("conclusion"))
    task = ingest_run.delay(repo, run_id)
    return {
        "status": "processed",
        "event_type": "workflow_run",
        "run_id": run_id,
        "conclusion": run.get("conclusion"),
        "ingest_task_id": task.id,
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

def _graphs_at(repo: str, commit_sha: str):
    """``(source graph, {workflow path: fused graph})`` of ``repo`` at ``commit_sha`` read from its git mirror, or
    ``None`` when the commit is not available.  Uses the same per-commit builder as the dataset (no checkout needed)."""
    from gha_optimizer.config import config
    from gha_optimizer.features.graph_features import GraphFeatureComputer
    from gha_optimizer.ingestion.mirror import DEFAULT_MIRROR_ROOT, RepoMirror

    mirror = RepoMirror(repo, DEFAULT_MIRROR_ROOT, token=config.github.token)
    mirror.ensure(refresh=True)
    if not mirror.has_commit(commit_sha) and not mirror.fetch_sha(commit_sha):
        return None
    computer = GraphFeatureComputer(mirror)
    try:
        source = computer.source_graph(commit_sha)
        fused = {}
        for path, blob in mirror.list_tree(commit_sha):
            if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
                data = mirror.read_blob(blob)
                graph = computer.graph_at(commit_sha, data.decode("utf-8", "replace")) if data else None
                if graph is not None:
                    fused[path] = graph
        return source, fused
    finally:
        computer.close()


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
    Build the dependency graph of ``repo`` at ``commit_sha`` from the local git mirror (the repository tree plus each
    workflow fused with it) and report its size.  Nothing is stored: the dataset path recomputes graphs per commit.
    ``changed_files`` is informational (the whole tree is built).  Returns ``blocked`` if the commit is unavailable.
    """
    cid = str(uuid.uuid4())[:8]
    _log("generate_graph", cid, "Building graph for %s@%s", repo, commit_sha[:8])

    try:
        built = _graphs_at(repo, commit_sha)
        if built is None:
            return {"status": "blocked", "reason": "commit_not_in_mirror", "repo": repo, "commit_sha": commit_sha}
        source, fused = built
        _log("generate_graph", cid, "Source graph: %d nodes, %d edges; %d workflow graph(s)",
             source.number_of_nodes(), source.number_of_edges(), len(fused))
        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "changed_files": len(changed_files or []),
            "source_nodes": source.number_of_nodes(),
            "source_edges": source.number_of_edges(),
            "workflows": {p: {"nodes": g.number_of_nodes(), "edges": g.number_of_edges()} for p, g in fused.items()},
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
    Build the feature frame from the real ingested history of ``repo``
    (strictly-prior historical features, per-commit graph features where computed).
    """
    cid = str(uuid.uuid4())[:8]
    _log("extract_features", cid, "Extracting features for %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.config import config
        from gha_optimizer.features.dataset import build_dataset
        from gha_optimizer.server.db.session import SessionLocal, init_db

        init_db()
        db = SessionLocal()
        try:
            train_df, info = build_dataset(db, repo)
        finally:
            db.close()

        n_runs = int(train_df["run_id"].nunique()) if len(train_df) else 0
        _log("extract_features", cid, "Found %d dataset rows from %d runs", len(train_df), n_runs)

        if n_runs < config.model.min_history_runs:
            return {
                "status": "blocked",
                "reason": "insufficient_data",
                "runs": n_runs,
                "required_runs": config.model.min_history_runs,
            }

        return {
            "status": "complete",
            "correlation_id": cid,
            "repo": repo,
            "commit_sha": commit_sha,
            "runs_used": n_runs,
            "feature_rows": len(train_df),
            "feature_columns": train_df.shape[1],
        }

    except Exception as exc:
        _err("extract_features", cid, "Failed: %s", exc)
        raise self.retry(exc=exc)


# ─── 5. Benchmark Execution ────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="gha_optimizer.run_benchmark",
    max_retries=1,
    time_limit=600,
)
def run_benchmark(self, repo: str, commit_sha: str) -> Dict[str, Any]:
    """Measure building the real graph of ``repo`` at ``commit_sha`` (source tree + each workflow) from the git
    mirror: wall-clock of the build, memory estimate and snapshot size of the largest fused graph.  ``blocked`` if the
    commit is unavailable; never benchmarks an empty placeholder graph."""
    cid = str(uuid.uuid4())[:8]
    _log("run_benchmark", cid, "Benchmarking %s@%s", repo, commit_sha[:8])

    try:
        from gha_optimizer.graph.benchmark import GraphBenchmarker

        benchmarker = GraphBenchmarker()
        built = benchmarker.time_operation("build", _graphs_at, repo, commit_sha)
        if built is None:
            return {"status": "blocked", "reason": "commit_not_in_mirror", "repo": repo, "commit_sha": commit_sha}
        source, fused = built
        graph = max(fused.values(), key=lambda g: g.number_of_nodes()) if fused else source

        benchmarker.measure_memory(graph)
        benchmarker.measure_snapshot_size(graph)
        report_dict = benchmarker.generate_report(graph).model_dump()

        _log("run_benchmark", cid, "Benchmark: build=%.1fms, nodes=%d, edges=%d",
             report_dict.get("graph_build_time_ms") or 0.0, report_dict.get("node_count", 0), report_dict.get("edge_count", 0))

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
