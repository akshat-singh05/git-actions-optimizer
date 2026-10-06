"""Real-ingestion schema: reconcile 001 with the ORM and add the real-data tables

Migration 001 diverged from ``gha_optimizer.server.db.models`` (13 ORM tables were
never created; ``workflow_runs``, ``workflow_jobs``, ``commits``, ``branches``,
``predictions`` and ``graph_snapshots`` had incompatible shapes).  No ORM-readable
data can exist in those six legacy tables, so they are dropped and recreated in
the ORM's shape.  ``repositories`` only gains columns.

New real-data tables: workflow_steps, run_changed_files, job_cache_events,
job_log_fetches, workflow_blobs, run_graph_features, ingestion_progress,
ingestion_runs.

Revision ID: 002_real_ingestion
Revises: 001_initial
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_real_ingestion"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_TABLES = (
    "workflow_jobs",
    "workflow_runs",
    "predictions",
    "graph_snapshots",
    "commits",
    "branches",
)

NEW_TABLES_DROP_ORDER = (
    "ingestion_runs",
    "ingestion_progress",
    "run_graph_features",
    "workflow_blobs",
    "job_log_fetches",
    "job_cache_events",
    "run_changed_files",
    "workflow_steps",
    "evaluation_metrics",
    "experiment_runs",
    "training_datasets",
    "feature_vectors",
    "decision_traces",
    "scheduler_decisions",
    "graph_embeddings",
    "dependency_edges",
    "dependency_nodes",
    "artifacts",
    "cache_entries",
    "runners",
    "workflows",
    "workflow_jobs",
    "workflow_runs",
    "predictions",
    "graph_snapshots",
    "commits",
    "branches",
)


def upgrade() -> None:
    for table in LEGACY_TABLES:
        op.drop_table(table)

    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("license_spdx", sa.String(), nullable=True))
        batch.add_column(sa.Column("mirror_path", sa.String(), nullable=True))
        batch.add_column(sa.Column("last_ingested_at", sa.DateTime(), nullable=True))
    op.create_index(op.f("ix_repositories_id"), "repositories", ["id"], unique=False)

    # Index / nullability drift in tables that 001 did create (ORM is the reference).
    op.drop_index("ix_continuous_learning_events_event_type", table_name="continuous_learning_events")
    op.create_index(op.f("ix_continuous_learning_events_id"), "continuous_learning_events", ["id"], unique=False)
    op.drop_index("ix_optimization_results_repo", table_name="optimization_results")
    op.create_index(op.f("ix_optimization_results_id"), "optimization_results", ["id"], unique=False)
    op.drop_index("ix_model_versions_model_type", table_name="model_versions")
    op.create_index(op.f("ix_model_versions_id"), "model_versions", ["id"], unique=False)
    with op.batch_alter_table("model_versions") as batch:
        batch.alter_column("model_type", existing_type=sa.String(), nullable=False)
        batch.alter_column("version", existing_type=sa.String(), nullable=False)

    op.create_table('decision_traces',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=True),
    sa.Column('trace_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_decision_traces_id'), 'decision_traces', ['id'], unique=False)
    op.create_index(op.f('ix_decision_traces_run_id'), 'decision_traces', ['run_id'], unique=False)
    op.create_table('experiment_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('mlflow_run_id', sa.String(), nullable=True),
    sa.Column('model_type', sa.String(), nullable=True),
    sa.Column('hyperparameters', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_experiment_runs_id'), 'experiment_runs', ['id'], unique=False)
    op.create_index(op.f('ix_experiment_runs_mlflow_run_id'), 'experiment_runs', ['mlflow_run_id'], unique=False)
    op.create_table('ingestion_progress',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo', sa.String(), nullable=False),
    sa.Column('stage', sa.String(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('repo', 'stage', 'key', name='uq_ingestion_progress')
    )
    op.create_index(op.f('ix_ingestion_progress_repo'), 'ingestion_progress', ['repo'], unique=False)
    op.create_table('ingestion_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo', sa.String(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('args', sa.JSON(), nullable=True),
    sa.Column('counters', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ingestion_runs_repo'), 'ingestion_runs', ['repo'], unique=False)
    op.create_table('job_log_fetches',
    sa.Column('github_job_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('run_id', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('bytes_downloaded', sa.BigInteger(), nullable=True),
    sa.Column('n_lines', sa.Integer(), nullable=True),
    sa.Column('n_cache_events', sa.Integer(), nullable=True),
    sa.Column('parser_version', sa.String(), nullable=True),
    sa.Column('fetched_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('github_job_id')
    )
    op.create_index(op.f('ix_job_log_fetches_run_id'), 'job_log_fetches', ['run_id'], unique=False)
    op.create_table('predictions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('job_id', sa.String(), nullable=False),
    sa.Column('predicted_cache_miss', sa.Float(), nullable=True),
    sa.Column('predicted_failure', sa.Float(), nullable=True),
    sa.Column('predicted_duration', sa.Float(), nullable=True),
    sa.Column('actual_cache_miss', sa.Boolean(), nullable=True),
    sa.Column('actual_failure', sa.Boolean(), nullable=True),
    sa.Column('actual_duration', sa.Float(), nullable=True),
    sa.Column('model_version', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_predictions_id'), 'predictions', ['id'], unique=False)
    op.create_index(op.f('ix_predictions_run_id'), 'predictions', ['run_id'], unique=False)
    op.create_table('runners',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('cores', sa.Integer(), nullable=True),
    sa.Column('memory_gb', sa.Float(), nullable=True),
    sa.Column('cost_per_minute', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_runners_id'), 'runners', ['id'], unique=False)
    op.create_index(op.f('ix_runners_label'), 'runners', ['label'], unique=True)
    op.create_table('scheduler_decisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=True),
    sa.Column('job_id', sa.String(), nullable=True),
    sa.Column('decision', sa.String(), nullable=True),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scheduler_decisions_id'), 'scheduler_decisions', ['id'], unique=False)
    op.create_index(op.f('ix_scheduler_decisions_run_id'), 'scheduler_decisions', ['run_id'], unique=False)
    op.create_table('training_datasets',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('version', sa.String(), nullable=True),
    sa.Column('s3_path', sa.String(), nullable=True),
    sa.Column('num_samples', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('version')
    )
    op.create_index(op.f('ix_training_datasets_id'), 'training_datasets', ['id'], unique=False)
    op.create_table('workflow_blobs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo', sa.String(), nullable=False),
    sa.Column('blob_sha', sa.String(), nullable=False),
    sa.Column('path', sa.String(), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('repo', 'blob_sha', name='uq_workflow_blobs')
    )
    op.create_index(op.f('ix_workflow_blobs_repo'), 'workflow_blobs', ['repo'], unique=False)
    op.create_table('branches',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_branches_id'), 'branches', ['id'], unique=False)
    op.create_index(op.f('ix_branches_name'), 'branches', ['name'], unique=False)
    op.create_index(op.f('ix_branches_repo_id'), 'branches', ['repo_id'], unique=False)
    op.create_table('cache_entries',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Integer(), nullable=True),
    sa.Column('cache_key', sa.String(), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('last_accessed', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cache_entries_cache_key'), 'cache_entries', ['cache_key'], unique=False)
    op.create_index(op.f('ix_cache_entries_id'), 'cache_entries', ['id'], unique=False)
    op.create_index(op.f('ix_cache_entries_repo_id'), 'cache_entries', ['repo_id'], unique=False)
    op.create_table('commits',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Integer(), nullable=True),
    sa.Column('sha', sa.String(), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('author', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_commits_id'), 'commits', ['id'], unique=False)
    op.create_index(op.f('ix_commits_repo_id'), 'commits', ['repo_id'], unique=False)
    op.create_index(op.f('ix_commits_sha'), 'commits', ['sha'], unique=False)
    op.create_table('evaluation_metrics',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('experiment_id', sa.Integer(), nullable=True),
    sa.Column('metric_name', sa.String(), nullable=True),
    sa.Column('metric_value', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['experiment_id'], ['experiment_runs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_evaluation_metrics_id'), 'evaluation_metrics', ['id'], unique=False)
    op.create_table('graph_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Integer(), nullable=True),
    sa.Column('commit_sha', sa.String(), nullable=True),
    sa.Column('graph_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_graph_snapshots_commit_sha'), 'graph_snapshots', ['commit_sha'], unique=False)
    op.create_index(op.f('ix_graph_snapshots_id'), 'graph_snapshots', ['id'], unique=False)
    op.create_index(op.f('ix_graph_snapshots_repo_id'), 'graph_snapshots', ['repo_id'], unique=False)
    op.create_table('workflows',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('file_path', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['repo_id'], ['repositories.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_workflows_id'), 'workflows', ['id'], unique=False)
    op.create_index(op.f('ix_workflows_repo_id'), 'workflows', ['repo_id'], unique=False)
    op.create_table('dependency_nodes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=True),
    sa.Column('node_type', sa.String(), nullable=True),
    sa.Column('node_name', sa.String(), nullable=True),
    sa.Column('attributes', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['snapshot_id'], ['graph_snapshots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dependency_nodes_id'), 'dependency_nodes', ['id'], unique=False)
    op.create_index(op.f('ix_dependency_nodes_snapshot_id'), 'dependency_nodes', ['snapshot_id'], unique=False)
    op.create_table('workflow_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('workflow_id', sa.Integer(), nullable=True),
    sa.Column('commit_sha', sa.String(), nullable=True),
    sa.Column('repo', sa.String(), nullable=True),
    sa.Column('conclusion', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('timestamp', sa.String(), nullable=True),
    sa.Column('github_workflow_id', sa.BigInteger(), nullable=True),
    sa.Column('workflow_path', sa.String(), nullable=True),
    sa.Column('workflow_name', sa.String(), nullable=True),
    sa.Column('workflow_blob_sha', sa.String(), nullable=True),
    sa.Column('event', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('head_branch', sa.String(), nullable=True),
    sa.Column('run_number', sa.Integer(), nullable=True),
    sa.Column('run_attempt', sa.Integer(), nullable=True),
    sa.Column('actor_login', sa.String(), nullable=True),
    sa.Column('triggering_actor_login', sa.String(), nullable=True),
    sa.Column('gh_created_at', sa.DateTime(), nullable=True),
    sa.Column('gh_run_started_at', sa.DateTime(), nullable=True),
    sa.Column('gh_updated_at', sa.DateTime(), nullable=True),
    sa.Column('pr_number', sa.Integer(), nullable=True),
    sa.Column('pr_base_sha', sa.String(), nullable=True),
    sa.Column('diff_source', sa.String(), nullable=True),
    sa.Column('diff_base_sha', sa.String(), nullable=True),
    sa.Column('diff_files_total', sa.Integer(), nullable=True),
    sa.Column('loc_added', sa.Integer(), nullable=True),
    sa.Column('loc_deleted', sa.Integer(), nullable=True),
    sa.Column('jobs_fetched_at', sa.DateTime(), nullable=True),
    sa.Column('jobs_total', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_workflow_runs_event'), 'workflow_runs', ['event'], unique=False)
    op.create_index(op.f('ix_workflow_runs_gh_created_at'), 'workflow_runs', ['gh_created_at'], unique=False)
    op.create_index(op.f('ix_workflow_runs_github_workflow_id'), 'workflow_runs', ['github_workflow_id'], unique=False)
    op.create_index(op.f('ix_workflow_runs_id'), 'workflow_runs', ['id'], unique=False)
    op.create_index(op.f('ix_workflow_runs_repo'), 'workflow_runs', ['repo'], unique=False)
    op.create_index(op.f('ix_workflow_runs_run_id'), 'workflow_runs', ['run_id'], unique=True)
    op.create_index(op.f('ix_workflow_runs_workflow_path'), 'workflow_runs', ['workflow_path'], unique=False)
    op.create_table('artifacts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_artifacts_id'), 'artifacts', ['id'], unique=False)
    op.create_index(op.f('ix_artifacts_run_id'), 'artifacts', ['run_id'], unique=False)
    op.create_table('dependency_edges',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=True),
    sa.Column('source_node_id', sa.Integer(), nullable=True),
    sa.Column('target_node_id', sa.Integer(), nullable=True),
    sa.Column('edge_type', sa.String(), nullable=True),
    sa.Column('weight', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['snapshot_id'], ['graph_snapshots.id'], ),
    sa.ForeignKeyConstraint(['source_node_id'], ['dependency_nodes.id'], ),
    sa.ForeignKeyConstraint(['target_node_id'], ['dependency_nodes.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_dependency_edges_id'), 'dependency_edges', ['id'], unique=False)
    op.create_index(op.f('ix_dependency_edges_snapshot_id'), 'dependency_edges', ['snapshot_id'], unique=False)
    op.create_table('graph_embeddings',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=True),
    sa.Column('node_id', sa.Integer(), nullable=True),
    sa.Column('embedding', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['dependency_nodes.id'], ),
    sa.ForeignKeyConstraint(['snapshot_id'], ['graph_snapshots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_graph_embeddings_id'), 'graph_embeddings', ['id'], unique=False)
    op.create_index(op.f('ix_graph_embeddings_snapshot_id'), 'graph_embeddings', ['snapshot_id'], unique=False)
    op.create_table('run_changed_files',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('filename', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('additions', sa.Integer(), nullable=True),
    sa.Column('deletions', sa.Integer(), nullable=True),
    sa.Column('previous_filename', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'filename', name='uq_run_changed_files')
    )
    op.create_index(op.f('ix_run_changed_files_run_id'), 'run_changed_files', ['run_id'], unique=False)
    op.create_table('run_graph_features',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('job_key', sa.String(), nullable=False),
    sa.Column('job_structurally_impacted', sa.Integer(), nullable=True),
    sa.Column('cache_key_touched', sa.Integer(), nullable=True),
    sa.Column('dep_graph_distance', sa.Float(), nullable=True),
    sa.Column('any_changed_file_in_graph', sa.Integer(), nullable=True),
    sa.Column('graph_nodes', sa.Integer(), nullable=True),
    sa.Column('graph_edges', sa.Integer(), nullable=True),
    sa.Column('computed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'job_key', name='uq_run_graph_features')
    )
    op.create_index(op.f('ix_run_graph_features_run_id'), 'run_graph_features', ['run_id'], unique=False)
    op.create_table('workflow_jobs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=True),
    sa.Column('job_id', sa.String(), nullable=False),
    sa.Column('conclusion', sa.String(), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('cache_hit', sa.Boolean(), nullable=True),
    sa.Column('runner_label', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('github_job_id', sa.BigInteger(), nullable=True),
    sa.Column('job_name', sa.String(), nullable=True),
    sa.Column('job_key', sa.String(), nullable=True),
    sa.Column('mapping_source', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('gh_created_at', sa.DateTime(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('queue_seconds', sa.Float(), nullable=True),
    sa.Column('runner_name', sa.String(), nullable=True),
    sa.Column('runner_group_name', sa.String(), nullable=True),
    sa.Column('labels', sa.JSON(), nullable=True),
    sa.Column('run_attempt', sa.Integer(), nullable=True),
    sa.Column('cache_outcome', sa.String(), nullable=True),
    sa.Column('cache_label_status', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['workflow_runs.run_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'job_id', name='uq_workflow_jobs_run_job')
    )
    op.create_index(op.f('ix_workflow_jobs_github_job_id'), 'workflow_jobs', ['github_job_id'], unique=True)
    op.create_index(op.f('ix_workflow_jobs_id'), 'workflow_jobs', ['id'], unique=False)
    op.create_index(op.f('ix_workflow_jobs_job_key'), 'workflow_jobs', ['job_key'], unique=False)
    op.create_index(op.f('ix_workflow_jobs_run_id'), 'workflow_jobs', ['run_id'], unique=False)
    op.create_table('feature_vectors',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.Integer(), nullable=True),
    sa.Column('features', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['job_id'], ['workflow_jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_feature_vectors_id'), 'feature_vectors', ['id'], unique=False)
    op.create_table('job_cache_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('github_job_id', sa.BigInteger(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('line_no', sa.Integer(), nullable=True),
    sa.Column('step_name', sa.String(), nullable=True),
    sa.Column('action', sa.String(), nullable=True),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('cache_key', sa.Text(), nullable=True),
    sa.Column('primary_key', sa.Text(), nullable=True),
    sa.Column('restore_kind', sa.String(), nullable=True),
    sa.Column('raw_line', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['github_job_id'], ['workflow_jobs.github_job_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('github_job_id', 'seq', name='uq_job_cache_events')
    )
    op.create_index(op.f('ix_job_cache_events_github_job_id'), 'job_cache_events', ['github_job_id'], unique=False)
    op.create_table('workflow_steps',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('github_job_id', sa.BigInteger(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('conclusion', sa.String(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['github_job_id'], ['workflow_jobs.github_job_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('github_job_id', 'number', name='uq_workflow_steps_job_number')
    )
    op.create_index(op.f('ix_workflow_steps_github_job_id'), 'workflow_steps', ['github_job_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    for table in NEW_TABLES_DROP_ORDER:
        op.drop_table(table)

    with op.batch_alter_table("model_versions") as batch:
        batch.alter_column("version", existing_type=sa.String(), nullable=True)
        batch.alter_column("model_type", existing_type=sa.String(), nullable=True)
    op.drop_index(op.f("ix_model_versions_id"), table_name="model_versions")
    op.create_index("ix_model_versions_model_type", "model_versions", ["model_type"], unique=False)
    op.drop_index(op.f("ix_optimization_results_id"), table_name="optimization_results")
    op.create_index("ix_optimization_results_repo", "optimization_results", ["repo"], unique=False)
    op.drop_index(op.f("ix_continuous_learning_events_id"), table_name="continuous_learning_events")
    op.create_index("ix_continuous_learning_events_event_type", "continuous_learning_events", ["event_type"], unique=False)
    op.drop_index(op.f("ix_repositories_id"), table_name="repositories")

    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("last_ingested_at")
        batch.drop_column("mirror_path")
        batch.drop_column("license_spdx")

    # Recreate the legacy 001 shapes.
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo_id", sa.Integer, sa.ForeignKey("repositories.id")),
        sa.Column("name", sa.String, index=True),
        sa.Column("is_default", sa.Boolean, default=False),
    )
    op.create_table(
        "commits",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo_id", sa.Integer, sa.ForeignKey("repositories.id")),
        sa.Column("sha", sa.String, index=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("author", sa.String, nullable=True),
        sa.Column("timestamp", sa.DateTime, nullable=True),
        sa.Column("files_changed", sa.JSON, nullable=True),
        sa.Column("loc_added", sa.Integer, default=0),
        sa.Column("loc_deleted", sa.Integer, default=0),
    )
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String, primary_key=True),
        sa.Column("commit_sha", sa.String, index=True, nullable=True),
        sa.Column("repo", sa.String, index=True, nullable=True),
        sa.Column("job_id", sa.String, nullable=True),
        sa.Column("conclusion", sa.String, nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("cache_hit", sa.Boolean, default=False),
        sa.Column("author", sa.String, nullable=True),
        sa.Column("branch", sa.String, nullable=True),
        sa.Column("runner_label", sa.String, nullable=True),
        sa.Column("files_changed", sa.JSON, nullable=True),
        sa.Column("loc_added", sa.Integer, default=0),
        sa.Column("loc_deleted", sa.Integer, default=0),
        sa.Column("timestamp", sa.String, nullable=True),
    )
    op.create_table(
        "workflow_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String, index=True),
        sa.Column("job_name", sa.String),
        sa.Column("conclusion", sa.String, nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("runner_label", sa.String, nullable=True),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo", sa.String, index=True, nullable=True),
        sa.Column("commit_sha", sa.String, nullable=True),
        sa.Column("job_id", sa.String, nullable=True),
        sa.Column("predicted_cache_miss", sa.Float, nullable=True),
        sa.Column("predicted_failure", sa.Float, nullable=True),
        sa.Column("predicted_duration", sa.Float, nullable=True),
        sa.Column("actual_cache_miss", sa.Integer, nullable=True),
        sa.Column("actual_failure", sa.Integer, nullable=True),
        sa.Column("actual_duration", sa.Float, nullable=True),
        sa.Column("model_version", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime),
    )
    op.create_table(
        "graph_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo_id", sa.Integer, nullable=True),
        sa.Column("commit_sha", sa.String, index=True, nullable=True),
        sa.Column("graph_json", sa.JSON, nullable=True),
        sa.Column("node_count", sa.Integer, nullable=True),
        sa.Column("edge_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime),
    )
