"""
Python AST Parser using Tree-sitter.

Extracts: Files, Classes, Functions, Methods, Imports, and
relationship edges (IMPORTS, BELONGS_TO, CALLS, INHERITS).
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


class PythonParser(BaseLanguageParser):
    """
    Python AST Parser using Tree-sitter.
    Extracts Classes, Functions, Methods, Variables, and IMPORTS/CALLS edges.
    """

    def __init__(self, repo_root: Path):
        super().__init__(repo_root)
        self.capabilities = ParserCapability(
            language="python",
            available=False,
            extracts_imports=True,
            extracts_classes=True,
            extracts_functions=True,
            extracts_calls=False,
        )
        try:
            import tree_sitter_python
            from tree_sitter import Language, Parser
            self.language = Language(tree_sitter_python.language())
            self.parser = Parser(self.language)
            self.capabilities.available = True
        except Exception as e:
            logger.warning("Tree-sitter Python unavailable: %s", e)
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
            origin_parser="PythonParser",
            creation_source="ast",
            source_file=str(file_path),
            confidence=0.9,
        )

        # File node
        try:
            rel_path = str(file_path.relative_to(self.repo_root))
        except ValueError:
            rel_path = str(file_path)
        file_node_id = self.generate_node_id(NodeType.FILE, rel_path)
        nodes.append(NodeMetadata(
            unique_id=file_node_id,
            node_type=NodeType.FILE,
            language="python",
            provenance=prov,
        ))

        def _text(node) -> str:
            return source_code[node.start_byte:node.end_byte].decode("utf8", errors="replace")

        def traverse(node, parent_scope: str = ""):
            # ── Imports ─────────────────────────────────────────
            if node.type in ("import_statement", "import_from_statement"):
                module_name = ""
                for child in node.children:
                    if child.type in ("dotted_name", "identifier"):
                        module_name = _text(child)
                        break
                if module_name:
                    pkg_id = self.generate_node_id(NodeType.PACKAGE, module_name)
                    nodes.append(NodeMetadata(
                        unique_id=pkg_id,
                        node_type=NodeType.PACKAGE,
                        language="python",
                        provenance=ProvenanceRecord(
                            origin_parser="PythonParser",
                            creation_source="ast",
                            source_file=str(file_path),
                            line_start=node.start_point[0] + 1,
                            confidence=0.8,
                        ),
                    ))
                    edges.append(self.create_edge(
                        file_node_id, pkg_id, EdgeType.IMPORTS, 0.8
                    ))

            # ── Classes ─────────────────────────────────────────
            elif node.type == "class_definition":
                class_name = ""
                bases = []
                for child in node.children:
                    if child.type == "identifier" and not class_name:
                        class_name = _text(child)
                    elif child.type == "argument_list":
                        for base_child in child.children:
                            if base_child.type in ("identifier", "dotted_name"):
                                bases.append(_text(base_child))

                if class_name:
                    fqn = f"{rel_path}::{class_name}"
                    class_id = self.generate_node_id(NodeType.CLASS, fqn)
                    nodes.append(NodeMetadata(
                        unique_id=class_id,
                        node_type=NodeType.CLASS,
                        language="python",
                        provenance=ProvenanceRecord(
                            origin_parser="PythonParser",
                            creation_source="ast",
                            source_file=str(file_path),
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            confidence=0.9,
                        ),
                    ))
                    edges.append(self.create_edge(
                        file_node_id, class_id, EdgeType.BELONGS_TO
                    ))

                    # Inheritance edges
                    for base in bases:
                        base_id = self.generate_node_id(NodeType.CLASS, base)
                        edges.append(self.create_edge(
                            class_id, base_id, EdgeType.INHERITS, 0.85
                        ))

                    # Recurse into class body for methods
                    for child in node.children:
                        if child.type == "block":
                            traverse(child, parent_scope=fqn)
                    return  # Don't recurse again below

            # ── Functions / Methods ─────────────────────────────
            elif node.type == "function_definition":
                func_name = ""
                for child in node.children:
                    if child.type == "identifier":
                        func_name = _text(child)
                        break
                if func_name:
                    if parent_scope:
                        fqn = f"{parent_scope}::{func_name}"
                        node_type = NodeType.METHOD
                    else:
                        fqn = f"{rel_path}::{func_name}"
                        node_type = NodeType.FUNCTION

                    func_id = self.generate_node_id(node_type, fqn)
                    nodes.append(NodeMetadata(
                        unique_id=func_id,
                        node_type=node_type,
                        language="python",
                        provenance=ProvenanceRecord(
                            origin_parser="PythonParser",
                            creation_source="ast",
                            source_file=str(file_path),
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            confidence=0.9,
                        ),
                    ))
                    edges.append(self.create_edge(
                        file_node_id, func_id, EdgeType.BELONGS_TO
                    ))

            for child in node.children:
                traverse(child, parent_scope)

        traverse(tree.root_node)
        return nodes, edges
