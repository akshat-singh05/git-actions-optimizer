"""
Graph Analytics Engine — computes structural and semantic metrics.

Extends the basic PageRank/Betweenness engine with:
  - Closeness / Eigenvector centrality
  - Louvain community detection
  - Dependency entropy
  - Graph density & evolution metrics
  - Repository hotspot identification
  - Workflow critical-path analysis
  - Topological ordering and cycle detection
  - Strongly connected component analysis

All metrics are injected back into node attributes so downstream
consumers (Feature Engineering, Scheduler, Explainer) can read them
directly from the graph without additional computation.
"""
from __future__ import annotations

import math
import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

logger = logging.getLogger(__name__)


class GraphAnalytics:
    """Computes centralities and structural metrics for the dependency graph."""

    # ─── Core centrality suite ──────────────────────────────────────

    @staticmethod
    def compute_all_metrics(g: nx.MultiDiGraph) -> nx.MultiDiGraph:
        """Compute structural metrics and inject them into node attributes."""
        if len(g) == 0:
            return g

        logger.info("Computing degree centrality…")
        in_degrees = dict(g.in_degree())
        out_degrees = dict(g.out_degree())

        logger.info("Computing PageRank…")
        try:
            pagerank = nx.pagerank(g, weight="weight")
        except Exception as e:
            logger.warning("PageRank failed: %s", e)
            pagerank = {}

        logger.info("Computing Betweenness…")
        try:
            betweenness = nx.betweenness_centrality(
                g, k=min(100, len(g)), weight="weight"
            )
        except Exception:
            betweenness = {}

        logger.info("Computing Closeness…")
        try:
            closeness = nx.closeness_centrality(g, wf_improved=True)
        except Exception:
            closeness = {}

        logger.info("Computing Eigenvector centrality…")
        try:
            eigenvector = nx.eigenvector_centrality_numpy(g, weight="weight")
        except Exception:
            eigenvector = {}

        # Inject back into graph
        for node in g.nodes():
            g.nodes[node]["degree"] = in_degrees.get(node, 0) + out_degrees.get(node, 0)
            g.nodes[node]["in_degree"] = in_degrees.get(node, 0)
            g.nodes[node]["out_degree"] = out_degrees.get(node, 0)
            g.nodes[node]["pagerank"] = pagerank.get(node, 0.0)
            g.nodes[node]["betweenness"] = betweenness.get(node, 0.0)
            g.nodes[node]["closeness"] = closeness.get(node, 0.0)
            g.nodes[node]["eigenvector_centrality"] = eigenvector.get(node, 0.0)

        return g

    # ─── Community detection (Louvain) ──────────────────────────────

    @staticmethod
    def compute_communities(g: nx.MultiDiGraph) -> nx.MultiDiGraph:
        """
        Detect communities using Louvain on the undirected projection.
        Assigns a ``community_id`` to each node.
        """
        if len(g) == 0:
            return g

        try:
            undirected = g.to_undirected()
            # networkx >= 3.1 has louvain_communities
            communities = nx.community.louvain_communities(
                undirected, weight="weight", seed=42
            )
            for community_idx, community_nodes in enumerate(communities):
                for node in community_nodes:
                    g.nodes[node]["community_id"] = community_idx

            logger.info("Detected %d communities", len(communities))
        except Exception as exc:
            logger.warning("Community detection failed: %s", exc)

        return g

    # ─── Dependency entropy ─────────────────────────────────────────

    @staticmethod
    def compute_dependency_entropy(g: nx.MultiDiGraph) -> float:
        """
        Shannon entropy over the out-degree distribution.
        High entropy → many nodes with varied connectivity (complex graph).
        Low entropy → uniform or star topology.
        """
        if len(g) == 0:
            return 0.0

        degrees = [d for _, d in g.out_degree()]
        total = sum(degrees) or 1
        probs = [d / total for d in degrees if d > 0]
        entropy = -sum(p * math.log2(p) for p in probs if p > 0)
        return round(entropy, 4)

    # ─── Graph density ──────────────────────────────────────────────

    @staticmethod
    def compute_graph_density(g: nx.MultiDiGraph) -> float:
        """Standard graph density (edges / max-possible-edges)."""
        return round(nx.density(g), 6)

    # ─── Repository hotspots ────────────────────────────────────────

    @staticmethod
    def identify_hotspots(
        g: nx.MultiDiGraph,
        top_k: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Hotspots are nodes with both high centrality *and* high failure
        or risk scores.  These are the most dangerous change-targets in
        the repository.
        """
        scored: List[Tuple[str, float]] = []
        for node, data in g.nodes(data=True):
            pr = data.get("pagerank", 0.0)
            bw = data.get("betweenness", 0.0)
            fail = data.get("failure_score", 0.0)
            risk = data.get("risk_score", 0.0)
            composite = (pr * 0.3) + (bw * 0.3) + (fail * 0.2) + (risk * 0.2)
            scored.append((node, composite))

        scored.sort(key=lambda x: x[1], reverse=True)
        hotspots = []
        for node_id, score in scored[:top_k]:
            g.nodes[node_id]["is_hotspot"] = True
            hotspots.append({
                "node_id": node_id,
                "node_type": g.nodes[node_id].get("node_type", "unknown"),
                "hotspot_score": round(score, 6),
                "pagerank": g.nodes[node_id].get("pagerank", 0.0),
                "betweenness": g.nodes[node_id].get("betweenness", 0.0),
                "failure_score": g.nodes[node_id].get("failure_score", 0.0),
            })
        return hotspots

    # ─── Critical paths ─────────────────────────────────────────────

    @staticmethod
    def get_critical_paths(
        g: nx.MultiDiGraph, source: str, target: str
    ) -> List[List[str]]:
        """Find all shortest paths between two nodes."""
        try:
            return list(
                nx.all_shortest_paths(g, source, target, weight="weight")
            )
        except nx.NetworkXNoPath:
            return []

    @staticmethod
    def find_workflow_critical_path(g: nx.MultiDiGraph) -> List[str]:
        """
        Find the longest weighted path among WorkflowJob nodes.
        This is the bottleneck path that determines total CI runtime.
        """
        job_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") in ("WorkflowJob", "WorkflowStep")
        ]
        if len(job_nodes) < 2:
            return job_nodes

        subgraph = g.subgraph(job_nodes).copy()
        try:
            return nx.dag_longest_path(subgraph, weight="weight")
        except (nx.NetworkXUnfeasible, nx.NetworkXError):
            return job_nodes

    # ─── Topological ordering ───────────────────────────────────────

    @staticmethod
    def topological_order(g: nx.MultiDiGraph) -> List[str]:
        """Return nodes in topological order; empty list if cycles exist."""
        try:
            return list(nx.topological_sort(g))
        except nx.NetworkXUnfeasible:
            return []

    # ─── Cycle detection ────────────────────────────────────────────

    @staticmethod
    def detect_cycles(g: nx.MultiDiGraph) -> List[List[str]]:
        """Return all simple cycles in the graph."""
        try:
            return list(nx.simple_cycles(g))
        except Exception:
            return []

    # ─── Strongly connected components ──────────────────────────────

    @staticmethod
    def strongly_connected_components(g: nx.MultiDiGraph) -> List[Set[str]]:
        """Return strongly connected components (groups of mutually reachable nodes)."""
        return [c for c in nx.strongly_connected_components(g) if len(c) > 1]

    # ─── Execution criticality ──────────────────────────────────────

    @staticmethod
    def compute_execution_criticality(g: nx.MultiDiGraph) -> nx.MultiDiGraph:
        """
        Execution criticality = PageRank × (1 + failure_score).
        Nodes with high centrality AND high failure history get the
        highest criticality — these are the nodes where failures cascade.
        """
        for node, data in g.nodes(data=True):
            pr = data.get("pagerank", 0.0)
            fail = data.get("failure_score", 0.0)
            data["execution_criticality"] = round(pr * (1.0 + fail), 6)
        return g

    # ─── Weighted centrality ────────────────────────────────────────

    @staticmethod
    def compute_weighted_centrality(g: nx.MultiDiGraph) -> nx.MultiDiGraph:
        """
        Composite centrality = weighted average of PageRank, Betweenness,
        Closeness, and Eigenvector centrality.
        """
        for node, data in g.nodes(data=True):
            pr = data.get("pagerank", 0.0)
            bw = data.get("betweenness", 0.0)
            cl = data.get("closeness", 0.0)
            ev = data.get("eigenvector_centrality", 0.0)
            data["weighted_centrality"] = round(
                (pr * 0.35) + (bw * 0.25) + (cl * 0.20) + (ev * 0.20), 6
            )
        return g

    # ─── Structural similarity ──────────────────────────────────────

    @staticmethod
    def structural_similarity(g1: nx.MultiDiGraph, g2: nx.MultiDiGraph) -> float:
        """
        Jaccard similarity of node sets between two graph snapshots.
        Useful for measuring how much a graph has evolved.
        """
        s1 = set(g1.nodes())
        s2 = set(g2.nodes())
        if not s1 and not s2:
            return 1.0
        intersection = s1 & s2
        union = s1 | s2
        return round(len(intersection) / len(union), 4) if union else 0.0

    # ─── Graph evolution metrics ────────────────────────────────────

    @staticmethod
    def evolution_metrics(
        g_old: nx.MultiDiGraph, g_new: nx.MultiDiGraph
    ) -> Dict[str, Any]:
        """Compute evolution metrics between two graph snapshots."""
        old_nodes = set(g_old.nodes())
        new_nodes = set(g_new.nodes())
        old_edges = set(g_old.edges(keys=True))
        new_edges = set(g_new.edges(keys=True))

        return {
            "nodes_added": len(new_nodes - old_nodes),
            "nodes_removed": len(old_nodes - new_nodes),
            "edges_added": len(new_edges - old_edges),
            "edges_removed": len(old_edges - new_edges),
            "structural_similarity": GraphAnalytics.structural_similarity(
                g_old, g_new
            ),
            "old_density": GraphAnalytics.compute_graph_density(g_old),
            "new_density": GraphAnalytics.compute_graph_density(g_new),
            "old_entropy": GraphAnalytics.compute_dependency_entropy(g_old),
            "new_entropy": GraphAnalytics.compute_dependency_entropy(g_new),
        }

    # ─── Summary ────────────────────────────────────────────────────

    @staticmethod
    def graph_summary(g: nx.MultiDiGraph) -> Dict[str, Any]:
        """Quick summary of graph composition."""
        type_counts: Dict[str, int] = Counter()
        for _, data in g.nodes(data=True):
            t = data.get("node_type", "unknown")
            type_counts[t] += 1

        return {
            "total_nodes": g.number_of_nodes(),
            "total_edges": g.number_of_edges(),
            "density": GraphAnalytics.compute_graph_density(g),
            "entropy": GraphAnalytics.compute_dependency_entropy(g),
            "node_type_counts": dict(type_counts),
            "num_communities": len(
                set(
                    d.get("community_id", -1)
                    for _, d in g.nodes(data=True)
                    if d.get("community_id") is not None
                )
            ),
        }
