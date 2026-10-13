"""
Central data contracts for gha-optimizer.

All inter-module communication uses these typed schemas instead of
arbitrary dicts.  Shared between: API, Celery, Graph, Features, ML,
Scheduler, Generator, Feedback.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────
# Repository & Commit
# ─────────────────────────────────────────────────────────────────────

class RepositoryData(BaseModel):
    """Repository metadata."""
    full_name: str
    default_branch: str = "main"
    language: Optional[str] = None
    is_active: bool = True


class CommitData(BaseModel):
    """A single commit with its changed files."""
    sha: str
    message: str = ""
    author: str = "unknown"
    branch: str = "main"
    timestamp: Optional[str] = None
    files_changed: List[str] = Field(default_factory=list)
    loc_added: int = 0
    loc_deleted: int = 0


# ─────────────────────────────────────────────────────────────────────
# Workflow & Jobs
# ─────────────────────────────────────────────────────────────────────

class WorkflowData(BaseModel):
    """Workflow metadata (separate from parsed WorkflowSpec)."""
    name: str
    file_path: str
    repo: str
    triggers: List[str] = Field(default_factory=list)


class JobData(BaseModel):
    """Per-job execution record — the atomic unit the ML models train on."""
    run_id: str
    commit_sha: str
    job_id: str
    duration_seconds: float = 0.0
    conclusion: str = "unknown"  # success | failure | cancelled | skipped
    cache_hit: bool = False
    runner_label: str = "ubuntu-latest"
    files_changed: List[str] = Field(default_factory=list)
    author: str = "unknown"
    branch: str = "main"
    timestamp: Optional[str] = None
    loc_added: int = 0
    loc_deleted: int = 0


class WorkflowRunData(BaseModel):
    """A complete workflow run containing multiple jobs."""
    run_id: str
    repo: str
    commit_sha: str
    branch: str = "main"
    conclusion: str = "unknown"
    timestamp: Optional[str] = None
    jobs: List[JobData] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Graph
# ─────────────────────────────────────────────────────────────────────

class GraphNodeSchema(BaseModel):
    """Serializable representation of a graph node."""
    node_id: str
    node_type: str
    attributes: Dict[str, Any] = Field(default_factory=dict)


class GraphEdgeSchema(BaseModel):
    """Serializable representation of a graph edge."""
    source: str
    target: str
    edge_type: str
    weight: float = 1.0
    confidence: float = 1.0
    attributes: Dict[str, Any] = Field(default_factory=dict)


class GraphSnapshotSchema(BaseModel):
    """Complete serializable graph snapshot."""
    repo: str
    commit_sha: str
    node_count: int = 0
    edge_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─────────────────────────────────────────────────────────────────────
# Features & Predictions
# ─────────────────────────────────────────────────────────────────────

class FeatureVectorSchema(BaseModel):
    """ML-ready feature vector for a single job."""
    job_id: str
    features: Dict[str, float] = Field(default_factory=dict)


class PredictionStatus(str, Enum):
    """Whether a prediction was made with a trained model or is unavailable."""
    PREDICTED = "predicted"
    UNTRAINED = "untrained"
    FAILED = "failed"


class PredictionResult(BaseModel):
    """ML prediction for a single job."""
    job_id: str
    cache_miss_probability: Optional[float] = None   # None = not predicted (never a default)
    failure_probability: Optional[float] = None
    predicted_duration_seconds: Optional[float] = None
    status: PredictionStatus = PredictionStatus.UNTRAINED
    model_version: Optional[str] = None
    message: str = ""


# ─────────────────────────────────────────────────────────────────────
# Optimization
# ─────────────────────────────────────────────────────────────────────

class OptimizationRequest(BaseModel):
    """Request to optimise a workflow."""
    repo: str
    workflow_path: str = ".github/workflows/ci.yml"
    commit_sha: Optional[str] = None
    changed_files: List[str] = Field(default_factory=list)
    alpha: Optional[float] = None
    beta: Optional[float] = None
    gamma: Optional[float] = None
    allow_runner_upsizing: Optional[bool] = None   # default off; the larger-runner labels must exist in the org
    speedup_exponent: Optional[float] = None       # ASSUMED runner speed-up law exponent
    allow_skip_recommendations: Optional[bool] = None  # default off; see docs/SCHEDULER_ASSUMPTIONS.md
    as_of: Optional[datetime] = None               # scoring time (hour / weekday are features); default: now
    relax_gates: Optional[bool] = None             # default off; drops only provably safe `needs` gates (gates.py)
    llm_summary: Optional[bool] = None             # default off; needs GEMINI_API_KEY + GHA_LLM_MODEL; wording only


class OptimizationResultSchema(BaseModel):
    """Result of an optimization run."""
    repo: str
    workflow_name: str
    jobs_total: int = 0
    jobs_skipped: int = 0
    baseline_wall_clock: float = 0.0
    optimized_wall_clock: float = 0.0
    baseline_cost: float = 0.0
    optimized_cost: float = 0.0
    baseline_energy: float = 0.0
    optimized_energy: float = 0.0
    plan_summary: Dict[str, Any] = Field(default_factory=dict)
    explanation: str = ""
    optimized_yaml: str = ""


# ─────────────────────────────────────────────────────────────────────
# Decision Trace
# ─────────────────────────────────────────────────────────────────────

class DecisionTraceSchema(BaseModel):
    """Full audit trail for one optimization decision."""
    trace_id: str = ""
    repo: str = ""
    commit_sha: str = ""
    graph_version: Optional[str] = None
    model_version: Optional[str] = None
    predictions: List[PredictionResult] = Field(default_factory=list)
    optimization_result: Optional[OptimizationResultSchema] = None
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─────────────────────────────────────────────────────────────────────
# Execution Result / Feedback
# ─────────────────────────────────────────────────────────────────────

class ExecutionResult(BaseModel):
    """Actual execution outcome used for feedback/retraining."""
    run_id: str
    job_id: str
    actual_cache_miss: Optional[bool] = None
    actual_failure: Optional[bool] = None
    actual_duration: Optional[float] = None
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─────────────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────────────

class WebhookEvent(BaseModel):
    """Normalised internal representation of a GitHub webhook event."""
    event_type: str  # push | pull_request | workflow_run | workflow_job
    repo: str
    action: str = ""  # e.g. "completed", "opened"
    commit_sha: str = ""
    branch: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    received_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─────────────────────────────────────────────────────────────────────
# Service Status
# ─────────────────────────────────────────────────────────────────────

class HealthStatus(BaseModel):
    """System health check response."""
    status: str = "healthy"
    version: str = "1.0.0"
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    db_connected: bool = False
    redis_connected: bool = False
    celery_connected: bool = False


class ParserCapability(BaseModel):
    """Reports what a language parser can and cannot do."""
    language: str
    available: bool = False
    extracts_imports: bool = False
    extracts_classes: bool = False
    extracts_functions: bool = False
    extracts_calls: bool = False
    error_message: str = ""
