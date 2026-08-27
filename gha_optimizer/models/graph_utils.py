"""
Utilities for converting a ``networkx.DiGraph`` (the heterogeneous
dependency graph) into ``torch_geometric`` data structures suitable
for the GNN-based cache-miss predictor (Model D).

This module bridges the gap between Layer 2 (graph construction) and
Layer 3 (GNN prediction) so neither needs to know the other's internals.

Requires ``torch`` and ``torch_geometric``.  Imported lazily — the rest
of the system works without these dependencies.
"""
from __future__ import annotations

from typing import Any

import networkx as nx
import numpy as np

# Node type index (must match dependency_graph.NODE_TYPE_INDEX)
NODE_TYPE_INDEX = {"file": 0, "job": 1, "cachekey": 2, "package": 3, "matrix_cell": 4}
NUM_NODE_TYPES = len(NODE_TYPE_INDEX)

# Edge relation index
EDGE_RELATION_INDEX = {
    "touched_by": 0,
    "declares": 1,
    "imports": 2,
    "needs": 3,
    "has_cell": 4,
}


def _check_torch():
    """Raise ImportError with a clear message if torch is missing."""
    try:
        import torch  # noqa: F401
        import torch_geometric  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "The GNN cache-miss predictor requires PyTorch and "
            "torch-geometric.  Install them with:\n"
            "  pip install torch torch-geometric\n"
            "Or use the tabular baseline: --model tabular"
        ) from e


def networkx_to_heterodata(
    g: nx.DiGraph,
    changed_files: list[str] | None = None,
    job_labels: dict[str, int] | None = None,
):
    """
    Convert a networkx heterogeneous dependency graph into a
    ``torch_geometric.data.HeteroData`` object.

    Parameters
    ----------
    g : The dependency graph from ``build_dependency_graph()``.
    changed_files : List of file paths in the current diff.  Sets the
        ``changed`` feature on file nodes (1 if changed, 0 otherwise).
    job_labels : Optional ``{job_id: cache_miss_label}`` dict for
        supervised training.  0 = cache hit, 1 = cache miss.

    Returns
    -------
    ``torch_geometric.data.HeteroData`` with node features and edge indices
    for each (source_type, relation, target_type) triplet.
    """
    _check_torch()
    import torch
    from torch_geometric.data import HeteroData

    changed_files = set(changed_files or [])
    job_labels = job_labels or {}

    data = HeteroData()

    # ----- Partition nodes by type -----
    nodes_by_type: dict[str, list[str]] = {}
    node_to_local_idx: dict[str, int] = {}

    for node, attrs in g.nodes(data=True):
        ntype = attrs.get("type", "unknown")
        if ntype not in nodes_by_type:
            nodes_by_type[ntype] = []
        node_to_local_idx[node] = len(nodes_by_type[ntype])
        nodes_by_type[ntype].append(node)

    # ----- Build node features -----
    for ntype, nodes in nodes_by_type.items():
        features = []
        for node in nodes:
            attrs = g.nodes[node]
            feat = _build_node_features(node, attrs, ntype, changed_files)
            features.append(feat)

        data[ntype].x = torch.tensor(np.array(features), dtype=torch.float32)

        # Add labels for job nodes
        if ntype == "job" and job_labels:
            labels = []
            for node in nodes:
                job_id = node.split("::", 1)[1] if "::" in node else node
                labels.append(job_labels.get(job_id, -1))
            data[ntype].y = torch.tensor(labels, dtype=torch.long)

    # ----- Build edge indices per (src_type, relation, dst_type) -----
    edges_by_triplet: dict[tuple[str, str, str], list[tuple[int, int]]] = {}

    for src, dst, edge_attrs in g.edges(data=True):
        src_type = g.nodes[src].get("type", "unknown")
        dst_type = g.nodes[dst].get("type", "unknown")
        relation = edge_attrs.get("relation", "connected")

        triplet = (src_type, relation, dst_type)
        if triplet not in edges_by_triplet:
            edges_by_triplet[triplet] = []

        src_idx = node_to_local_idx.get(src)
        dst_idx = node_to_local_idx.get(dst)
        if src_idx is not None and dst_idx is not None:
            edges_by_triplet[triplet].append((src_idx, dst_idx))

    for triplet, edge_list in edges_by_triplet.items():
        if not edge_list:
            continue
        src_type, relation, dst_type = triplet
        edge_index = torch.tensor(edge_list, dtype=torch.long).t().contiguous()
        data[src_type, relation, dst_type].edge_index = edge_index

    return data


def _build_node_features(
    node: str,
    attrs: dict[str, Any],
    ntype: str,
    changed_files: set[str],
) -> list[float]:
    """
    Build a fixed-length feature vector for a single node.

    Feature layout (13-dimensional):
        [0-4]  type one-hot (file, job, cachekey, package, matrix_cell)
        [5]    in_degree
        [6]    out_degree
        [7]    changed (1 if this file is in the diff, 0 otherwise)
        [8]    has_cache (1 if job uses cache action)
        [9]    historical_failure_rate
        [10]   historical_mean_duration (normalized by /300)
        [11]   historical_cache_miss_rate
        [12]   matrix_size (0 if not a job, else cell count)
    """
    # One-hot type encoding
    type_idx = NODE_TYPE_INDEX.get(ntype, 0)
    one_hot = [0.0] * NUM_NODE_TYPES
    one_hot[type_idx] = 1.0

    # Graph topology
    in_deg = float(attrs.get("in_degree", 0))
    out_deg = float(attrs.get("out_degree", 0))

    # File-specific: is this file changed?
    changed = 0.0
    if ntype == "file":
        file_path = node.split("::", 1)[1] if "::" in node else node
        changed = 1.0 if file_path in changed_files else 0.0

    # Job-specific
    has_cache = float(attrs.get("has_cache", 0))
    hist_fail = float(attrs.get("hist_failure_rate", 0.0))
    hist_dur = float(attrs.get("hist_mean_duration", 0.0)) / 300.0  # normalize
    hist_cache_miss = float(attrs.get("hist_cache_miss_rate", 0.0))
    matrix_size = float(attrs.get("matrix_size", 0))

    return one_hot + [
        in_deg,
        out_deg,
        changed,
        has_cache,
        hist_fail,
        hist_dur,
        hist_cache_miss,
        matrix_size,
    ]


NODE_FEATURE_DIM = NUM_NODE_TYPES + 8  # = 13


def create_training_batch(
    graphs: list[nx.DiGraph],
    changed_files_list: list[list[str]],
    job_labels_list: list[dict[str, int]],
):
    """
    Create a batch of HeteroData objects for training.

    Each entry corresponds to one historical snapshot: a dependency graph +
    the set of changed files + the observed cache-hit/miss labels.
    """
    _check_torch()
    from torch_geometric.data import Batch

    data_list = []
    for g, changed, labels in zip(graphs, changed_files_list, job_labels_list):
        hetero = networkx_to_heterodata(g, changed, labels)
        data_list.append(hetero)

    # Note: Batch.from_data_list works for homogeneous data; for hetero,
    # we return a plain list (the training loop handles iteration).
    return data_list
