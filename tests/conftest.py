"""
Shared test fixtures for gha-optimizer.

Provides: sample workflow YAML, parsed WorkflowSpec, synthetic history,
graph instances, and database sessions for testing.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from gha_optimizer.ingestion.workflow_parser import parse_workflow, WorkflowSpec
from gha_optimizer.ingestion.github_client import WorkflowRunRecord


# tests/fixtures holds data (a toy repo with its own test files, real log excerpts): never collect it
collect_ignore_glob = ["fixtures/*"]


# ─── Sample Workflow YAML ───────────────────────────────────────────

SAMPLE_WORKFLOW_YAML = {
    "name": "CI",
    "on": {
        "push": {"branches": ["main"]},
        "pull_request": None,
    },
    "jobs": {
        "lint": {
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {
                    "uses": "actions/cache@v4",
                    "with": {
                        "path": "~/.cache/pip",
                        "key": "pip-${{ hashFiles('requirements.txt') }}",
                    },
                },
                {"run": "pip install -r requirements.txt"},
                {"run": "ruff check ."},
            ],
        },
        "unit_tests": {
            "needs": "lint",
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {
                    "uses": "actions/cache@v4",
                    "with": {
                        "path": "~/.cache/pip",
                        "key": "pip-${{ hashFiles('requirements.txt') }}",
                    },
                },
                {"run": "pip install -r requirements.txt"},
                {"run": "pytest tests/unit"},
            ],
        },
        "integration_tests": {
            "needs": "lint",
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {"run": "pip install -r requirements.txt"},
                {"run": "pytest tests/integration"},
            ],
        },
        "build": {
            "needs": ["unit_tests", "integration_tests"],
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {"run": "python -m build"},
            ],
        },
        "deploy": {
            "needs": "build",
            "runs-on": "ubuntu-latest",
            "environment": "production",
            "steps": [
                {"uses": "actions/checkout@v4"},
                {"run": "./deploy.sh"},
            ],
        },
    },
}


# ─── Source Files ───────────────────────────────────────────────────

SAMPLE_SOURCE_FILES = [
    "src/app.py",
    "src/utils.py",
    "src/api/routes.py",
    "tests/unit/test_app.py",
    "tests/integration/test_api.py",
    "requirements.txt",
]

SAMPLE_CHANGED_FILES = ["src/api/routes.py", "requirements.txt"]


# ─── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def sample_workflow_path(tmp_path: Path) -> Path:
    """Write sample workflow YAML to a temp file and return its path."""
    wf_path = tmp_path / ".github" / "workflows" / "ci.yml"
    wf_path.parent.mkdir(parents=True, exist_ok=True)
    wf_path.write_text(yaml.dump(SAMPLE_WORKFLOW_YAML), encoding="utf-8")
    return wf_path


@pytest.fixture
def sample_workflow(sample_workflow_path: Path) -> WorkflowSpec:
    """Parse the sample workflow YAML into a WorkflowSpec."""
    return parse_workflow(sample_workflow_path)


@pytest.fixture
def sample_repo_root(tmp_path: Path) -> Path:
    """Create a minimal sample repository structure."""
    # Create source files
    for f in SAMPLE_SOURCE_FILES:
        fp = tmp_path / f
        fp.parent.mkdir(parents=True, exist_ok=True)
        if f.endswith(".py"):
            fp.write_text(
                '"""Auto-generated test file."""\n\ndef placeholder():\n    pass\n',
                encoding="utf-8",
            )
        elif f == "requirements.txt":
            fp.write_text("requests>=2.31\npytest>=8.0\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def synthetic_history() -> list[WorkflowRunRecord]:
    """SYNTHETIC test input (seeded RNG) — wiring tests only, never a result."""
    from tests.fixtures.synthetic_history import generate_synthetic_test_history
    return generate_synthetic_test_history(n_runs=30, seed=42)


@pytest.fixture
def sample_history() -> list[WorkflowRunRecord]:
    """SYNTHETIC test input (seeded RNG) — wiring tests only, never a result."""
    records = []
    import random
    rng = random.Random(99)
    jobs = ["lint", "unit_tests", "integration_tests", "build", "deploy"]
    for i in range(50):
        for job_id in jobs:
            records.append(
                WorkflowRunRecord(
                    run_id=f"run_{i}",
                    commit_sha=f"sha_{i:04d}",
                    files_changed=["src/app.py"],
                    job_id=job_id,
                    duration_seconds=rng.uniform(20, 300),
                    conclusion="success" if rng.random() > 0.1 else "failure",
                    cache_hit=rng.random() > 0.3,
                    author="test_user",
                    timestamp=f"2026-01-{(i % 28) + 1:02d}T12:00:00Z",
                    branch="main",
                    runner_label="ubuntu-latest",
                    loc_added=rng.randint(1, 100),
                    loc_deleted=rng.randint(0, 50),
                )
            )
    return records
