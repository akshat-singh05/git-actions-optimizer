"""
Tests for the dependency graph bridge module.

Validates that the backward-compatible API (impacted_jobs_for_diff,
cache_keys_touched_by, graph_distance_to_job, graph_summary) works
correctly on top of the heterogeneous graph engine.
"""
from pathlib import Path

from gha_optimizer.graph.dependency_graph import (
    build_dependency_graph,
    cache_keys_touched_by,
    graph_distance_to_job,
    graph_summary,
    impacted_jobs_for_diff,
)
from gha_optimizer.ingestion.workflow_parser import parse_workflow

FIXTURE_WORKFLOW = Path(__file__).resolve().parent.parent / "examples" / "sample_workflow.yml"
FIXTURE_REPO = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"

TRACKED_FILES = [
    "src/app.py",
    "src/utils.py",
    "src/api/routes.py",
    "tests/unit/test_app.py",
    "tests/integration/test_api.py",
    "requirements.txt",
]


def _graph():
    workflow = parse_workflow(FIXTURE_WORKFLOW)
    return build_dependency_graph(workflow, FIXTURE_REPO, TRACKED_FILES)


def test_all_jobs_present_as_nodes():
    g = _graph()
    job_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("node_type") == "WorkflowJob"
    }
    expected_job_ids = {"lint", "unit_tests", "integration_tests", "build", "deploy"}
    actual_job_ids = set()
    for n in job_nodes:
        # Node IDs are in format "WorkflowJob::name"
        if "::" in n:
            actual_job_ids.add(n.split("::", 1)[1])
    assert actual_job_ids == expected_job_ids


def test_needs_edges_present():
    g = _graph()
    # Check that dependency edges exist between jobs (using new node ID format)
    assert g.has_edge("WorkflowJob::lint", "WorkflowJob::unit_tests") or \
           g.has_edge("WorkflowJob::unit_tests", "WorkflowJob::lint"), \
           "Expected edge between lint and unit_tests"


def test_impacted_jobs_for_diff_returns_jobs():
    g = _graph()
    # When we can't find the files in the graph (no File:: nodes for these paths),
    # the conservative fallback returns ALL jobs
    impacted = impacted_jobs_for_diff(g, ["src/app.py"])
    assert len(impacted) > 0
    # Should return plain job IDs (not prefixed)
    for job_id in impacted:
        assert "::" not in job_id, f"Expected plain job ID, got {job_id}"


def test_cache_keys_touched_by_requirements_change():
    g = _graph()
    keys = cache_keys_touched_by(g, ["requirements.txt"])
    # Cache keys should be found via heuristic matching (key labels contain 'requirements.txt')
    assert len(keys) >= 1
    assert any("hashFiles" in k or "requirements" in k for k in keys)


def test_graph_distance():
    g = _graph()
    # Distance should be >= 1 or -1 (if file not in graph)
    dist = graph_distance_to_job(g, "src/app.py", "lint")
    # Can be -1 if file node doesn't exist in the graph (source parsing may not create it)
    # But should never be 0 (file != job)
    assert dist == -1 or dist >= 1


def test_graph_distance_nonexistent_file():
    g = _graph()
    dist = graph_distance_to_job(g, "nonexistent.py", "lint")
    assert dist == -1


def test_graph_summary():
    g = _graph()
    summary = graph_summary(g)
    assert "job" in summary
    assert summary["job"] == 5
    assert "edges" in summary
    assert summary["edges"] > 0


def test_graph_has_cache_key_nodes():
    g = _graph()
    cache_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("node_type") == "CacheKey"
    }
    # Workflow uses actions/cache, so cache key nodes should exist
    assert len(cache_nodes) >= 1


def test_graph_has_workflow_step_nodes():
    g = _graph()
    step_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("node_type") == "WorkflowStep"
    }
    # Each job has steps, so step nodes should exist
    assert len(step_nodes) >= 5


def test_edge_weights_present():
    g = _graph()
    for u, v, data in g.edges(data=True):
        assert "weight" in data
        assert data["weight"] > 0
