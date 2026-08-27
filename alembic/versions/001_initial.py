"""Initial schema — all tables

Revision ID: 001_initial
Revises:
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Repositories
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("full_name", sa.String, unique=True, index=True),
        sa.Column("default_branch", sa.String, default="main"),
        sa.Column("language", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime),
        sa.Column("is_active", sa.Boolean, default=True),
    )

    # Branches
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo_id", sa.Integer, sa.ForeignKey("repositories.id")),
        sa.Column("name", sa.String, index=True),
        sa.Column("is_default", sa.Boolean, default=False),
    )

    # Commits
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

    # Workflow runs
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

    # Workflow jobs
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

    # Predictions
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

    # Optimization results
    op.create_table(
        "optimization_results",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("repo", sa.String, index=True, nullable=True),
        sa.Column("workflow_name", sa.String, nullable=True),
        sa.Column("baseline_wall_clock", sa.Float, nullable=True),
        sa.Column("optimized_wall_clock", sa.Float, nullable=True),
        sa.Column("baseline_cost", sa.Float, nullable=True),
        sa.Column("optimized_cost", sa.Float, nullable=True),
        sa.Column("baseline_energy", sa.Float, nullable=True),
        sa.Column("optimized_energy", sa.Float, nullable=True),
        sa.Column("jobs_total", sa.Integer, nullable=True),
        sa.Column("jobs_skipped", sa.Integer, nullable=True),
        sa.Column("plan_json", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime),
    )

    # Model versions
    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("model_type", sa.String, index=True),
        sa.Column("version", sa.String),
        sa.Column("metrics", sa.JSON, nullable=True),
        sa.Column("artifact_path", sa.String, nullable=True),
        sa.Column("training_samples", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime),
    )

    # Graph snapshots
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

    # Continuous learning events
    op.create_table(
        "continuous_learning_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String, index=True),
        sa.Column("details", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime),
    )


def downgrade() -> None:
    op.drop_table("continuous_learning_events")
    op.drop_table("graph_snapshots")
    op.drop_table("model_versions")
    op.drop_table("optimization_results")
    op.drop_table("predictions")
    op.drop_table("workflow_jobs")
    op.drop_table("workflow_runs")
    op.drop_table("commits")
    op.drop_table("branches")
    op.drop_table("repositories")
