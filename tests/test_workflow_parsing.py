"""
Tests for workflow YAML parsing (Layer 1 — Data Ingestion).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from gha_optimizer.ingestion.workflow_parser import (
    WorkflowSpec,
    parse_workflow,
)


class TestWorkflowParsing:
    """Verify that workflow YAML is correctly parsed into a WorkflowSpec."""

    def test_parse_basic_workflow(self, sample_workflow_path: Path):
        """A valid workflow YAML produces a WorkflowSpec with correct jobs."""
        wf = parse_workflow(sample_workflow_path)

        assert isinstance(wf, WorkflowSpec)
        assert wf.name == "CI"
        assert set(wf.jobs.keys()) == {"lint", "unit_tests", "integration_tests", "build", "deploy"}

    def test_job_dependencies(self, sample_workflow: WorkflowSpec):
        """needs: declarations are correctly parsed into DAG edges."""
        edges = sample_workflow.job_dag_edges()

        # lint → unit_tests, lint → integration_tests
        assert ("lint", "unit_tests") in edges
        assert ("lint", "integration_tests") in edges
        # unit_tests → build, integration_tests → build
        assert ("unit_tests", "build") in edges
        assert ("integration_tests", "build") in edges
        # build → deploy
        assert ("build", "deploy") in edges

    def test_runner_labels(self, sample_workflow: WorkflowSpec):
        """Each job should report its runner label."""
        for job in sample_workflow.jobs.values():
            assert job.runs_on == "ubuntu-latest"

    def test_cache_detection(self, sample_workflow: WorkflowSpec):
        """Jobs with actions/cache steps should be detected."""
        lint = sample_workflow.jobs["lint"]
        assert lint.uses_cache_action is True

        build = sample_workflow.jobs["build"]
        assert build.uses_cache_action is False

    def test_cache_key_template(self, sample_workflow: WorkflowSpec):
        """Cache key template should be extracted from the YAML."""
        lint = sample_workflow.jobs["lint"]
        key = lint.cache_key_template
        assert key is not None
        assert "hashFiles" in key
        assert "requirements.txt" in key

    def test_trigger_parsing(self, sample_workflow: WorkflowSpec):
        """Triggers (on: block) should be parsed correctly."""
        assert "push" in sample_workflow.triggers
        push = sample_workflow.triggers["push"]
        assert "main" in push.branches

    def test_environment_detection(self, sample_workflow: WorkflowSpec):
        """deploy job should have environment=production."""
        deploy = sample_workflow.jobs["deploy"]
        assert deploy.environment == "production"

    def test_action_uses(self, sample_workflow: WorkflowSpec):
        """action_uses should list all action references."""
        lint = sample_workflow.jobs["lint"]
        uses = lint.action_uses
        assert "actions/checkout@v4" in uses
        assert "actions/cache@v4" in uses

    def test_empty_workflow(self, tmp_path: Path):
        """A workflow with no jobs should parse without error."""
        wf_path = tmp_path / "empty.yml"
        wf_path.write_text(yaml.dump({"name": "Empty", "on": "push", "jobs": {}}))
        wf = parse_workflow(wf_path)
        assert len(wf.jobs) == 0

    def test_matrix_config(self, tmp_path: Path):
        """strategy.matrix should be parsed into MatrixConfig."""
        wf_yaml = {
            "name": "Matrix",
            "on": "push",
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "strategy": {
                        "matrix": {
                            "python-version": ["3.10", "3.11", "3.12"],
                            "os": ["ubuntu-latest", "macos-latest"],
                        }
                    },
                    "steps": [{"run": "echo test"}],
                }
            },
        }
        wf_path = tmp_path / "matrix.yml"
        wf_path.write_text(yaml.dump(wf_yaml))
        wf = parse_workflow(wf_path)

        test_job = wf.jobs["test"]
        assert test_job.matrix.cell_count == 6  # 3 × 2
        cells = test_job.matrix.enumerate_cells()
        assert len(cells) == 6

    def test_total_matrix_cells(self, sample_workflow: WorkflowSpec):
        """total_matrix_cells should be the sum across all jobs."""
        # No matrices in sample → each job = 1 cell
        assert sample_workflow.total_matrix_cells == 5
