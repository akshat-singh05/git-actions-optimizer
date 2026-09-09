"""Database access layer."""
import os


def get_connection():
    """Get a database connection."""
    db_url = os.environ.get("DATABASE_URL", "sqlite:///demo.db")
    return {"url": db_url, "connected": True}


def query_data(connection: dict, query: str) -> str:
    """Execute a query against the database."""
    if not connection.get("connected"):
        raise ConnectionError("Not connected to database")
    return f"result_for_{query}"


def migrate():
    """Run database migrations."""
    conn = get_connection()
    return conn
