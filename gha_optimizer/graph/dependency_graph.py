"""
Bridge module — provides backward-compatible graph query functions on top
of the heterogeneous graph engine.

The feature-extraction layer and several tests import these helpers to
answer questions like "which jobs are impacted by this diff?" and "how
far is this file from that job in the dependency graph?".

Internally every function delegates to :class:`GraphBuilder`,
:class:`GraphFusion`, and :class:`GraphQueries` while translating between
plain names (``"lint"``, ``"src/app.py"``) and the canonical node-ID
format (``"WorkflowJob::lint"``, ``"File::src/app.py"``).
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import List, Optional, Set

import networkx as nx

from gha_optimizer.graph.models import NodeType

logger = logging.getLogger(__name__)


# ── Node-ID helpers ─────────────────────────────────────────────────

def _file_node_id(file_path: str) -> str:
    return f"{NodeType.FILE.value}::{file_path}"


def _job_node_id(job_id: str) -> str:
    return f"{NodeType.WORKFLOW_JOB.value}::{job_id}"


def _strip_prefix(node_id: str, node_type: NodeType) -> str:
    prefix = f"{node_type.value}::"
    if node_id.startswith(prefix):
        return node_id[len(prefix):]
    return node_id


# ── Public API ──────────────────────────────────────────────────────

def build_dependency_graph(
    workflow,
    repo_root: Path,
    tracked_files: Optional[List[str]] = None,
) -> nx.MultiDiGraph:
    """
    Build the unified heterogeneous dependency graph.

    1. Parse source files with language-specific AST parsers.
    2. Parse the workflow YAML into CI/CD nodes.
    3. Fuse the two domains with cross-domain edges.

    This mirrors :func:`gha_optimizer.pipeline.build_dependency_graph`
    but lives here to avoid circular imports (feature_extraction imports
    this module, and pipeline imports feature_extraction).
    """
    from gha_optimizer.graph.builder import GraphBuilder
    from gha_optimizer.graph.fusion import GraphFusion

    repo_root = Path(repo_root)
    builder = GraphBuilder(repo_root)

    if tracked_files:
        source_paths = [
            repo_root / f for f in tracked_files if (repo_root / f).exists()
        ]
        builder.build_from_source(source_paths)

    source_graph = builder.get_graph()

    ci_builder = GraphBuilder(repo_root)
    ci_builder.build_from_workflow(workflow)
    ci_graph = ci_builder.get_graph()

    fused = GraphFusion.fuse(source_graph, ci_graph)
    return fused


def impacted_jobs_for_diff(
    g: nx.DiGraph | nx.MultiDiGraph,
    changed_files: List[str],
) -> Set[str]:
    """
    Return the set of **plain job IDs** (e.g. ``"lint"``) that are
    structurally reachable from the changed files in the dependency graph.

    If no ``File::`` nodes exist in the graph for the changed files,
    conservatively returns **all** job IDs (every job is potentially
    impacted).
    """
    all_job_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("node_type") == NodeType.WORKFLOW_JOB.value
    }

    if not all_job_nodes:
        return set()

    impacted: Set[str] = set()
    any_file_found = False

    for file_path in changed_files:
        node_id = _file_node_id(file_path)
        if node_id not in g:
            continue
        any_file_found = True

        try:
            reachable = nx.descendants(g, node_id)
        except nx.NetworkXError:
            continue

        for node in reachable:
            if node in all_job_nodes:
                impacted.add(_strip_prefix(node, NodeType.WORKFLOW_JOB))

    # Conservative fallback: if we couldn't find any of the changed files
    # in the graph, assume all jobs are potentially impacted.
    if not any_file_found:
        return {_strip_prefix(n, NodeType.WORKFLOW_JOB) for n in all_job_nodes}

    return impacted


def cache_keys_touched_by(
    g: nx.DiGraph | nx.MultiDiGraph,
    changed_files: List[str],
) -> Set[str]:
    """
    Return cache-key labels that could be invalidated by the given
    file changes.

    Walks forward from each ``File::`` node to reachable ``CacheKey::``
    nodes.  Also performs a heuristic match: if a cache key's label
    contains a changed filename (e.g. ``hashFiles('requirements.txt')``),
    include it.
    """
    cache_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("node_type") == NodeType.CACHE_KEY.value
    }

    touched: Set[str] = set()

    # Graph-reachability pass
    for file_path in changed_files:
        node_id = _file_node_id(file_path)
        if node_id not in g:
            continue
        try:
            reachable = nx.descendants(g, node_id)
        except nx.NetworkXError:
            continue
        for node in reachable:
            if node in cache_nodes:
                touched.add(_strip_prefix(node, NodeType.CACHE_KEY))

    # Heuristic pass: cache keys whose label mentions a changed file
    for cache_node in cache_nodes:
        label = _strip_prefix(cache_node, NodeType.CACHE_KEY)
        for file_path in changed_files:
            basename = Path(file_path).name
            if basename in label or file_path in label:
                touched.add(label)

    return touched


def graph_distance_to_job(
    g: nx.DiGraph | nx.MultiDiGraph,
    file_path: str,
    job_id: str,
) -> int:
    """
    Return the shortest-path distance from a file node to a job node.

    Uses the **undirected** projection of the graph so that edges in
    either direction count (e.g. ``Job → CacheKey`` can still link a
    file to a job via the cache).

    Returns ``-1`` if no path exists or either node is missing.
    """
    src = _file_node_id(file_path)
    dst = _job_node_id(job_id)

    if src not in g or dst not in g:
        return -1

    try:
        undirected = g.to_undirected()
        return nx.shortest_path_length(undirected, src, dst)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return -1


def graph_summary(g: nx.DiGraph | nx.MultiDiGraph) -> dict:
    """
    Return a compact summary of the graph, counting nodes by their
    (lowercased) type and including the total edge count.

    Example return::

        {"file": 6, "job": 5, "package": 3, "edges": 22}
    """
    type_counts: Counter = Counter()
    for _, data in g.nodes(data=True):
        raw_type = data.get("node_type", "unknown")
        # Normalise to lowercase short name for backward compatibility
        # e.g. "WorkflowJob" → "job", "CacheKey" → "cachekey"
        short = raw_type.replace("Workflow", "").lower()
        type_counts[short] += 1

    result = dict(type_counts)
    result["edges"] = g.number_of_edges()
    return result
