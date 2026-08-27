import networkx as nx
from typing import Set, List

class GraphQueries:
    """Reusable API for interrogating the graph structure."""
    
    @staticmethod
    def get_impacted_jobs(g: nx.MultiDiGraph, changed_files: List[str]) -> Set[str]:
        """
        Given changed file paths, return workflow jobs reachable via 
        DEPENDS_ON, TOUCHED_BY, or TESTS edges.
        """
        impacted_jobs = set()
        
        # In a MultiDiGraph, node names are unique IDs (e.g., 'File::src/main.py')
        for file_path in changed_files:
            file_node_id = f"File::{file_path}"
            if file_node_id not in g:
                continue
                
            # Perform BFS/DFS to find reachable WorkflowJob nodes
            reachable = nx.descendants(g, file_node_id)
            for node in reachable:
                if g.nodes[node].get("node_type") == "WorkflowJob":
                    impacted_jobs.add(node)
                    
        return impacted_jobs

    @staticmethod
    def get_cache_keys_touched(g: nx.MultiDiGraph, changed_files: List[str]) -> Set[str]:
        """Find cache keys that could be invalidated by these file changes."""
        impacted_caches = set()
        for file_path in changed_files:
            file_node_id = f"File::{file_path}"
            if file_node_id not in g:
                continue
                
            reachable = nx.descendants(g, file_node_id)
            for node in reachable:
                if g.nodes[node].get("node_type") == "CacheKey":
                    impacted_caches.add(node)
                    
        return impacted_caches
