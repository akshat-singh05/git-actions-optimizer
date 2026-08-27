"""
Alembic environment configuration.

Reads DATABASE_URL from the environment (falling back to SQLite for
development) and runs migrations against the ORM models defined in
gha_optimizer.server.db.models.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the declarative Base so Alembic can detect all models
from gha_optimizer.server.db.session import Base
from gha_optimizer.server.db import models as _models  # noqa: F401  (side-effect import)

config = context.config

# Override sqlalchemy.url from environment if set
db_url = os.environ.get("DATABASE_URL", "sqlite:///gha_optimizer.db")
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL only)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (execute against a live DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
