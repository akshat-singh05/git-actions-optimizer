"""
Go Parser using Tree-sitter.

Extracts: imports, functions, structs, interfaces.
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


class GoParser(BaseLanguageParser):
    """Go AST parser using tree-sitter."""

    def __init__(self, repo_root: Path):
        super().__init__(repo_root)
        self.capabilities = ParserCapability(
            language="go",
            available=False,
            extracts_imports=True,
            extracts_classes=True,  # structs
            extracts_functions=True,
            extracts_calls=False,
        )
        try:
            import tree_sitter_go
            from tree_sitter import Language, Parser
            self.language = Language(tree_sitter_go.language())
            self.parser = Parser(self.language)
            self.capabilities.available = True
        except Exception as e:
            logger.warning("Tree-sitter Go unavailable: %s", e)
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
            origin_parser="GoParser",
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
            language="go",
            provenance=prov,
        ))

        def _text(node) -> str:
            return source_code[node.start_byte:node.end_byte].decode("utf8", errors="replace")

        def traverse(node):
            # Import declarations
            if node.type == "import_declaration":
                for child in node.children:
                    if child.type == "import_spec_list":
                        for spec in child.children:
                            if spec.type == "import_spec":
                                for part in spec.children:
                                    if part.type == "interpreted_string_literal":
                                        module = _text(part).strip('"')
                                        pkg_id = self.generate_node_id(NodeType.PACKAGE, module)
                                        nodes.append(NodeMetadata(
                                            unique_id=pkg_id, node_type=NodeType.PACKAGE,
                                            language="go", provenance=prov,
                                        ))
                                        edges.append(self.create_edge(
                                            file_node_id, pkg_id, EdgeType.IMPORTS, 0.8
                                        ))
                    elif child.type == "import_spec":
                        for part in child.children:
                            if part.type == "interpreted_string_literal":
                                module = _text(part).strip('"')
                                pkg_id = self.generate_node_id(NodeType.PACKAGE, module)
                                nodes.append(NodeMetadata(
                                    unique_id=pkg_id, node_type=NodeType.PACKAGE,
                                    language="go", provenance=prov,
                                ))
                                edges.append(self.create_edge(
                                    file_node_id, pkg_id, EdgeType.IMPORTS, 0.8
                                ))

            # Function declarations
            elif node.type == "function_declaration":
                for child in node.children:
                    if child.type == "identifier":
                        func_name = _text(child)
                        func_id = self.generate_node_id(
                            NodeType.FUNCTION, f"{rel_path}::{func_name}"
                        )
                        nodes.append(NodeMetadata(
                            unique_id=func_id, node_type=NodeType.FUNCTION,
                            language="go", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, func_id, EdgeType.BELONGS_TO
                        ))
                        break

            # Struct/Interface (Go's equivalent of classes)
            elif node.type == "type_declaration":
                for child in node.children:
                    if child.type == "type_spec":
                        for part in child.children:
                            if part.type == "identifier":
                                struct_name = _text(part)
                                struct_id = self.generate_node_id(
                                    NodeType.CLASS, f"{rel_path}::{struct_name}"
                                )
                                nodes.append(NodeMetadata(
                                    unique_id=struct_id, node_type=NodeType.CLASS,
                                    language="go", provenance=prov,
                                ))
                                edges.append(self.create_edge(
                                    file_node_id, struct_id, EdgeType.BELONGS_TO
                                ))
                                break

            for child in node.children:
                traverse(child)

        traverse(tree.root_node)
        return nodes, edges
