"""
SYNTHETIC TEST FIXTURE — NOT DATA.

Deterministic, randomly generated ``WorkflowRunRecord`` objects used ONLY by unit tests
that need a fixed input (feature-frame shape, pipeline wiring).  Nothing here is a
measurement: the failure, cache and duration "behaviour" is an arbitrary function of a
seeded RNG.  It must never be imported from ``gha_optimizer/``, the CLI, the server or any
demo, and no number derived from it may be displayed as a result.  Real history comes from
``gha-optimizer ingest`` (see ``gha_optimizer/ingestion``).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from gha_optimizer.ingestion.github_client import WorkflowRunRecord

JOBS = ["lint", "unit_tests", "integration_tests", "build", "deploy"]
FILE_POOL = [
    "src/app.py",
    "src/utils.py",
    "src/api/routes.py",
    "tests/unit/test_app.py",
    "tests/integration/test_api.py",
    "requirements.txt",
    "README.md",
]
AUTHORS = ["akshat", "contributor_a", "contributor_b"]

# Bias: integration_tests fails more often, deploy is slow, lint is flaky
# on Fridays-style patterns are intentionally baked in so the models have
# something real to learn from.
FAILURE_BIAS = {
    "lint": 0.05,
    "unit_tests": 0.08,
    "integration_tests": 0.22,
    "build": 0.06,
    "deploy": 0.04,
}
DURATION_BASE = {
    "lint": 30,
    "unit_tests": 90,
    "integration_tests": 240,
    "build": 120,
    "deploy": 60,
}
CACHE_HIT_BIAS = {
    "lint": 0.85,
    "unit_tests": 0.85,
    "integration_tests": 0.85,
    "build": 0.6,
    "deploy": 0.6,
}

# LOC delta ranges per file type
LOC_RANGES = {
    "src/": (5, 150),
    "tests/": (3, 80),
    "requirements.txt": (1, 5),
    "README.md": (1, 20),
}


def _loc_for_file(rng: random.Random, file_path: str) -> tuple[int, int]:
    """Generate plausible LOC added/deleted for a file."""
    for prefix, (lo, hi) in LOC_RANGES.items():
        if file_path.startswith(prefix) or file_path == prefix.rstrip("/"):
            added = rng.randint(lo, hi)
            deleted = rng.randint(0, added // 2)
            return added, deleted
    return rng.randint(1, 50), rng.randint(0, 20)


def generate_synthetic_test_history(n_runs: int = 150, seed: int = 42) -> list[WorkflowRunRecord]:
    """Generate SYNTHETIC history for unit tests only (see module docstring)."""
    rng = random.Random(seed)
    records: list[WorkflowRunRecord] = []
    base_time = datetime(2026, 1, 1)

    for i in range(n_runs):
        run_id = f"run_{i}"
        sha = f"sha_{i:04d}"
        n_files = rng.randint(1, 4)
        files_changed = rng.sample(FILE_POOL, n_files)
        touches_requirements = "requirements.txt" in files_changed
        author = rng.choice(AUTHORS)
        ts = base_time + timedelta(hours=i * 3)
        day_of_week = ts.weekday()

        # LOC stats
        total_added = 0
        total_deleted = 0
        for f in files_changed:
            a, d = _loc_for_file(rng, f)
            total_added += a
            total_deleted += d

        for job_id in JOBS:
            fail_p = FAILURE_BIAS[job_id]
            # more files touched -> slightly higher failure chance
            fail_p += 0.02 * (n_files - 1)
            # Weekend effect for integration tests
            if day_of_week >= 5 and job_id == "integration_tests":
                fail_p += 0.05
            failed = rng.random() < fail_p

            cache_hit_p = CACHE_HIT_BIAS[job_id]
            if touches_requirements:
                cache_hit_p -= 0.5  # changing the lockfile invalidates cache
            cache_hit = rng.random() < max(cache_hit_p, 0.02)

            duration = DURATION_BASE[job_id] * rng.uniform(0.8, 1.3)
            if not cache_hit:
                duration *= rng.uniform(1.4, 2.0)  # cold install is slower
            if failed:
                duration *= rng.uniform(0.5, 0.9)  # fails fast sometimes

            records.append(
                WorkflowRunRecord(
                    run_id=run_id,
                    commit_sha=sha,
                    files_changed=files_changed,
                    job_id=job_id,
                    duration_seconds=round(duration, 1),
                    conclusion="failure" if failed else "success",
                    cache_hit=cache_hit,
                    author=author,
                    timestamp=ts.isoformat(),
                    branch="main" if rng.random() < 0.7 else "feature-branch",
                    runner_label="ubuntu-latest",
                    workflow_path=".github/workflows/ci.yml",
                    event="push",
                    loc_added=total_added,
                    loc_deleted=total_deleted,
                )
            )
    return records
