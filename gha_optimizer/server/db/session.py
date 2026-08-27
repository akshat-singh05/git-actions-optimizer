import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import declarative_base

# Default to SQLite if DATABASE_URL is not set, for backward compatibility
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///gha_optimizer.db")

engine = create_engine(
    DATABASE_URL, 
    # check_same_thread is needed only for SQLite
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
