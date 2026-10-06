"""
Incremental Graph Update Engine.

Instead of rebuilding the entire graph on every commit, this module:
  1. Accepts a list of changed files.
  2. Identifies nodes and edges that belong to those files.
  3. Removes the stale sub-graph rooted at each changed file.
  4. Re-parses only the changed files via the appropriate parser.
  5. Re-inserts the new nodes and edges.
  6. Generates a delta (snapshot diff) describing what changed.

This makes graph updates O(changed-files) instead of O(repo-size).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Set, Tuple

import networkx as nx

from gha_optimizer.graph.builder import GraphBuilder
from gha_optimizer.graph.models import NodeType

logger = logging.getLogger(__name__)


class IncrementalGraphUpdater:
    """Applies partial graph updates based on repository diffs."""

    def __init__(self, builder: GraphBuilder):
        self.builder = builder

    # ─── Core incremental update ────────────────────────────────────

    def update(
        self,
        graph: nx.MultiDiGraph,
        changed_files: List[str],
    ) -> Tuple[nx.MultiDiGraph, Dict[str, Any]]:
        """
        Incrementally update *graph* by re-parsing only *changed_files*.

        Returns
        -------
        (updated_graph, delta)
            delta contains added/removed node and edge counts.
        """
        t0 = time.perf_counter()

        removed_nodes: Set[str] = set()
        removed_edges: Set[Tuple[str, str, str]] = set()

        # Phase 1: Identify and remove stale nodes owned by changed files
        for rel_path in changed_files:
            file_node_id = f"{NodeType.FILE.value}::{rel_path}"
            if file_node_id not in graph:
                continue

            # Find all nodes that were BELONGS_TO / owned by this file node
            owned = self._owned_descendants(graph, file_node_id)
            owned.add(file_node_id)

            # Record removed edges before node removal
            for node_id in owned:
                for u, v, k in list(graph.edges(node_id, keys=True)):
                    removed_edges.add((u, v, k))
                for u, v, k in list(graph.in_edges(node_id, keys=True)):
                    removed_edges.add((u, v, k))

            removed_nodes |= owned
            graph.remove_nodes_from(owned)

        # Phase 2: Re-parse changed files
        file_paths = [
            self.builder.repo_root / f for f in changed_files
            if (self.builder.repo_root / f).exists()
        ]
        self.builder.graph = graph          # point builder at existing graph
        self.builder.build_from_source(file_paths)
        updated_graph = self.builder.get_graph()

        # Phase 3: Compute delta
        new_nodes = set(updated_graph.nodes()) - (set(graph.nodes()) - removed_nodes)
        new_edges = set(updated_graph.edges(keys=True)) - (
            set(graph.edges(keys=True)) - removed_edges
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000

        delta = {
            "removed_nodes": len(removed_nodes),
            "removed_edges": len(removed_edges),
            "added_nodes": len(new_nodes),
            "added_edges": len(new_edges),
            "changed_files": changed_files,
            "update_time_ms": round(elapsed_ms, 2),
        }

        logger.info(
            "Incremental update complete: %d files → -%d/+%d nodes in %.1fms",
            len(changed_files),
            delta["removed_nodes"],
            delta["added_nodes"],
            elapsed_ms,
        )

        return updated_graph, delta

    # ─── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _owned_descendants(
        g: nx.MultiDiGraph, file_node_id: str
    ) -> Set[str]:
        """
        Return nodes that are *structurally owned* by the file node,
        i.e. connected via BELONGS_TO edges (Classes, Functions defined
        in this file).
        """
        owned: Set[str] = set()
        for _, target, data in g.out_edges(file_node_id, data=True):
            if data.get("edge_type") == "BELONGS_TO":
                owned.add(target)
        return owned

    # ─── Changed file detection helper ──────────────────────────────

    @staticmethod
    def detect_affected_nodes(
        g: nx.MultiDiGraph, changed_files: List[str]
    ) -> Set[str]:
        """
        Given changed file paths, return the set of ALL node IDs
        reachable from those files (direct + transitive).
        """
        affected: Set[str] = set()
        for rel_path in changed_files:
            node_id = f"{NodeType.FILE.value}::{rel_path}"
            if node_id in g:
                affected.add(node_id)
                affected |= nx.descendants(g, node_id)
        return affected
