import networkx as nx
from typing import Dict, Any

class GraphVersioning:
    """Handles logic for comparing graph snapshots over time."""

    @staticmethod
    def generate_diff(graph_old: nx.MultiDiGraph, graph_new: nx.MultiDiGraph) -> Dict[str, Any]:
        """
        Compare two graphs and return structural differences (added/removed nodes and edges).
        Useful for tracking incremental architectural changes across commits.
        """
        old_nodes = set(graph_old.nodes())
        new_nodes = set(graph_new.nodes())

        added_nodes = list(new_nodes - old_nodes)
        removed_nodes = list(old_nodes - new_nodes)

        old_edges = set(graph_old.edges(keys=True))
        new_edges = set(graph_new.edges(keys=True))

        added_edges = list(new_edges - old_edges)
        removed_edges = list(old_edges - new_edges)

        return {
            "added_nodes_count": len(added_nodes),
            "removed_nodes_count": len(removed_nodes),
            "added_edges_count": len(added_edges),
            "removed_edges_count": len(removed_edges),
            "added_nodes": added_nodes,
            "removed_nodes": removed_nodes,
        }
