import datetime
from sqlalchemy import BigInteger, Column, Integer, String, Float, Boolean, DateTime, UniqueConstraint, JSON, ForeignKey, Text
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
    # Real-ingestion metadata (NULL = not yet observed)
    license_spdx = Column(String)
    mirror_path = Column(String)
    last_ingested_at = Column(DateTime)

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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)  # row-insert time, NOT the GitHub run time
    timestamp = Column(String)  # GitHub run created_at (ISO-8601 UTC)

    # --- real GitHub Actions fields; NULL means "not observed", never a default ---
    github_workflow_id = Column(BigInteger, index=True)
    workflow_path = Column(String, index=True)
    workflow_name = Column(String)
    workflow_blob_sha = Column(String)     # blob of the workflow YAML at head_sha (NULL = not resolved)
    event = Column(String, index=True)
    status = Column(String)
    head_branch = Column(String)
    run_number = Column(Integer)
    run_attempt = Column(Integer)
    actor_login = Column(String)
    triggering_actor_login = Column(String)
    gh_created_at = Column(DateTime, index=True)
    gh_run_started_at = Column(DateTime)
    gh_updated_at = Column(DateTime)
    pr_number = Column(Integer)
    pr_base_sha = Column(String)
    diff_source = Column(String)           # 'commit' | 'compare_pr' | NULL (diff not fetched)
    diff_base_sha = Column(String)
    diff_files_total = Column(Integer)
    loc_added = Column(Integer)
    loc_deleted = Column(Integer)
    jobs_fetched_at = Column(DateTime)
    jobs_total = Column(Integer)

class WorkflowJob(Base):
    """One GitHub job (a matrix cell is its own row). ``job_id`` keeps its legacy
    meaning (the job's display name); ``job_key`` is the workflow-YAML key."""
    __tablename__ = "workflow_jobs"
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), index=True)
    job_id = Column(String, nullable=False)
    conclusion = Column(String)
    duration_seconds = Column(Float)       # completed_at - started_at, NULL if either missing
    cache_hit = Column(Boolean)            # NULL = no log-derived label (see cache_label_status)
    runner_label = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # --- real GitHub Actions fields ---
    github_job_id = Column(BigInteger, unique=True, index=True)
    job_name = Column(String)
    job_key = Column(String, index=True)   # NULL = could not be mapped to a YAML job key
    mapping_source = Column(String)        # 'blob_at_sha' | 'unmapped'
    status = Column(String)
    gh_created_at = Column(DateTime)       # when the job was queued
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    queue_seconds = Column(Float)
    runner_name = Column(String)
    runner_group_name = Column(String)
    labels = Column(JSON)
    run_attempt = Column(Integer)
    cache_outcome = Column(String)         # miss | partial_hit | exact_hit | hit_unresolved | NULL
    cache_label_status = Column(String)    # why cache_hit is NULL (or 'labelled')

    __table_args__ = (
        UniqueConstraint('run_id', 'job_id', name='uq_workflow_jobs_run_job'),
    )

class WorkflowStep(Base):
    """Per-step timing/conclusion from the jobs API ``steps`` array."""
    __tablename__ = "workflow_steps"
    id = Column(Integer, primary_key=True, autoincrement=True)
    github_job_id = Column(BigInteger, ForeignKey("workflow_jobs.github_job_id"), index=True, nullable=False)
    number = Column(Integer, nullable=False)
    name = Column(String)
    status = Column(String)
    conclusion = Column(String)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    duration_seconds = Column(Float)

    __table_args__ = (
        UniqueConstraint('github_job_id', 'number', name='uq_workflow_steps_job_number'),
    )

class RunChangedFile(Base):
    """Files changed by a run's diff (see WorkflowRun.diff_source for the scope)."""
    __tablename__ = "run_changed_files"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), index=True, nullable=False)
    filename = Column(String, nullable=False)
    status = Column(String)
    additions = Column(Integer)
    deletions = Column(Integer)
    previous_filename = Column(String)

    __table_args__ = (
        UniqueConstraint('run_id', 'filename', name='uq_run_changed_files'),
    )

class JobCacheEvent(Base):
    """One cache-related line parsed from a real job log."""
    __tablename__ = "job_cache_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    github_job_id = Column(BigInteger, ForeignKey("workflow_jobs.github_job_id"), index=True, nullable=False)
    seq = Column(Integer, nullable=False)
    line_no = Column(Integer)
    step_name = Column(String)             # the '##[group]Run ...' step the line belongs to
    action = Column(String)                # e.g. actions/cache, actions/setup-go
    kind = Column(String, nullable=False)  # restore_hit | restore_miss | saved | primary_hit_not_saved
    cache_key = Column(Text)               # key (or key list) reported on the line
    primary_key = Column(Text)             # 'key:' input from the step header, when present
    restore_kind = Column(String)          # exact | partial | unresolved (restore_hit only)
    raw_line = Column(Text)

    __table_args__ = (
        UniqueConstraint('github_job_id', 'seq', name='uq_job_cache_events'),
    )

class JobLogFetch(Base):
    """Ledger: a job's log is downloaded at most once; the outcome is recorded here."""
    __tablename__ = "job_log_fetches"
    github_job_id = Column(BigInteger, primary_key=True, autoincrement=False)
    run_id = Column(String, index=True)
    status = Column(String, nullable=False)  # parsed_events | parsed_no_events | expired | unavailable |
                                             # skipped_not_cache_capable | skipped_budget | skipped_not_attempted | error
    http_status = Column(Integer)
    bytes_downloaded = Column(BigInteger)
    n_lines = Column(Integer)
    n_cache_events = Column(Integer)
    parser_version = Column(String)
    fetched_at = Column(DateTime, default=datetime.datetime.utcnow)

class WorkflowBlob(Base):
    """Workflow YAML blobs (content-addressed) used to map API job names to YAML job keys."""
    __tablename__ = "workflow_blobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    repo = Column(String, nullable=False, index=True)
    blob_sha = Column(String, nullable=False)
    path = Column(String)
    content = Column(Text)

    __table_args__ = (
        UniqueConstraint('repo', 'blob_sha', name='uq_workflow_blobs'),
    )

class RunGraphFeatures(Base):
    """Graph-derived features for (run, job key), computed on the dependency graph
    built from the repository tree at the run's head sha."""
    __tablename__ = "run_graph_features"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey("workflow_runs.run_id"), index=True, nullable=False)
    job_key = Column(String, nullable=False)
    job_structurally_impacted = Column(Integer)
    cache_key_touched = Column(Integer)
    dep_graph_distance = Column(Float)
    any_changed_file_in_graph = Column(Integer)  # 0 => no changed file was a graph node: no structural information
    job_has_file_links = Column(Integer)          # 0 => the job had no file-level evidence in the graph (impacted is NULL)
    feature_version = Column(Integer, nullable=False, default=1, server_default="1")  # GRAPH_FEATURE_VERSION at compute time
    graph_nodes = Column(Integer)
    graph_edges = Column(Integer)
    computed_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('run_id', 'job_key', name='uq_run_graph_features'),
    )

class IngestionProgress(Base):
    """Resumable-ingestion checkpoints, one row per (repo, stage, key)."""
    __tablename__ = "ingestion_progress"
    id = Column(Integer, primary_key=True, autoincrement=True)
    repo = Column(String, nullable=False, index=True)
    stage = Column(String, nullable=False)
    key = Column(String, nullable=False)
    status = Column(String, nullable=False)
    detail = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('repo', 'stage', 'key', name='uq_ingestion_progress'),
    )

class IngestionRun(Base):
    """One row per CLI/worker ingestion invocation (audit trail of real progress)."""
    __tablename__ = "ingestion_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    repo = Column(String, nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime)
    status = Column(String)
    args = Column(JSON, default=dict)
    counters = Column(JSON, default=dict)

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
