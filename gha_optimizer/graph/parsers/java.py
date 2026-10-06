"""
Java Parser using Tree-sitter.

Extracts: imports, classes, interfaces, methods.
Reports capabilities honestly.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Tuple

from gha_optimizer.graph.models import (
    EdgeMetadata,
    EdgeType,
    NodeMetadata,
    NodeType,
    ProvenanceRecord,
)
from gha_optimizer.graph.parsers.base import BaseLanguageParser
from gha_optimizer.schemas import ParserCapability

logger = logging.getLogger(__name__)


class JavaParser(BaseLanguageParser):
    """Java AST parser using tree-sitter."""

    def __init__(self, repo_root: Path):
        super().__init__(repo_root)
        self.capabilities = ParserCapability(
            language="java",
            available=False,
            extracts_imports=True,
            extracts_classes=True,
            extracts_functions=True,
            extracts_calls=False,
        )
        try:
            import tree_sitter_java
            from tree_sitter import Language, Parser
            self.language = Language(tree_sitter_java.language())
            self.parser = Parser(self.language)
            self.capabilities.available = True
        except Exception as e:
            logger.warning("Tree-sitter Java unavailable: %s", e)
            self.parser = None
            self.capabilities.error_message = str(e)

    def parse_file(self, file_path: Path) -> Tuple[List[NodeMetadata], List[EdgeMetadata]]:
        if not self.parser or not file_path.exists():
            return [], []

        try:
            source_code = file_path.read_bytes()
        except Exception as exc:
            logger.warning("Cannot read %s: %s", file_path, exc)
            return [], []

        tree = self.parser.parse(source_code)
        nodes: List[NodeMetadata] = []
        edges: List[EdgeMetadata] = []
        prov = ProvenanceRecord(
            origin_parser="JavaParser",
            creation_source="ast",
            source_file=str(file_path),
            confidence=0.85,
        )

        try:
            rel_path = file_path.relative_to(self.repo_root).as_posix()  # posix: GitHub paths use '/'
        except ValueError:
            rel_path = file_path.as_posix()

        file_node_id = self.generate_node_id(NodeType.FILE, rel_path)
        nodes.append(NodeMetadata(
            unique_id=file_node_id,
            node_type=NodeType.FILE,
            language="java",
            provenance=prov,
        ))

        def _text(node) -> str:
            return source_code[node.start_byte:node.end_byte].decode("utf8", errors="replace")

        def traverse(node, parent_scope: str = ""):
            if node.type == "import_declaration":
                for child in node.children:
                    if child.type == "scoped_identifier":
                        module = _text(child)
                        pkg_id = self.generate_node_id(NodeType.PACKAGE, module)
                        nodes.append(NodeMetadata(
                            unique_id=pkg_id, node_type=NodeType.PACKAGE,
                            language="java", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, pkg_id, EdgeType.IMPORTS, 0.8
                        ))

            elif node.type in ("class_declaration", "interface_declaration"):
                for child in node.children:
                    if child.type == "identifier":
                        class_name = _text(child)
                        fqn = f"{rel_path}::{class_name}"
                        class_id = self.generate_node_id(NodeType.CLASS, fqn)
                        nodes.append(NodeMetadata(
                            unique_id=class_id, node_type=NodeType.CLASS,
                            language="java", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, class_id, EdgeType.BELONGS_TO
                        ))

                        # Traverse class body for methods
                        for sub in node.children:
                            if sub.type == "class_body":
                                traverse(sub, parent_scope=fqn)
                        return

            elif node.type == "method_declaration":
                for child in node.children:
                    if child.type == "identifier":
                        method_name = _text(child)
                        fqn = f"{parent_scope}::{method_name}" if parent_scope else f"{rel_path}::{method_name}"
                        method_id = self.generate_node_id(NodeType.METHOD, fqn)
                        nodes.append(NodeMetadata(
                            unique_id=method_id, node_type=NodeType.METHOD,
                            language="java", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, method_id, EdgeType.BELONGS_TO
                        ))
                        break

            for child in node.children:
                traverse(child, parent_scope)

        traverse(tree.root_node)
        return nodes, edges
