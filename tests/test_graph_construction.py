"""
Tests for graph construction (GraphBuilder — Layer 2).

Verifies that workflow YAML is correctly converted into graph nodes/edges,
and that source code parsing creates appropriate AST nodes.
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx

from gha_optimizer.graph.builder import GraphBuilder
from gha_optimizer.graph.models import NodeType, EdgeType
from gha_optimizer.ingestion.workflow_parser import WorkflowSpec


class TestGraphBuilderWorkflow:
    """Test GraphBuilder.build_from_workflow()."""

    def test_creates_workflow_node(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """A Workflow node should be created."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        wf_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW.value
        ]
        assert len(wf_nodes) == 1
        assert "CI" in wf_nodes[0]

    def test_creates_job_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """All 5 jobs should become WorkflowJob nodes."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        job_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_JOB.value
        ]
        assert len(job_nodes) == 5

        job_names = {n.split("::")[-1] for n in job_nodes}
        assert job_names == {"lint", "unit_tests", "integration_tests", "build", "deploy"}

    def test_creates_dependency_edges(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """needs: declarations should create DEPENDS_ON edges between jobs."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        depends_edges = [
            (u, v) for u, v, d in g.edges(data=True)
            if d.get("edge_type") == EdgeType.DEPENDS_ON.value
        ]
        # lint → unit_tests, lint → integration_tests,
        # unit_tests → build, integration_tests → build, build → deploy
        assert len(depends_edges) == 5

    def test_creates_runner_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """Runner nodes should be created (deduplicated)."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        runner_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.RUNNER.value
        ]
        # All jobs use ubuntu-latest → 1 unique runner node
        assert len(runner_nodes) == 1
        assert "ubuntu-latest" in runner_nodes[0]

    def test_creates_cache_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """actions/cache steps should create CacheKey nodes."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        cache_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.CACHE_KEY.value
        ]
        # lint and unit_tests both use cache with same key
        assert len(cache_nodes) >= 1

    def test_creates_step_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """Each step should become a WorkflowStep node."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        step_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_STEP.value
        ]
        # Total steps across all 5 jobs
        total_steps = sum(len(j.steps) for j in sample_workflow.jobs.values())
        assert len(step_nodes) == total_steps

    def test_creates_trigger_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """Trigger nodes should be created for push and pull_request."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        trigger_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_TRIGGER.value
        ]
        assert len(trigger_nodes) == 2  # push + pull_request

    def test_creates_environment_node(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """deploy job with environment: production should create an Environment node."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        env_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.ENVIRONMENT.value
        ]
        assert len(env_nodes) == 1
        assert "production" in env_nodes[0]

    def test_creates_action_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """uses: references should create ExternalDependency nodes."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        action_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.EXTERNAL_DEPENDENCY.value
        ]
        action_names = {n.split("::")[-1] for n in action_nodes}
        assert "actions/checkout@v4" in action_names
        assert "actions/cache@v4" in action_names

    def test_provenance_on_nodes(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """All nodes should carry provenance metadata."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        for node, data in g.nodes(data=True):
            assert "provenance" in data, f"Node {node} missing provenance"

    def test_graph_is_connected(self, sample_workflow: WorkflowSpec, tmp_path: Path):
        """The workflow graph should be weakly connected."""
        builder = GraphBuilder(tmp_path)
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        assert nx.is_weakly_connected(g), "Workflow graph should be weakly connected"


class TestGraphBuilderSource:
    """Test GraphBuilder.build_from_source()."""

    def test_parse_python_files(self, sample_repo_root: Path):
        """Python source files should produce File nodes."""
        builder = GraphBuilder(sample_repo_root)
        py_files = list(sample_repo_root.rglob("*.py"))
        parsed = builder.build_from_source(py_files)

        g = builder.get_graph()
        file_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.FILE.value
        ]
        # Parsed count may be 0 if tree-sitter-python not installed
        # but the builder should not crash
        assert parsed >= 0
        if parsed > 0:
            assert len(file_nodes) > 0

    def test_parser_capabilities_reporting(self, tmp_path: Path):
        """get_parser_capabilities() should report which parsers are available."""
        builder = GraphBuilder(tmp_path)
        caps = builder.get_parser_capabilities()

        # Should always have entries (even if unavailable)
        assert isinstance(caps, dict)
        # Python parser should be listed
        if ".py" in caps:
            assert "available" in caps[".py"]
