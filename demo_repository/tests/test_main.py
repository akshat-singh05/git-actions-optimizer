"""Tests for main module."""
from src.main import main


def test_main_returns_result():
    result = main()
    assert result is not None
