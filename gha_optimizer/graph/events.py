"""
Graph Event Engine — event-driven graph layer.

Receives system events (commits, failures, cache misses, etc.)
and triggers targeted graph updates or metric recalculations.
Events are dispatched to the Celery task queue for async processing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import networkx as nx

from gha_optimizer.graph.models import (
    GraphEvent,
    GraphEventType,
    NodeType,
)

logger = logging.getLogger(__name__)


class GraphEventEngine:
    """
    Processes discrete events and determines their impact on the graph.
    Each event identifies impacted graph regions so downstream systems
    (Feature Engineering, Scheduler) know exactly what changed.
    """

    def __init__(self, graph: nx.MultiDiGraph):
        self.graph = graph
        self._event_log: List[GraphEvent] = []

    # ─── Event creation ─────────────────────────────────────────────

    def emit_commit_created(
        self,
        repository: str,
        commit_sha: str,
        changed_files: List[str],
    ) -> GraphEvent:
        """A new commit was pushed — identify affected graph regions."""
        impacted = self._find_impacted_nodes(changed_files)
        event = GraphEvent(
            event_type=GraphEventType.COMMIT_CREATED,
            repository=repository,
            commit=commit_sha,
            payload={"changed_files": changed_files},
            impacted_node_ids=list(impacted),
        )
        self._event_log.append(event)
        logger.info(
            "Event COMMIT_CREATED: %s, %d impacted nodes",
            commit_sha[:8],
            len(impacted),
        )
        return event

    def emit_workflow_completed(
        self,
        repository: str,
        workflow_name: str,
        conclusion: str,
        job_results: Dict[str, Any],
    ) -> GraphEvent:
        """A workflow finished — update historical metrics on job nodes."""
        impacted: List[str] = []
        for job_id, result in job_results.items():
            node_id = f"{NodeType.WORKFLOW_JOB.value}::{job_id}"
            if node_id in self.graph:
                impacted.append(node_id)
                data = self.graph.nodes[node_id]
                data["execution_count"] = data.get("execution_count", 0) + 1
                if result.get("conclusion") == "failure":
                    data["historical_failures"] = (
                        data.get("historical_failures", 0) + 1
                    )
                # Update running average runtime
                old_avg = data.get("average_runtime", 0.0)
                count = data.get("execution_count", 1)
                new_duration = result.get("duration_seconds", 0.0)
                data["average_runtime"] = (
                    old_avg + (new_duration - old_avg) / count
                )

        event = GraphEvent(
            event_type=GraphEventType.WORKFLOW_COMPLETED,
            repository=repository,
            payload={
                "workflow": workflow_name,
                "conclusion": conclusion,
                "jobs": job_results,
            },
            impacted_node_ids=impacted,
        )
        self._event_log.append(event)
        logger.info(
            "Event WORKFLOW_COMPLETED: %s (%s), %d jobs updated",
            workflow_name,
            conclusion,
            len(impacted),
        )
        return event

    def emit_cache_miss(
        self,
        repository: str,
        cache_key: str,
        job_id: str,
    ) -> GraphEvent:
        """A cache miss was observed — update cache stability metrics."""
        node_id = f"{NodeType.CACHE_KEY.value}::{cache_key}"
        impacted: List[str] = []
        if node_id in self.graph:
            impacted.append(node_id)
            data = self.graph.nodes[node_id]
            total = data.get("execution_count", 0) + 1
            hits = data.get("average_cache_hit_rate", 0.0) * (total - 1)
            data["execution_count"] = total
            data["average_cache_hit_rate"] = hits / total
            data["cache_stability"] = data["average_cache_hit_rate"]

        event = GraphEvent(
            event_type=GraphEventType.CACHE_MISS,
            repository=repository,
            payload={"cache_key": cache_key, "job_id": job_id},
            impacted_node_ids=impacted,
        )
        self._event_log.append(event)
        return event

    def emit_job_failure(
        self,
        repository: str,
        job_id: str,
        error_message: str = "",
    ) -> GraphEvent:
        """A job failed — update failure scores on the node."""
        node_id = f"{NodeType.WORKFLOW_JOB.value}::{job_id}"
        impacted: List[str] = []
        if node_id in self.graph:
            impacted.append(node_id)
            data = self.graph.nodes[node_id]
            data["historical_failures"] = data.get("historical_failures", 0) + 1
            count = data.get("execution_count", 1) or 1
            data["failure_score"] = data["historical_failures"] / count

        event = GraphEvent(
            event_type=GraphEventType.JOB_FAILURE,
            repository=repository,
            payload={"job_id": job_id, "error": error_message},
            impacted_node_ids=impacted,
        )
        self._event_log.append(event)
        return event

    def emit_dependency_change(
        self,
        repository: str,
        package_name: str,
        old_version: Optional[str] = None,
        new_version: Optional[str] = None,
    ) -> GraphEvent:
        """A package dependency was updated."""
        node_id = f"{NodeType.PACKAGE.value}::{package_name}"
        impacted: List[str] = []
        if node_id in self.graph:
            impacted.append(node_id)
            data = self.graph.nodes[node_id]
            data["dependency_stability"] = max(
                0.0, data.get("dependency_stability", 1.0) - 0.1
            )
            data["version"] = new_version

        event = GraphEvent(
            event_type=GraphEventType.DEPENDENCY_CHANGE,
            repository=repository,
            payload={
                "package": package_name,
                "old_version": old_version,
                "new_version": new_version,
            },
            impacted_node_ids=impacted,
        )
        self._event_log.append(event)
        return event

    # ─── Generic emitter ────────────────────────────────────────────

    def emit(self, event: GraphEvent) -> GraphEvent:
        """Record an externally constructed event."""
        self._event_log.append(event)
        logger.info("Event %s recorded", event.event_type.value)
        return event

    # ─── Query the event log ────────────────────────────────────────

    def get_recent_events(self, limit: int = 50) -> List[GraphEvent]:
        """Return the most recent events (newest first)."""
        return list(reversed(self._event_log[-limit:]))

    def get_events_for_node(self, node_id: str) -> List[GraphEvent]:
        """Return events that impacted a specific node."""
        return [
            e for e in self._event_log if node_id in e.impacted_node_ids
        ]

    # ─── Internal helpers ───────────────────────────────────────────

    def _find_impacted_nodes(self, changed_files: List[str]) -> set:
        impacted: set = set()
        for f in changed_files:
            node_id = f"{NodeType.FILE.value}::{f}"
            if node_id in self.graph:
                impacted.add(node_id)
                impacted |= nx.descendants(self.graph, node_id)
        return impacted
