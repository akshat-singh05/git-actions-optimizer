"""Shared utility functions."""
import json
import re


def format_output(data) -> str:
    """Format data for output."""
    if isinstance(data, dict):
        return json.dumps(data, indent=2)
    return str(data)


def sanitize(text: str) -> str:
    """Remove special characters from text."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "", text)


def parse_config(config_str: str) -> dict:
    """Parse a JSON configuration string."""
    return json.loads(config_str)
