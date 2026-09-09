"""Tests for service module."""
from src.service import process_request, validate_input


def test_process_request():
    result = process_request("test_data")
    assert result["status"] == "ok"


def test_validate_input():
    assert validate_input("valid") is True
    assert validate_input("") is False
