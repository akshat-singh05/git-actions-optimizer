"""run_graph_features: feature_version + job_has_file_links

``feature_version`` lets a changed graph-feature definition invalidate rows computed under the
old one (``build-dataset`` recomputes rows whose version differs from ``GRAPH_FEATURE_VERSION``).
Existing rows are version 1 (the original definition: source files only, no step commands).
``job_has_file_links`` records whether the job had any file-level evidence in the graph; NULL
``job_structurally_impacted`` now means "unknown", not "all jobs impacted".

Revision ID: 003_graph_feature_version
Revises: 002_real_ingestion
Create Date: 2026-10-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_graph_feature_version"
down_revision: Union[str, None] = "002_real_ingestion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("run_graph_features") as batch:
        batch.add_column(sa.Column("feature_version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("job_has_file_links", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_graph_features") as batch:
        batch.drop_column("job_has_file_links")
        batch.drop_column("feature_version")
