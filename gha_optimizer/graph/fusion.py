"""
Graph Fusion Engine — connects source code, CI/CD, and execution domains.

The purpose of fusion is to establish meaningful cross-domain relationships:
  Source Code  ↔  Workflow Jobs
  Files        ↔  Cache Keys
  Packages     ↔  Dependency install steps
  Tests        ↔  Workflow test jobs

Every generated edge carries: type, confidence, provenance, timestamp.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Set

import networkx as nx

from gha_optimizer.graph.models import (
    EdgeMetadata,
    EdgeType,
    NodeType,
    ProvenanceRecord,
)

logger = logging.getLogger(__name__)


class GraphFusion:
    """Merges static source code graphs with dynamic CI/CD execution graphs
    and creates intelligent cross-domain edges."""

    @staticmethod
    def fuse(
        static_graph: nx.MultiDiGraph,
        ci_graph: nx.MultiDiGraph,
    ) -> nx.MultiDiGraph:
        """
        Merge source code and CI/CD graphs, then create cross-domain edges.

        1. Compose the two graphs.
        2. Link files to jobs that test them.
        3. Link files to cache keys they can invalidate.
        4. Link packages to jobs that install them.
        """
        merged = nx.compose(static_graph, ci_graph)

        prov = ProvenanceRecord(
            origin_parser="GraphFusion",
            creation_source="cross_domain_inference",
            confidence=0.7,
        )

        # Pass 1: File → Job relationships (test linkage)
        n_test_edges = GraphFusion._link_files_to_test_jobs(merged, prov)

        # Pass 2: File → CacheKey relationships (invalidation)
        n_cache_edges = GraphFusion._link_files_to_caches(merged, prov)

        # Pass 3: Package → Job relationships (dependency)
        n_pkg_edges = GraphFusion._link_packages_to_jobs(merged, prov)

        # Pass 4: Directory → Job containment
        n_dir_edges = GraphFusion._link_directories_to_jobs(merged, prov)

        logger.info(
            "Graph fusion complete: %d nodes, %d edges "
            "(+%d test, +%d cache, +%d package, +%d directory edges)",
            merged.number_of_nodes(),
            merged.number_of_edges(),
            n_test_edges,
            n_cache_edges,
            n_pkg_edges,
            n_dir_edges,
        )
        return merged

    # ─── File → Test Job linking ────────────────────────────────────

    @staticmethod
    def _link_files_to_test_jobs(
        g: nx.MultiDiGraph, prov: ProvenanceRecord
    ) -> int:
        """
        If a WorkflowStep runs a test command (pytest, jest, go test, etc.)
        targeting a directory, link all File nodes under that directory to
        the parent WorkflowJob via TESTS edges.
        """
        edges_added = 0

        # Find all step nodes and check for test commands
        test_patterns = [
            (r"pytest\s+(\S+)", "pytest"),
            (r"python\s+-m\s+pytest\s+(\S+)", "pytest"),
            (r"npm\s+(?:run\s+)?test", "npm"),
            (r"jest\s+(\S*)", "jest"),
            (r"go\s+test\s+(\S+)", "go"),
            (r"mvn\s+test", "maven"),
            (r"gradle\s+test", "gradle"),
        ]

        job_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_JOB.value
        }
        step_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_STEP.value
        }
        file_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.FILE.value
        }

        for step_id, step_data in step_nodes.items():
            tags = step_data.get("tags", [])
            step_label = tags[0] if tags else step_id

            # Find the parent job for this step
            parent_job = None
            for pred in g.predecessors(step_id):
                if g.nodes.get(pred, {}).get("node_type") == NodeType.WORKFLOW_JOB.value:
                    parent_job = pred
                    break
            if not parent_job:
                continue

            # Check step tags/name for test commands
            for pattern, framework in test_patterns:
                match = re.search(pattern, step_label)
                if match:
                    test_dir = match.group(1) if match.lastindex else ""
                    # Link relevant file nodes to this job
                    for file_id in file_nodes:
                        file_name = file_id.replace(f"{NodeType.FILE.value}::", "")
                        if test_dir and file_name.startswith(test_dir):
                            _add_edge_if_missing(
                                g, file_id, parent_job,
                                EdgeType.TESTS,
                                ProvenanceRecord(
                                    origin_parser="GraphFusion",
                                    creation_source=f"test_command_{framework}",
                                    confidence=0.8,
                                ),
                            )
                            edges_added += 1
                    break

        return edges_added

    # ─── File → CacheKey linking ────────────────────────────────────

    @staticmethod
    def _link_files_to_caches(
        g: nx.MultiDiGraph, prov: ProvenanceRecord
    ) -> int:
        """
        If a CacheKey's hash pattern references a file (e.g.
        hashFiles('requirements.txt')), link that file to the cache
        via INVALIDATES_CACHE.
        """
        edges_added = 0

        cache_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.CACHE_KEY.value
        }
        file_nodes = set(
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.FILE.value
        )

        hashfiles_pattern = re.compile(r"hashFiles\(['\"]([^'\"]+)['\"]\)")

        for cache_id, cache_data in cache_nodes.items():
            cache_label = cache_id.replace(f"{NodeType.CACHE_KEY.value}::", "")
            # Extract hashFiles references
            matches = hashfiles_pattern.findall(cache_label)
            for ref_file in matches:
                for file_id in file_nodes:
                    file_name = file_id.replace(f"{NodeType.FILE.value}::", "")
                    if fnmatch.fnmatch(file_name, ref_file) or file_name == ref_file:
                        _add_edge_if_missing(
                            g, file_id, cache_id,
                            EdgeType.INVALIDATES_CACHE,
                            ProvenanceRecord(
                                origin_parser="GraphFusion",
                                creation_source="cache_key_hashfiles",
                                confidence=0.9,
                            ),
                        )
                        edges_added += 1

        return edges_added

    # ─── Package → Job linking ──────────────────────────────────────

    @staticmethod
    def _link_packages_to_jobs(
        g: nx.MultiDiGraph, prov: ProvenanceRecord
    ) -> int:
        """
        If a step runs `pip install -r requirements.txt` or
        `npm install`, link Package nodes to the parent Job.
        """
        edges_added = 0

        install_patterns = [
            r"pip\s+install",
            r"npm\s+install",
            r"npm\s+ci",
            r"yarn\s+install",
            r"go\s+mod\s+download",
            r"mvn\s+install",
        ]

        package_nodes = {
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.PACKAGE.value
        }

        if not package_nodes:
            return 0

        step_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_STEP.value
        }

        for step_id, step_data in step_nodes.items():
            tags = step_data.get("tags", [])
            step_label = tags[0] if tags else ""

            has_install = any(
                re.search(pat, step_label) for pat in install_patterns
            )
            if not has_install:
                continue

            # Find parent job
            parent_job = None
            for pred in g.predecessors(step_id):
                if g.nodes.get(pred, {}).get("node_type") == NodeType.WORKFLOW_JOB.value:
                    parent_job = pred
                    break
            if not parent_job:
                continue

            for pkg_id in package_nodes:
                _add_edge_if_missing(
                    g, pkg_id, parent_job,
                    EdgeType.DEPENDS_ON,
                    ProvenanceRecord(
                        origin_parser="GraphFusion",
                        creation_source="install_command",
                        confidence=0.6,
                    ),
                )
                edges_added += 1

        return edges_added

    # ─── Directory → Job linking ────────────────────────────────────

    @staticmethod
    def _link_directories_to_jobs(
        g: nx.MultiDiGraph, prov: ProvenanceRecord
    ) -> int:
        """
        Link Directory nodes to Job nodes when step commands reference
        that directory (e.g., ``ruff check src/``, ``pytest tests/``).
        """
        edges_added = 0

        dir_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.DIRECTORY.value
        }
        if not dir_nodes:
            return 0

        step_nodes = {
            n: d for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_STEP.value
        }

        for step_id, step_data in step_nodes.items():
            tags = step_data.get("tags", [])
            step_label = tags[0] if tags else ""

            # Find the parent job for this step
            parent_job = None
            for pred in g.predecessors(step_id):
                if g.nodes.get(pred, {}).get("node_type") == NodeType.WORKFLOW_JOB.value:
                    parent_job = pred
                    break
            if not parent_job:
                continue

            # Check if step command references any directory
            for dir_id in dir_nodes:
                dir_name = dir_id.replace(f"{NodeType.DIRECTORY.value}::", "")
                if dir_name and dir_name in step_label:
                    _add_edge_if_missing(
                        g, dir_id, parent_job,
                        EdgeType.REFERENCES,
                        ProvenanceRecord(
                            origin_parser="GraphFusion",
                            creation_source="directory_reference",
                            confidence=0.5,
                        ),
                    )
                    edges_added += 1

        return edges_added


# ─── Utility ────────────────────────────────────────────────────────

def _add_edge_if_missing(
    g: nx.MultiDiGraph,
    source: str,
    target: str,
    edge_type: EdgeType,
    prov: ProvenanceRecord,
) -> None:
    """Add an edge only if it doesn't already exist."""
    existing = g.get_edge_data(source, target)
    if existing:
        for key, data in existing.items():
            if data.get("edge_type") == edge_type.value:
                return
    g.add_edge(
        source, target,
        key=edge_type.value,
        edge_type=edge_type.value,
        source_id=source,
        target_id=target,
        weight=prov.confidence,
        confidence=prov.confidence,
        provenance=prov.model_dump(),
    )
