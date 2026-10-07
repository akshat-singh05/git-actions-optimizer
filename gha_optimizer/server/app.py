"""
FastAPI server — API endpoints, webhook processing, health checks.

Responsibilities:
  - GitHub webhook receipt with HMAC signature validation
  - Optimization pipeline invocation
  - Health and readiness endpoints
  - Graph API routing
  - CORS middleware
  - Structured request logging
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from gha_optimizer.config import config
from gha_optimizer.schemas import (
    HealthStatus,
    OptimizationRequest,
    OptimizationResultSchema,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="GHA Optimizer API",
    description="ML-based predictive scheduler for GitHub Actions CI/CD pipelines",
    version="1.0.0",
)

# ─── CORS ───────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Request Logging Middleware ─────────────────────────────────────

@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Add request ID and log every request with timing."""
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "[%s] %s %s → %d (%.1fms)",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    response.headers["X-Request-ID"] = request_id
    return response


# ─── DB Dependency ──────────────────────────────────────────────────

def get_db():
    from gha_optimizer.server.db.session import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Health & Readiness ────────────────────────────────────────────

def _db_ok() -> bool:
    """True if a ``SELECT 1`` succeeds on the database the API sessions use (``server/db/session.py``)."""
    try:
        from sqlalchemy import text

        from gha_optimizer.server.db.session import SessionLocal
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            return True
        finally:
            db.close()
    except Exception:
        return False


@app.get("/health", response_model=HealthStatus, tags=["system"])
async def health_check():
    """Basic health check — always returns 200 if the server is up."""
    status = HealthStatus(status="healthy")

    status.db_connected = _db_ok()

    # Check Redis
    try:
        from gha_optimizer.server.cache import redis_client
        if redis_client and redis_client.ping():
            status.redis_connected = True
    except Exception:
        status.redis_connected = False

    return status


@app.get("/ready", tags=["system"])
async def readiness_check():
    """Readiness check — runs a real ``SELECT 1`` against the session database."""
    if not _db_ok():
        raise HTTPException(status_code=503, detail="Not ready: database unreachable")
    return {"status": "ready"}


# ─── Webhook ───────────────────────────────────────────────────────

def _verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/github", tags=["webhook"])
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
    x_github_delivery: Optional[str] = Header(None),
):
    """
    Receive GitHub webhook events.  Validates HMAC signature if
    GITHUB_WEBHOOK_SECRET is configured.

    Dispatches processing to Celery for async handling — does NOT
    do expensive work in the HTTP request.
    """
    body = await request.body()

    # Validate signature if secret is configured
    webhook_secret = config.github.webhook_secret
    if webhook_secret:
        sig = x_hub_signature_256 or ""
        if not _verify_webhook_signature(body, sig, webhook_secret):
            logger.warning("Webhook signature verification failed")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = x_github_event or "unknown"

    logger.info(
        "Received webhook: event=%s delivery=%s",
        event_type,
        x_github_delivery or "unknown",
    )

    # Dispatch to Celery for async processing
    try:
        from gha_optimizer.worker.tasks import process_webhook
        task = process_webhook.delay(event_type, payload)
        return {
            "status": "accepted",
            "event_type": event_type,
            "task_id": task.id,
            "delivery_id": x_github_delivery,
        }
    except Exception as exc:
        logger.error("Failed to dispatch webhook task: %s", exc)
        # Still return 200 to GitHub so it doesn't retry
        return {
            "status": "accepted_with_error",
            "error": str(exc),
        }


# ─── Optimization ──────────────────────────────────────────────────

@app.post("/api/v1/optimize", tags=["optimization"])
async def optimize_workflow(request: OptimizationRequest, db: Session = Depends(get_db)):
    """
    Run the optimization pipeline on a workflow.  Returns the optimized
    YAML, schedule plan, and decision trace.
    """
    logger.info("Optimization request for %s", request.repo)

    try:
        from pathlib import Path
        from gha_optimizer.ingestion.history import InsufficientHistoryError, load_history
        from gha_optimizer.ingestion.workspace import tracked_files, workflow_repo_path
        from gha_optimizer.pipeline import run_pipeline
        from gha_optimizer.server.db import crud

        workflow_path = Path(request.workflow_path)
        if not workflow_path.exists():
            raise HTTPException(status_code=404, detail=f"Workflow file not found: {workflow_path}")

        repo_root = workflow_path.parent.parent.parent
        wf_repo_path = workflow_repo_path(workflow_path, repo_root)

        # Real ingested history only.  No synthetic fallback: with too little history the
        # pipeline raises InsufficientHistoryError and we decline with HTTP 422.
        history = load_history(db, request.repo, wf_repo_path)

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            out_path = f.name

        try:
            plan, explanation = run_pipeline(
                workflow_path=workflow_path,
                repo_root=repo_root,
                tracked_files=tracked_files(repo_root) or request.changed_files,
                history=history,
                changed_files=request.changed_files,
                out_path=out_path,
                model_backend=request.model_backend,
                alpha=request.alpha,
                beta=request.beta,
                gamma=request.gamma,
                allow_runner_upsizing=request.allow_runner_upsizing,
                speedup_exponent=request.speedup_exponent,
                model_dir=config.model.model_dir,
                workflow_repo_path=wf_repo_path,
            )
        except InsufficientHistoryError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "insufficient_history", "reason": exc.reason, "details": exc.details,
                        "hint": "ingest real history first: gha-optimizer ingest --repo <owner/repo>"},
            )

        # Read the generated YAML
        optimized_yaml = Path(out_path).read_text(encoding="utf-8")

        result = OptimizationResultSchema(
            repo=request.repo,
            workflow_name=workflow_path.stem,
            jobs_total=len(plan.jobs),
            jobs_skipped=sum(1 for j in plan.jobs if j.skip_recommended),
            baseline_wall_clock=plan.baseline_wall_clock_seconds,
            optimized_wall_clock=plan.estimated_wall_clock_seconds,
            baseline_cost=plan.baseline_cost_usd,
            optimized_cost=plan.estimated_cost_usd,
            baseline_energy=plan.baseline_energy_wh,
            optimized_energy=plan.estimated_energy_wh,
            plan_summary=plan.summary_dict(),
            explanation=explanation,
            optimized_yaml=optimized_yaml,
        )

        # Store result
        crud.insert_optimization_result(db, {
            "repo": request.repo,
            "workflow_name": workflow_path.stem,
            "baseline_wall_clock": plan.baseline_wall_clock_seconds,
            "optimized_wall_clock": plan.estimated_wall_clock_seconds,
            "baseline_cost": plan.baseline_cost_usd,
            "optimized_cost": plan.estimated_cost_usd,
            "baseline_energy": plan.baseline_energy_wh,
            "optimized_energy": plan.estimated_energy_wh,
            "jobs_total": len(plan.jobs),
            "jobs_skipped": sum(1 for j in plan.jobs if j.skip_recommended),
            "plan": plan.summary_dict(),
        })

        return result.model_dump()

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Optimization failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ─── Dashboard / Metrics ───────────────────────────────────────────

@app.get("/api/v1/metrics", tags=["metrics"])
async def get_metrics(db: Session = Depends(get_db)):
    """Return prediction accuracy and system metrics."""
    from gha_optimizer.server.db import crud
    return crud.get_prediction_accuracy(db)


@app.get("/api/v1/dashboard/summary", tags=["metrics"])
async def dashboard_summary(db: Session = Depends(get_db)):
    """Aggregate summary for the dashboard."""
    from gha_optimizer.server.db import crud
    optimizations = crud.get_optimization_history(db, limit=10)
    accuracy = crud.get_prediction_accuracy(db)
    return {
        "recent_optimizations": optimizations,
        "prediction_accuracy": accuracy,
    }


@app.get("/api/v1/parsers", tags=["system"])
async def parser_capabilities():
    """Report which language parsers are available."""
    from gha_optimizer.graph.builder import GraphBuilder
    from pathlib import Path
    builder = GraphBuilder(Path("."))
    return builder.get_parser_capabilities()


# ─── Graph API Router ──────────────────────────────────────────────

try:
    from gha_optimizer.server.api.v1.graph import router as graph_router
    app.include_router(graph_router, prefix="/api/v1")
except ImportError as e:
    logger.warning("Graph API router not available: %s", e)
