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
from collections import Counter, deque
from pathlib import Path
from typing import Callable, List, Optional, Set

import networkx as nx

from gha_optimizer.graph.ci_semantics import glob_matches
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

NAN = float("nan")

# Bump when the definition of any structural feature changes: ``build-dataset`` recomputes stored rows
# whose version differs, and ``load_history`` ignores them.
#   1: source files only as File nodes, step labels only, undirected distance, "all jobs" fallback
#   2: every tree path is a File node, full commands / reusable workflows / paths-filter gates / caches,
#      directed impact distance, unknown (NaN) instead of the fallback
GRAPH_FEATURE_VERSION = 2

# Impact flows File -> (Directory | BuildTarget | Job) -> Job -> (Job | Artifact) -> ...  Only these node types
# take part: steps, runners, caches, packages and parsed code entities are not "things a change reaches".
_IMPACT_TYPES = frozenset({
    NodeType.WORKFLOW_JOB.value, NodeType.BUILD_TARGET.value, NodeType.DIRECTORY.value, NodeType.ARTIFACT.value,
})


def build_dependency_graph(
    workflow,
    repo_root: Path,
    tracked_files: Optional[List[str]] = None,
    source_graph: Optional[nx.MultiDiGraph] = None,
    read_file: Optional[Callable[[str], Optional[str]]] = None,
) -> nx.MultiDiGraph:
    """
    Build the unified heterogeneous dependency graph.

    1. Parse source files with language-specific AST parsers (or use a
       pre-built ``source_graph``, e.g. assembled from cached per-blob parses) and add a
       path-only ``File`` node for every other tracked file (docs, manifests, lock files).
    2. Inline local reusable workflows / composite actions (``read_file(path) -> text|None``;
       default: read from ``repo_root``) and parse the workflow YAML into CI/CD nodes.
    3. Fuse the two domains with cross-domain edges.

    This mirrors :func:`gha_optimizer.pipeline.build_dependency_graph`
    but lives here to avoid circular imports (feature_extraction imports
    this module, and pipeline imports feature_extraction).
    """
    from gha_optimizer.graph.builder import GraphBuilder
    from gha_optimizer.graph.fusion import GraphFusion
    from gha_optimizer.graph.workflow_expand import expand_workflow

    repo_root = Path(repo_root)
    if source_graph is None:
        builder = GraphBuilder(repo_root)
        if tracked_files:
            source_paths = [
                repo_root / f for f in tracked_files if (repo_root / f).exists()
            ]
            builder.build_from_source(source_paths)
            builder.add_file_nodes([Path(f).as_posix() for f in tracked_files])
        source_graph = builder.get_graph()

    if read_file is None:
        def read_file(path: str) -> Optional[str]:
            target = repo_root / path
            try:
                return target.read_text(encoding="utf-8") if target.is_file() else None
            except (OSError, UnicodeDecodeError):
                return None

    ci_builder = GraphBuilder(repo_root)
    ci_builder.build_from_workflow(expand_workflow(workflow, read_file))
    ci_graph = ci_builder.get_graph()

    return GraphFusion.fuse(source_graph, ci_graph)


def _impact_distances(g: nx.DiGraph | nx.MultiDiGraph, sources: List[str]) -> dict:
    """Hop count from the nearest source over impact-carrying nodes (see ``_IMPACT_TYPES``)."""
    dist = {s: 0 for s in sources}
    queue = deque(sources)
    while queue:
        u = queue.popleft()
        for v in g.successors(u):
            if v not in dist and g.nodes[v].get("node_type") in _IMPACT_TYPES:
                dist[v] = dist[u] + 1
                queue.append(v)
    return dist


def linked_job_nodes(g: nx.DiGraph | nx.MultiDiGraph) -> Set[str]:
    """Job nodes some ``File`` node reaches at all (directly, via a scope node, or through ``needs``).
    A job outside this set has no file-level evidence in the graph: nothing is known about what
    triggers it, which is different from "no change touches it"."""
    files = [n for n, d in g.nodes(data=True) if d.get("node_type") == NodeType.FILE.value]
    seeds = list({v for f in files for v in g.successors(f) if g.nodes[v].get("node_type") in _IMPACT_TYPES})
    reach = _impact_distances(g, seeds)
    return {n for n in reach if g.nodes[n].get("node_type") == NodeType.WORKFLOW_JOB.value}


def job_cache_touched(g: nx.DiGraph | nx.MultiDiGraph, changed_files: List[str], job_id: str) -> float:
    """1.0 if a cache restored by this job is keyed on a file the diff changes; 0.0 if the job restores
    no cache or every cache it restores is keyed on known files none of which changed; NaN if some
    cache's key cannot be tied to files (no ``hashFiles``, undocumented built-in cache) and nothing
    touched the others."""
    job_node = _job_node_id(job_id)
    if job_node not in g:
        return NAN
    caches = [g.nodes[n].get("attributes") or {} for n in g.successors(job_node)
              if g.nodes[n].get("node_type") == NodeType.CACHE_KEY.value]
    if not caches:
        return 0.0
    for attrs in caches:
        if any(glob_matches(pat, f) for pat in attrs.get("hash_globs") or [] for f in changed_files):
            return 1.0
    return 0.0 if all(a.get("resolved") for a in caches) else NAN


def job_graph_features(
    g: nx.DiGraph | nx.MultiDiGraph, changed_files: List[str], job_ids: List[str]
) -> dict:
    """
    The structural features of every job for one diff - the single implementation shared by training
    (per-commit graphs), the static-graph fallback and inference.

    Per job: ``job_structurally_impacted`` (1 = a changed file reaches the job; 0 = the job is
    file-linked but no changed file reaches it; NaN = no file evidence for this job, or no changed file
    is a graph node), ``dep_graph_distance`` (hops from the nearest changed file; NaN if unreached),
    ``cache_key_touched`` (see :func:`job_cache_touched`), ``any_changed_file_in_graph`` and
    ``job_has_file_links``.
    """
    sources = [_file_node_id(f) for f in changed_files if _file_node_id(f) in g]
    dist = _impact_distances(g, sources)
    linked = linked_job_nodes(g)
    in_graph = float(bool(sources))
    out = {}
    for job in job_ids:
        node = _job_node_id(job)
        has_links = node in linked
        reached = node in dist
        out[job] = {
            "job_structurally_impacted": (float(reached) if (has_links and sources) else NAN),
            "dep_graph_distance": float(dist[node]) if reached else NAN,
            "cache_key_touched": job_cache_touched(g, changed_files, job),
            "any_changed_file_in_graph": in_graph,
            "job_has_file_links": float(has_links),
        }
    return out


def impacted_jobs_for_diff(
    g: nx.DiGraph | nx.MultiDiGraph,
    changed_files: List[str],
) -> Set[str]:
    """
    Plain job IDs (e.g. ``"lint"``) that must be assumed impacted by the diff - the *conservative* set
    a scheduler needs: jobs a changed file reaches, plus every job with no file evidence (it cannot be
    proven unaffected).  If no changed file is a graph node, all jobs.

    Training features use :func:`job_graph_features` instead, which keeps "unknown" as NaN.
    """
    all_job_nodes = {
        n for n, d in g.nodes(data=True)
        if d.get("node_type") == NodeType.WORKFLOW_JOB.value
    }
    if not all_job_nodes:
        return set()
    sources = [_file_node_id(f) for f in changed_files if _file_node_id(f) in g]
    if not sources:
        return {_strip_prefix(n, NodeType.WORKFLOW_JOB) for n in all_job_nodes}
    reached = set(_impact_distances(g, sources))
    impacted = (reached | (all_job_nodes - linked_job_nodes(g))) & all_job_nodes
    return {_strip_prefix(n, NodeType.WORKFLOW_JOB) for n in impacted}


def cache_keys_touched_by(
    g: nx.DiGraph | nx.MultiDiGraph,
    changed_files: List[str],
) -> Set[str]:
    """
    Labels of the ``CacheKey`` nodes whose key is a hash of a file the diff changes
    (``hashFiles('requirements.txt')`` or a ``setup-*`` action's built-in dependency globs).
    """
    touched: Set[str] = set()
    for node, data in g.nodes(data=True):
        if data.get("node_type") != NodeType.CACHE_KEY.value:
            continue
        globs = (data.get("attributes") or {}).get("hash_globs") or []
        if any(glob_matches(pat, f) for pat in globs for f in changed_files):
            touched.add(_strip_prefix(node, NodeType.CACHE_KEY))
    return touched


def graph_distance_to_job(
    g: nx.DiGraph | nx.MultiDiGraph,
    file_path: str,
    job_id: str,
) -> int:
    """
    Hops from a file node to a job node along impact-carrying edges (file -> scope -> job -> downstream
    job ...).  ``-1`` if no such path exists or either node is missing.
    """
    src = _file_node_id(file_path)
    dst = _job_node_id(job_id)
    if src not in g or dst not in g:
        return -1
    return _impact_distances(g, [src]).get(dst, -1)


def any_changed_file_in_graph(g: nx.DiGraph | nx.MultiDiGraph, changed_files: List[str]) -> bool:
    """True if at least one changed file is a ``File::`` node.  When False no structural feature
    can be computed for the diff."""
    return any(_file_node_id(f) in g for f in changed_files)


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
