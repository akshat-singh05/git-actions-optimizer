"""
Graph Explainability Engine — produces human-readable reasoning traces.

Given a changed file and a target job, this module traces the complete
dependency path through the knowledge graph, annotating every hop with
the node type, edge type, reason, and confidence score.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import networkx as nx

from gha_optimizer.graph.models import (
    ExplainabilityHop,
    ExplainabilityTrace,
    NodeType,
)

logger = logging.getLogger(__name__)

_EDGE_REASONS: Dict[str, str] = {
    "IMPORTS": "imports code from",
    "CALLS": "directly calls",
    "DEPENDS_ON": "depends on",
    "USES": "uses",
    "INHERITS": "inherits from",
    "IMPLEMENTS": "implements interface of",
    "GENERATES": "generates output for",
    "PRODUCES": "produces artifact for",
    "CONSUMES": "consumes input from",
    "EXECUTES": "executes",
    "RESTORES_CACHE": "restores cache from",
    "INVALIDATES_CACHE": "may invalidate cache of",
    "RUNS_ON": "runs on runner",
    "TRIGGERS": "triggers execution of",
    "BELONGS_TO": "is defined inside",
    "REFERENCES": "references",
    "TESTS": "tests",
    "DEPLOYS": "deploys to",
    "LINKS_TO": "is linked to",
}


class GraphExplainer:
    """Produces structured reasoning chains between any two nodes."""

    def __init__(self, graph: nx.MultiDiGraph):
        self.graph = graph

    def explain_impact(self, source_file: str, target_job: str) -> ExplainabilityTrace:
        """Explain why changing source_file impacts target_job."""
        src_id = f"{NodeType.FILE.value}::{source_file}"
        tgt_id = f"{NodeType.WORKFLOW_JOB.value}::{target_job}"

        if src_id not in self.graph or tgt_id not in self.graph:
            return self._empty_trace(source_file, target_job, "Node not in graph")

        try:
            path = nx.shortest_path(self.graph, src_id, tgt_id)
        except nx.NetworkXNoPath:
            return self._empty_trace(source_file, target_job, "No path exists")

        hops: List[ExplainabilityHop] = []
        running_confidence = 1.0

        for i, node_id in enumerate(path):
            node_data = self.graph.nodes.get(node_id, {})
            node_type = node_data.get("node_type", "unknown")
            edge_type_str: Optional[str] = None
            reason = f"Changed file: {source_file}" if i == 0 else ""
            confidence = 1.0

            if i > 0:
                prev = path[i - 1]
                edge_data = self._get_edge_data(prev, node_id)
                edge_type_str = edge_data.get("edge_type", "LINKS_TO")
                confidence = edge_data.get("confidence", 1.0)
                short_prev = self._short_name(prev)
                short_curr = self._short_name(node_id)
                reason = f"{short_prev} {_EDGE_REASONS.get(edge_type_str, 'connects to')} {short_curr}"

            running_confidence *= confidence
            hops.append(ExplainabilityHop(
                node_id=node_id, node_type=node_type,
                edge_type=edge_type_str, reason=reason,
                confidence=round(confidence, 4),
            ))

        risk = "HIGH" if running_confidence > 0.7 else ("MEDIUM" if running_confidence > 0.3 else "LOW")

        return ExplainabilityTrace(
            source_file=source_file, target_job=target_job,
            hops=hops, total_confidence=round(running_confidence, 4),
            risk_assessment=risk,
        )

    def explain_all_impacts(self, changed_files: List[str]) -> List[ExplainabilityTrace]:
        """For every changed file, find all reachable WorkflowJob nodes and explain."""
        traces: List[ExplainabilityTrace] = []
        for f in changed_files:
            src_id = f"{NodeType.FILE.value}::{f}"
            if src_id not in self.graph:
                continue
            for node_id in nx.descendants(self.graph, src_id):
                if self.graph.nodes.get(node_id, {}).get("node_type") == NodeType.WORKFLOW_JOB.value:
                    job_name = node_id.replace(f"{NodeType.WORKFLOW_JOB.value}::", "")
                    traces.append(self.explain_impact(f, job_name))
        return traces

    def _get_edge_data(self, u: str, v: str) -> Dict:
        edges = self.graph.get_edge_data(u, v)
        if not edges:
            return {}
        return edges[next(iter(edges))]

    @staticmethod
    def _short_name(node_id: str) -> str:
        return node_id.split("::", 1)[1] if "::" in node_id else node_id

    @staticmethod
    def _empty_trace(source_file: str, target_job: str, reason: str) -> ExplainabilityTrace:
        return ExplainabilityTrace(
            source_file=source_file, target_job=target_job,
            hops=[ExplainabilityHop(node_id="N/A", node_type="N/A", reason=reason, confidence=0.0)],
            total_confidence=0.0, risk_assessment="UNKNOWN",
        )
