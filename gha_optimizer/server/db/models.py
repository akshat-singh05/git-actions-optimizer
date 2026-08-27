import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, UniqueConstraint, JSON, ForeignKey, Text
from sqlalchemy.orm import relationship
from .session import Base

# -----------------------------------------------------------------------------
# Base Entities
# -----------------------------------------------------------------------------

class Repository(Base):
    __tablename__ = "repositories"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    full_name = Column(String, unique=True, index=True)
    default_branch = Column(String, default="main")
    language = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    is_active = Column(Boolean, default=True)

class Branch(Base):
    __tablename__ = "branches"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), index=True)
    name = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Commit(Base):
    __tablename__ = "commits"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), index=True)
    sha = Column(String, index=True)
    message = Column(Text)
    author = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

# -----------------------------------------------------------------------------
# CI/CD Execution
# -----------------------------------------------------------------------------

class Workflow(Base):
    __tablename__ = "workflows"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), index=True)
    name = Column(String)
    file_path = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, nullable=False, index=True, unique=True)
    workflow_id = Column(Integer, ForeignKey("workflows.id"), nullable=True)
    commit_sha = Column(String)
    repo = Column(String, index=True)
    conclusion = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    timestamp = Column(String)

class WorkflowJob(Base):
    __tablename__ = "workflow_jobs"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), index=True)
    job_id = Column(String, nullable=False)
    conclusion = Column(String)
    duration_seconds = Column(Float)
    cache_hit = Column(Boolean)
    runner_label = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('run_id', 'job_id', name='uq_workflow_jobs_run_job'),
    )

class Runner(Base):
    __tablename__ = "runners"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    label = Column(String, unique=True, index=True)
    cores = Column(Integer)
    memory_gb = Column(Float)
    cost_per_minute = Column(Float)

class CacheEntry(Base):
    __tablename__ = "cache_entries"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), index=True)
    cache_key = Column(String, index=True)
    size_bytes = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_accessed = Column(DateTime)

class Artifact(Base):
    __tablename__ = "artifacts"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), index=True)
    name = Column(String)
    size_bytes = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

# -----------------------------------------------------------------------------
# Dependency Graph
# -----------------------------------------------------------------------------

class GraphSnapshot(Base):
    __tablename__ = "graph_snapshots"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    repo_id = Column(Integer, ForeignKey("repositories.id"), index=True)
    commit_sha = Column(String, index=True)
    graph_json = Column(JSON)  # serialized nx.DiGraph
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class DependencyNode(Base):
    __tablename__ = "dependency_nodes"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    snapshot_id = Column(Integer, ForeignKey("graph_snapshots.id"), index=True)
    node_type = Column(String) # File, Job, Package, CacheKey
    node_name = Column(String)
    attributes = Column(JSON, default=dict)

class DependencyEdge(Base):
    __tablename__ = "dependency_edges"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    snapshot_id = Column(Integer, ForeignKey("graph_snapshots.id"), index=True)
    source_node_id = Column(Integer, ForeignKey("dependency_nodes.id"))
    target_node_id = Column(Integer, ForeignKey("dependency_nodes.id"))
    edge_type = Column(String) # IMPORTS, NEEDS, TOUCHED_BY
    weight = Column(Float, default=1.0)

class GraphEmbedding(Base):
    __tablename__ = "graph_embeddings"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    snapshot_id = Column(Integer, ForeignKey("graph_snapshots.id"), index=True)
    node_id = Column(Integer, ForeignKey("dependency_nodes.id"))
    embedding = Column(JSON) # Array of floats
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

# -----------------------------------------------------------------------------
# Machine Learning
# -----------------------------------------------------------------------------

class FeatureVector(Base):
    __tablename__ = "feature_vectors"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("workflow_jobs.id"))
    features = Column(JSON) # Extracted tabular features
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Prediction(Base):
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, nullable=False, index=True)
    job_id = Column(String, nullable=False)
    predicted_cache_miss = Column(Float)
    predicted_failure = Column(Float)
    predicted_duration = Column(Float)
    actual_cache_miss = Column(Boolean)
    actual_failure = Column(Boolean)
    actual_duration = Column(Float)
    model_version = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ModelVersion(Base):
    __tablename__ = "model_versions"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    model_type = Column(String, nullable=False)
    version = Column(String, nullable=False)
    metrics = Column(JSON, default=dict)
    artifact_path = Column(String)
    training_samples = Column(Integer)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class TrainingDataset(Base):
    __tablename__ = "training_datasets"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    version = Column(String, unique=True)
    s3_path = Column(String)
    num_samples = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    mlflow_run_id = Column(String, index=True)
    model_type = Column(String)
    hyperparameters = Column(JSON)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class EvaluationMetrics(Base):
    __tablename__ = "evaluation_metrics"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    experiment_id = Column(Integer, ForeignKey("experiment_runs.id"))
    metric_name = Column(String)
    metric_value = Column(Float)

# -----------------------------------------------------------------------------
# Optimization & Tracing
# -----------------------------------------------------------------------------

class SchedulerDecision(Base):
    __tablename__ = "scheduler_decisions"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, index=True)
    job_id = Column(String)
    decision = Column(String) # RUN, SKIP, REORDER
    reason = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class OptimizationResult(Base):
    __tablename__ = "optimization_results"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    repo = Column(String)
    workflow_name = Column(String)
    baseline_wall_clock = Column(Float)
    optimized_wall_clock = Column(Float)
    baseline_cost = Column(Float)
    optimized_cost = Column(Float)
    baseline_energy = Column(Float)
    optimized_energy = Column(Float)
    jobs_total = Column(Integer)
    jobs_skipped = Column(Integer)
    plan_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class DecisionTrace(Base):
    __tablename__ = "decision_traces"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, index=True)
    trace_json = Column(JSON)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ContinuousLearningEvent(Base):
    __tablename__ = "continuous_learning_events"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    event_type = Column(String) # DRIFT_DETECTED, RETRAINING_STARTED
    details = Column(JSON)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
