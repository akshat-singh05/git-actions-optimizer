"""
Graph Benchmarking Engine — measures performance of graph operations.

Provides context managers and utility functions to measure:
  - Graph build time
  - Incremental update time
  - Traversal / query time
  - Memory usage
  - Parser throughput
  - Snapshot storage size
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Callable, Dict

import networkx as nx

from gha_optimizer.graph.models import GraphBenchmarkReport

logger = logging.getLogger(__name__)


class GraphBenchmarker:
    """Collects performance measurements for graph operations."""

    def __init__(self):
        self._timings: Dict[str, float] = {}

    def time_operation(self, label: str, func: Callable, *args, **kwargs) -> Any:
        """Run func and record its wall-clock time under label."""
        t0 = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._timings[label] = round(elapsed_ms, 2)
        logger.info("Benchmark [%s]: %.2f ms", label, elapsed_ms)
        return result

    def measure_memory(self, g: nx.MultiDiGraph) -> int:
        """Approximate memory footprint of the graph via JSON serialization."""
        try:
            blob = json.dumps(nx.node_link_data(g))
            size = sys.getsizeof(blob)
            self._timings["memory_usage_bytes"] = size
            return size
        except Exception:
            return 0

    def measure_snapshot_size(self, g: nx.MultiDiGraph) -> int:
        """Size in bytes of the serialized graph snapshot."""
        try:
            blob = json.dumps(nx.node_link_data(g), default=str)
            size = len(blob.encode("utf-8"))
            self._timings["snapshot_storage_bytes"] = size
            return size
        except Exception:
            return 0

    def generate_report(self, g: nx.MultiDiGraph) -> GraphBenchmarkReport:
        """Produce a consolidated benchmark report."""
        return GraphBenchmarkReport(
            graph_build_time_ms=self._timings.get("build", 0.0),
            incremental_update_time_ms=self._timings.get("incremental_update", 0.0),
            traversal_time_ms=self._timings.get("traversal", 0.0),
            query_performance_ms=self._timings.get("query", 0.0),
            memory_usage_bytes=self._timings.get("memory_usage_bytes", 0),
            parser_throughput_files_per_sec=self._timings.get("parser_throughput", 0.0),
            snapshot_storage_bytes=self._timings.get("snapshot_storage_bytes", 0),
            node_count=g.number_of_nodes(),
            edge_count=g.number_of_edges(),
        )
