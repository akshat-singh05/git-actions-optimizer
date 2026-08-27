"""
Parses a GitHub Actions workflow YAML file into a normalized in-memory
representation: a job DAG (nodes = jobs, edges = ``needs:`` dependencies),
plus per-job metadata (runner label, steps, cache actions, matrix config,
path filters, concurrency groups).

This is Layer 1 (Data Ingestion) of the architecture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class MatrixConfig:
    """Parsed ``strategy.matrix`` configuration for a job."""

    axes: dict[str, list[Any]] = field(default_factory=dict)
    include: list[dict[str, Any]] = field(default_factory=list)
    exclude: list[dict[str, Any]] = field(default_factory=list)
    fail_fast: bool = True
    max_parallel: int | None = None

    @property
    def cell_count(self) -> int:
        """Total number of matrix cells (before exclude/include adjustments)."""
        if not self.axes:
            return 1
        count = 1
        for values in self.axes.values():
            count *= len(values)
        count -= len(self.exclude)
        count += len(self.include)
        return max(count, 1)

    def enumerate_cells(self) -> list[dict[str, Any]]:
        """Enumerate all concrete matrix-cell combinations."""
        if not self.axes:
            return [{}]

        import itertools

        keys = list(self.axes.keys())
        combos = list(itertools.product(*(self.axes[k] for k in keys)))
        cells = [dict(zip(keys, combo)) for combo in combos]

        # Apply excludes
        cells = [
            c
            for c in cells
            if not any(
                all(c.get(k) == v for k, v in exc.items()) for exc in self.exclude
            )
        ]

        # Apply includes (additive)
        for inc in self.include:
            cells.append(inc)

        return cells


@dataclass
class JobSpec:
    """Normalized representation of a single workflow job."""

    job_id: str
    runs_on: str
    needs: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    matrix: MatrixConfig = field(default_factory=MatrixConfig)
    condition: str | None = None  # job-level `if:` expression
    concurrency_group: str | None = None
    timeout_minutes: int | None = None
    environment: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    permissions: dict[str, str] = field(default_factory=dict)

    @property
    def uses_cache_action(self) -> bool:
        return any(
            "actions/cache" in str(step.get("uses", "")) for step in self.steps
        )

    @property
    def cache_key_template(self) -> str | None:
        for step in self.steps:
            if "actions/cache" in str(step.get("uses", "")):
                return step.get("with", {}).get("key")
        return None

    @property
    def cache_paths(self) -> list[str]:
        """Return all cache paths declared by actions/cache steps."""
        paths = []
        for step in self.steps:
            if "actions/cache" in str(step.get("uses", "")):
                path_val = step.get("with", {}).get("path", "")
                if isinstance(path_val, str):
                    paths.extend(
                        p.strip() for p in path_val.split("\n") if p.strip()
                    )
        return paths

    @property
    def uses_reusable_workflow(self) -> bool:
        """True if any step calls a reusable workflow (uses: owner/repo/.github/...)."""
        return any(
            ".github/workflows/" in str(step.get("uses", ""))
            for step in self.steps
        )

    @property
    def action_uses(self) -> list[str]:
        """List of all action references (e.g. 'actions/checkout@v4')."""
        return [
            step["uses"]
            for step in self.steps
            if "uses" in step
        ]


@dataclass
class TriggerFilter:
    """Parsed ``on.push`` / ``on.pull_request`` trigger configuration."""

    branches: list[str] = field(default_factory=list)
    branches_ignore: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    paths_ignore: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    tags_ignore: list[str] = field(default_factory=list)


@dataclass
class WorkflowSpec:
    """Complete normalized representation of a workflow YAML file."""

    name: str
    jobs: dict[str, JobSpec]
    raw: dict[str, Any]
    triggers: dict[str, TriggerFilter] = field(default_factory=dict)
    workflow_env: dict[str, str] = field(default_factory=dict)
    concurrency: dict[str, Any] | None = None
    workflow_permissions: dict[str, str] = field(default_factory=dict)

    def job_dag_edges(self) -> list[tuple[str, str]]:
        """Return (upstream_job, downstream_job) edges derived from ``needs:``."""
        edges = []
        for job_id, job in self.jobs.items():
            for dep in job.needs:
                edges.append((dep, job_id))
        return edges

    @property
    def all_paths_filters(self) -> list[str]:
        """Aggregate all ``paths:`` filters across triggers."""
        result = []
        for trigger in self.triggers.values():
            result.extend(trigger.paths)
        return result

    @property
    def all_paths_ignore_filters(self) -> list[str]:
        """Aggregate all ``paths-ignore:`` filters across triggers."""
        result = []
        for trigger in self.triggers.values():
            result.extend(trigger.paths_ignore)
        return result

    @property
    def total_matrix_cells(self) -> int:
        """Total number of matrix cells across all jobs."""
        return sum(job.matrix.cell_count for job in self.jobs.values())


def _parse_trigger(trigger_body: Any) -> TriggerFilter:
    """Parse a single trigger event (push, pull_request, etc.)."""
    if trigger_body is None or isinstance(trigger_body, str):
        return TriggerFilter()
    if not isinstance(trigger_body, dict):
        return TriggerFilter()

    return TriggerFilter(
        branches=_to_list(trigger_body.get("branches", [])),
        branches_ignore=_to_list(trigger_body.get("branches-ignore", [])),
        paths=_to_list(trigger_body.get("paths", [])),
        paths_ignore=_to_list(trigger_body.get("paths-ignore", [])),
        tags=_to_list(trigger_body.get("tags", [])),
        tags_ignore=_to_list(trigger_body.get("tags-ignore", [])),
    )


def _parse_matrix(strategy: dict[str, Any] | None) -> MatrixConfig:
    """Parse a job's ``strategy`` block into a MatrixConfig."""
    if not strategy:
        return MatrixConfig()

    matrix_raw = strategy.get("matrix", {})
    if not isinstance(matrix_raw, dict):
        return MatrixConfig()

    # Separate control keys from axes
    include = matrix_raw.pop("include", [])
    exclude = matrix_raw.pop("exclude", [])

    # Everything remaining is an axis
    axes = {k: _to_list(v) for k, v in matrix_raw.items()}

    return MatrixConfig(
        axes=axes,
        include=include if isinstance(include, list) else [],
        exclude=exclude if isinstance(exclude, list) else [],
        fail_fast=strategy.get("fail-fast", True),
        max_parallel=strategy.get("max-parallel"),
    )


def _to_list(val: Any) -> list:
    """Normalize a scalar-or-list YAML value to a list."""
    if val is None:
        return []
    if isinstance(val, list):
        return list(val)
    return [val]


def _parse_concurrency(body: Any) -> dict[str, Any] | None:
    """Parse workflow-level or job-level ``concurrency:``."""
    if body is None:
        return None
    if isinstance(body, str):
        return {"group": body, "cancel-in-progress": False}
    if isinstance(body, dict):
        return {
            "group": body.get("group", ""),
            "cancel-in-progress": body.get("cancel-in-progress", False),
        }
    return None


def parse_workflow(path: str | Path) -> WorkflowSpec:
    """Parse a workflow YAML file into a normalized ``WorkflowSpec``."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    name = raw.get("name", path.stem)

    # --- Triggers ---
    on_block = raw.get("on", raw.get(True, {}))  # YAML parses `on:` as True
    triggers: dict[str, TriggerFilter] = {}
    if isinstance(on_block, dict):
        for event, body in on_block.items():
            triggers[str(event)] = _parse_trigger(body)
    elif isinstance(on_block, list):
        for event in on_block:
            triggers[str(event)] = TriggerFilter()
    elif isinstance(on_block, str):
        triggers[on_block] = TriggerFilter()

    # --- Workflow-level env ---
    workflow_env = raw.get("env", {})
    if not isinstance(workflow_env, dict):
        workflow_env = {}

    # --- Workflow-level concurrency ---
    concurrency = _parse_concurrency(raw.get("concurrency"))

    # --- Workflow-level permissions ---
    perms = raw.get("permissions", {})
    if isinstance(perms, str):
        perms = {perms: "read"}
    elif not isinstance(perms, dict):
        perms = {}

    # --- Jobs ---
    jobs_raw = raw.get("jobs", {})
    jobs: dict[str, JobSpec] = {}

    for job_id, job_body in jobs_raw.items():
        needs = job_body.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]

        # Job-level concurrency
        job_concurrency = _parse_concurrency(job_body.get("concurrency"))
        concurrency_group = (
            job_concurrency["group"] if job_concurrency else None
        )

        # Job-level env
        job_env = job_body.get("env", {})
        if not isinstance(job_env, dict):
            job_env = {}

        # Job-level permissions
        job_perms = job_body.get("permissions", {})
        if isinstance(job_perms, str):
            job_perms = {job_perms: "read"}
        elif not isinstance(job_perms, dict):
            job_perms = {}

        jobs[job_id] = JobSpec(
            job_id=job_id,
            runs_on=job_body.get("runs-on", "ubuntu-latest"),
            needs=list(needs),
            steps=job_body.get("steps", []),
            matrix=_parse_matrix(job_body.get("strategy")),
            condition=job_body.get("if"),
            concurrency_group=concurrency_group,
            timeout_minutes=job_body.get("timeout-minutes"),
            environment=job_body.get("environment")
            if isinstance(job_body.get("environment"), str)
            else (job_body.get("environment", {}) or {}).get("name"),
            env=job_env,
            permissions=job_perms,
        )

    return WorkflowSpec(
        name=name,
        jobs=jobs,
        raw=raw,
        triggers=triggers,
        workflow_env=workflow_env,
        concurrency=concurrency,
        workflow_permissions=perms,
    )
