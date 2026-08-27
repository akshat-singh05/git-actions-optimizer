"""
Graph Quality Engine — evaluates structural health of the knowledge graph.

Scans for:
  - Disconnected components
  - Broken dependencies (edges pointing to non-existent nodes)
  - Missing file nodes (files in repo not represented in graph)
  - Duplicate edges
  - Parser confidence distribution
  - Overall graph completeness score

Produces a ``GraphQualityReport`` that can be serialized to JSON
and stored historically.
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set

import networkx as nx

from gha_optimizer.graph.models import GraphQualityReport, NodeType

logger = logging.getLogger(__name__)


class GraphQualityEvaluator:
    """Evaluate and report on graph health."""

    def __init__(self, graph: nx.MultiDiGraph):
        self.graph = graph

    def evaluate(
        self,
        repo_root: Optional[Path] = None,
        tracked_extensions: Optional[Set[str]] = None,
    ) -> GraphQualityReport:
        """
        Run all quality checks and return a consolidated report.

        Parameters
        ----------
        repo_root
            If provided, used to check coverage (what % of files are in
            the graph).
        tracked_extensions
            File extensions to consider when computing coverage
            (e.g. ``{".py", ".js", ".ts"}``).
        """
        g = self.graph

        total_nodes = g.number_of_nodes()
        total_edges = g.number_of_edges()

        # ── Disconnected components ─────────────────────────────────
        undirected = g.to_undirected()
        components = list(nx.connected_components(undirected))
        disconnected = max(0, len(components) - 1)

        # ── Broken dependencies ─────────────────────────────────────
        broken = 0
        for u, v, _k in g.edges(keys=True):
            if u not in g.nodes or v not in g.nodes:
                broken += 1

        # ── Duplicate edges ─────────────────────────────────────────
        edge_counts: Counter = Counter()
        for u, v, k in g.edges(keys=True):
            edge_counts[(u, v, k)] += 1
        duplicate_edges = sum(c - 1 for c in edge_counts.values() if c > 1)

        # ── Duplicate nodes (same unique_id) ────────────────────────
        # In a networkx graph nodes are unique by definition, but we
        # check for nodes that share the same logical identity via
        # their "unique_id" attribute.
        uid_counts: Counter = Counter()
        for _n, data in g.nodes(data=True):
            uid = data.get("unique_id", _n)
            uid_counts[uid] += 1
        duplicate_nodes = sum(c - 1 for c in uid_counts.values() if c > 1)

        # ── Parser confidence ───────────────────────────────────────
        confidences = []
        for _n, data in g.nodes(data=True):
            prov = data.get("provenance")
            if isinstance(prov, dict):
                confidences.append(prov.get("confidence", 1.0))
            elif hasattr(prov, "confidence"):
                confidences.append(prov.confidence)
            else:
                confidences.append(1.0)
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        # ── Coverage ────────────────────────────────────────────────
        coverage = 0.0
        missing_nodes = 0
        if repo_root and repo_root.is_dir():
            exts = tracked_extensions or {".py", ".js", ".ts", ".java", ".go"}
            all_files = {
                str(p.relative_to(repo_root))
                for p in repo_root.rglob("*")
                if p.is_file() and p.suffix in exts
            }
            graph_files = {
                data.get("unique_id", n).replace(f"{NodeType.FILE.value}::", "")
                for n, data in g.nodes(data=True)
                if data.get("node_type") == NodeType.FILE.value
            }
            covered = all_files & graph_files
            coverage = len(covered) / len(all_files) if all_files else 1.0
            missing_nodes = len(all_files - graph_files)

        # ── Completeness score ──────────────────────────────────────
        #    heuristic: penalise disconnections, broken deps, missing nodes
        penalty = (disconnected * 0.05) + (broken * 0.1) + (missing_nodes * 0.02)
        completeness = max(0.0, min(1.0, coverage - penalty))

        report = GraphQualityReport(
            total_nodes=total_nodes,
            total_edges=total_edges,
            coverage=round(coverage, 4),
            average_parser_confidence=round(avg_confidence, 4),
            broken_dependencies=broken,
            missing_nodes=missing_nodes,
            missing_edges=0,  # we count broken deps above
            duplicate_nodes=duplicate_nodes,
            duplicate_edges=duplicate_edges,
            disconnected_components=disconnected,
            graph_completeness=round(completeness, 4),
        )

        logger.info(
            "Graph quality: %d nodes, %d edges, %.1f%% coverage, "
            "%.1f%% completeness",
            total_nodes,
            total_edges,
            coverage * 100,
            completeness * 100,
        )

        return report
