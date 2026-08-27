"""
Base class for language-specific AST parsers.

All parsers must implement parse_file() and return NodeMetadata/EdgeMetadata
lists.  This ensures a common interface so future languages can be added
without changing the graph engine.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Tuple

from gha_optimizer.graph.models import (
    EdgeMetadata,
    EdgeType,
    NodeMetadata,
    NodeType,
    ProvenanceRecord,
)


class BaseLanguageParser(ABC):
    """Common interface for language-specific AST parsers."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    @abstractmethod
    def parse_file(
        self, file_path: Path
    ) -> Tuple[List[NodeMetadata], List[EdgeMetadata]]:
        """
        Parse a single source file and return graph nodes + edges.

        Returns
        -------
        (nodes, edges) — lists of NodeMetadata and EdgeMetadata
        """
        ...

    def generate_node_id(self, node_type: NodeType, name: str) -> str:
        """Generate a standardised node ID."""
        return f"{node_type.value}::{name}"

    def create_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        confidence: float = 1.0,
    ) -> EdgeMetadata:
        """Create an EdgeMetadata with standard provenance."""
        return EdgeMetadata(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=confidence,
            provenance=ProvenanceRecord(
                origin_parser=self.__class__.__name__,
                creation_source="ast",
                confidence=confidence,
            ),
        )
