import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import DeclarativeBase

# Default to SQLite if DATABASE_URL is not set, for backward compatibility
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///gha_optimizer.db")

engine = create_engine(
    DATABASE_URL,
    # check_same_thread is needed only for SQLite; the 60 s busy timeout lets several
    # ingestion processes share one SQLite file (writers queue instead of failing)
    connect_args={"check_same_thread": False, "timeout": 60} if DATABASE_URL.startswith("sqlite") else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Declarative base of every table (SQLAlchemy 2.0 form: a real class, so type checkers accept it as a base class)."""

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create any missing tables from the ORM (idempotent).

    Convenience for local/dev databases.  Managed deployments should use
    ``alembic upgrade head``; this is a no-op for tables that already exist.
    """
    from . import models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
