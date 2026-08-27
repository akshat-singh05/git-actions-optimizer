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
    job_nodes = {n for n, d in g.nodes(data=True) if d.get("type") == "job"}
    assert job_nodes == {
        "job::lint",
        "job::unit_tests",
        "job::integration_tests",
        "job::build",
        "job::deploy",
    }


def test_needs_edges_present():
    g = _graph()
    assert g.has_edge("job::lint", "job::unit_tests")
    assert g.has_edge("job::lint", "job::integration_tests")
    assert g.has_edge("job::unit_tests", "job::build")
    assert g.has_edge("job::integration_tests", "job::build")
    assert g.has_edge("job::build", "job::deploy")


def test_impacted_jobs_for_diff_includes_every_job_by_default():
    g = _graph()
    # heuristic: every tracked file touches every job unless a paths filter
    # narrows it, so changing any tracked file impacts the whole job set
    impacted = impacted_jobs_for_diff(g, ["src/app.py"])
    assert impacted == {"lint", "unit_tests", "integration_tests", "build", "deploy"}


def test_cache_keys_touched_by_requirements_change():
    g = _graph()
    keys = cache_keys_touched_by(g, ["requirements.txt"])
    assert len(keys) >= 1
    assert any("hashFiles" in k for k in keys)


def test_import_extraction_builds_package_nodes():
    g = _graph()
    pkg_nodes = {n for n, d in g.nodes(data=True) if d.get("type") == "package"}
    # src/app.py does `import json`
    assert "pkg::json" in pkg_nodes


def test_graph_distance():
    g = _graph()
    # File -> job should be distance 1 (direct edge)
    dist = graph_distance_to_job(g, "src/app.py", "lint")
    assert dist >= 1


def test_graph_distance_nonexistent_file():
    g = _graph()
    dist = graph_distance_to_job(g, "nonexistent.py", "lint")
    assert dist == -1


def test_graph_summary():
    g = _graph()
    summary = graph_summary(g)
    assert summary["job"] == 5
    assert summary["file"] == 6
    assert "edges" in summary


def test_node_degree_attributes():
    g = _graph()
    for node in g.nodes:
        assert "in_degree" in g.nodes[node]
        assert "out_degree" in g.nodes[node]


def test_edge_weights_present():
    g = _graph()
    for u, v, data in g.edges(data=True):
        assert "weight" in data
        assert data["weight"] > 0
