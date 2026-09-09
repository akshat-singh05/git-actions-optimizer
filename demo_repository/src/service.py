"""Service layer — business logic."""
from src.database import get_connection, query_data
from src.utils import format_output


def process_request(input_data: str) -> dict:
    """Process an incoming request using database and utils."""
    conn = get_connection()
    raw = query_data(conn, input_data)
    return {"status": "ok", "data": format_output(raw)}


def validate_input(data: str) -> bool:
    """Validate incoming data."""
    return bool(data and len(data) > 0)
