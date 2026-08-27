"""
Graph Data Models — Node Types, Edge Types, and Metadata schemas.

Defines 33 Node Types and 19 Edge Types for the Heterogeneous Dependency
Graph Engine.  All metadata is validated via Pydantic and carries full
provenance tracking so every fact in the knowledge graph can be traced
back to its origin parser, source file, line number, and commit.
"""
from __future__ import annotations

from enum import Enum
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────
# Node Types (33)
# ─────────────────────────────────────────────────────────────────────

class NodeType(str, Enum):
    REPOSITORY = "Repository"
    ORGANIZATION = "Organization"
    BRANCH = "Branch"
    COMMIT = "Commit"
    DIRECTORY = "Directory"
    FILE = "File"
    CLASS = "Class"
    INTERFACE = "Interface"
    FUNCTION = "Function"
    METHOD = "Method"
    VARIABLE = "Variable"
    PACKAGE = "Package"
    MODULE = "Module"
    EXTERNAL_DEPENDENCY = "ExternalDependency"
    WORKFLOW = "Workflow"
    WORKFLOW_TRIGGER = "WorkflowTrigger"
    WORKFLOW_JOB = "WorkflowJob"
    WORKFLOW_STEP = "WorkflowStep"
    CACHE_KEY = "CacheKey"
    RUNNER = "Runner"
    ARTIFACT = "Artifact"
    ENVIRONMENT = "Environment"
    DOCKER_IMAGE = "DockerImage"
    SECRET = "Secret"
    SERVICE = "Service"
    LANGUAGE_RUNTIME = "LanguageRuntime"
    TEST_SUITE = "TestSuite"
    BUILD_TARGET = "BuildTarget"
    DEPLOYMENT_TARGET = "DeploymentTarget"
    DEVELOPER = "Developer"
    PULL_REQUEST = "PullRequest"
    ISSUE = "Issue"
    RELEASE = "Release"


# ─────────────────────────────────────────────────────────────────────
# Edge Types (19)
# ─────────────────────────────────────────────────────────────────────

class EdgeType(str, Enum):
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    DEPENDS_ON = "DEPENDS_ON"
    USES = "USES"
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"
    GENERATES = "GENERATES"
    PRODUCES = "PRODUCES"
    CONSUMES = "CONSUMES"
    EXECUTES = "EXECUTES"
    RESTORES_CACHE = "RESTORES_CACHE"
    INVALIDATES_CACHE = "INVALIDATES_CACHE"
    RUNS_ON = "RUNS_ON"
    TRIGGERS = "TRIGGERS"
    BELONGS_TO = "BELONGS_TO"
    REFERENCES = "REFERENCES"
    TESTS = "TESTS"
    DEPLOYS = "DEPLOYS"
    LINKS_TO = "LINKS_TO"


# ─────────────────────────────────────────────────────────────────────
# Graph Event Types
# ─────────────────────────────────────────────────────────────────────

class GraphEventType(str, Enum):
    COMMIT_CREATED = "COMMIT_CREATED"
    PULL_REQUEST = "PULL_REQUEST"
    WORKFLOW_STARTED = "WORKFLOW_STARTED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    CACHE_MISS = "CACHE_MISS"
    JOB_FAILURE = "JOB_FAILURE"
    DEPENDENCY_CHANGE = "DEPENDENCY_CHANGE"
    PACKAGE_UPDATE = "PACKAGE_UPDATE"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"


# ─────────────────────────────────────────────────────────────────────
# Provenance Record  (immutable audit trail for every graph fact)
# ─────────────────────────────────────────────────────────────────────

class ProvenanceRecord(BaseModel):
    """Immutable record that traces how a node or edge was created."""
    origin_parser: str = "unknown"          # e.g. "PythonParser", "WorkflowParser"
    creation_source: str = "unknown"        # e.g. "ast", "workflow_yaml", "github_api"
    repository: Optional[str] = None
    branch: Optional[str] = None
    commit: Optional[str] = None
    workflow: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    confidence: float = 1.0                 # parser confidence 0..1
    source_file: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    version: str = "1.0"
    modification_history: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Node Metadata  (structural + provenance + semantic knowledge)
# ─────────────────────────────────────────────────────────────────────

class NodeMetadata(BaseModel):
    """Complete metadata attached to every node in the knowledge graph."""

    # ── Identity ────────────────────────────────────────────────────
    unique_id: str
    node_type: NodeType
    repository: Optional[str] = None
    commit: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    language: Optional[str] = None
    version: Optional[str] = None
    owner: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    # ── Provenance ──────────────────────────────────────────────────
    provenance: ProvenanceRecord = Field(default_factory=ProvenanceRecord)

    # ── Historical execution metrics ────────────────────────────────
    execution_count: int = 0
    historical_failures: int = 0
    average_runtime: float = 0.0
    average_cache_hit_rate: float = 0.0
    creation_time: Optional[datetime] = None
    modification_time: Optional[datetime] = None

    # ── Semantic knowledge scores ───────────────────────────────────
    risk_score: float = 0.0
    failure_score: float = 0.0
    importance_score: float = 0.0
    execution_frequency: float = 0.0
    cache_usage: float = 0.0
    cache_stability: float = 0.0
    developer_ownership: Optional[str] = None
    is_hotspot: bool = False
    dependency_stability: float = 1.0       # 1.0 = perfectly stable
    technical_debt_score: float = 0.0

    # ── Analytics (populated by GraphAnalytics) ─────────────────────
    degree: Optional[int] = None
    in_degree: Optional[int] = None
    out_degree: Optional[int] = None
    pagerank: Optional[float] = None
    betweenness: Optional[float] = None
    closeness: Optional[float] = None
    eigenvector_centrality: Optional[float] = None
    community_id: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────
# Edge Metadata  (relationship + provenance)
# ─────────────────────────────────────────────────────────────────────

class EdgeMetadata(BaseModel):
    """Complete metadata attached to every edge in the knowledge graph."""

    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    confidence: float = 1.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    version: Optional[str] = None
    execution_frequency: int = 0

    # ── Provenance ──────────────────────────────────────────────────
    provenance: ProvenanceRecord = Field(default_factory=ProvenanceRecord)


# ─────────────────────────────────────────────────────────────────────
# Graph Event Payload
# ─────────────────────────────────────────────────────────────────────

class GraphEvent(BaseModel):
    """A discrete event that triggers a graph update or metric refresh."""
    event_type: GraphEventType
    repository: Optional[str] = None
    commit: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    payload: Dict[str, Any] = Field(default_factory=dict)
    impacted_node_ids: List[str] = Field(default_factory=list)
    impacted_edge_keys: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Graph Quality Report
# ─────────────────────────────────────────────────────────────────────

class GraphQualityReport(BaseModel):
    """Snapshot of graph health metrics."""
    total_nodes: int = 0
    total_edges: int = 0
    coverage: float = 0.0                   # % of repo files represented
    average_parser_confidence: float = 0.0
    broken_dependencies: int = 0
    missing_nodes: int = 0
    missing_edges: int = 0
    duplicate_nodes: int = 0
    duplicate_edges: int = 0
    disconnected_components: int = 0
    graph_completeness: float = 0.0         # 0..1
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────
# Benchmark Report
# ─────────────────────────────────────────────────────────────────────

class GraphBenchmarkReport(BaseModel):
    """Performance measurements for graph operations."""
    graph_build_time_ms: float = 0.0
    incremental_update_time_ms: float = 0.0
    traversal_time_ms: float = 0.0
    query_performance_ms: float = 0.0
    memory_usage_bytes: int = 0
    parser_throughput_files_per_sec: float = 0.0
    snapshot_storage_bytes: int = 0
    node_count: int = 0
    edge_count: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────
# Explainability Trace
# ─────────────────────────────────────────────────────────────────────

class ExplainabilityHop(BaseModel):
    """One hop in an explainability chain."""
    node_id: str
    node_type: str
    edge_type: Optional[str] = None
    reason: str = ""
    confidence: float = 1.0


class ExplainabilityTrace(BaseModel):
    """Complete reasoning chain from a changed file to an impacted job."""
    source_file: str
    target_job: str
    hops: List[ExplainabilityHop] = Field(default_factory=list)
    total_confidence: float = 1.0
    risk_assessment: str = "NORMAL"
