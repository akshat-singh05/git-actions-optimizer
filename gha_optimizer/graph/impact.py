from typing import List, Dict
import networkx as nx
from gha_optimizer.graph.queries import GraphQueries

class ImpactAnalysisEngine:
    """Determines high-level execution risks based on graph queries."""
    
    @staticmethod
    def analyze_commit(g: nx.MultiDiGraph, changed_files: List[str]) -> Dict:
        """
        Produce a comprehensive impact report for a commit.
        This output feeds directly into Feature Engineering.
        """
        impacted_jobs = GraphQueries.get_impacted_jobs(g, changed_files)
        impacted_caches = GraphQueries.get_cache_keys_touched(g, changed_files)
        
        # Calculate a naive dependency risk score based on PageRank of changed files
        risk_score = 0.0
        for f in changed_files:
            node_id = f"File::{f}"
            if node_id in g:
                pr = g.nodes[node_id].get("pagerank", 0.0)
                risk_score += pr
                
        return {
            "impacted_files_count": len(changed_files),
            "impacted_jobs": list(impacted_jobs),
            "impacted_caches": list(impacted_caches),
            "dependency_risk_score": risk_score,
            "execution_priority": "HIGH" if risk_score > 0.05 else "NORMAL"
        }
