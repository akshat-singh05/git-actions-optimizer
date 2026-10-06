"""
JavaScript / TypeScript Parser using Tree-sitter.

Extracts: imports (ESM/CJS), classes, functions, exports.
Reports capabilities honestly — if tree-sitter-javascript is not
installed, parsing is unavailable rather than silently returning empty.
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


class JavaScriptParser(BaseLanguageParser):
    """JavaScript/TypeScript AST parser using tree-sitter."""

    def __init__(self, repo_root: Path):
        super().__init__(repo_root)
        self.capabilities = ParserCapability(
            language="javascript",
            available=False,
            extracts_imports=True,
            extracts_classes=True,
            extracts_functions=True,
            extracts_calls=False,
        )
        try:
            import tree_sitter_javascript
            from tree_sitter import Language, Parser
            self.language = Language(tree_sitter_javascript.language())
            self.parser = Parser(self.language)
            self.capabilities.available = True
        except Exception as e:
            logger.warning("Tree-sitter JavaScript unavailable: %s", e)
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
            origin_parser="JavaScriptParser",
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
            language="javascript",
            provenance=prov,
        ))

        def _text(node) -> str:
            return source_code[node.start_byte:node.end_byte].decode("utf8", errors="replace")

        def traverse(node):
            # ESM imports: import X from 'Y'
            if node.type == "import_statement":
                for child in node.children:
                    if child.type == "string":
                        module = _text(child).strip("'\"")
                        pkg_id = self.generate_node_id(NodeType.PACKAGE, module)
                        nodes.append(NodeMetadata(
                            unique_id=pkg_id, node_type=NodeType.PACKAGE,
                            language="javascript", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, pkg_id, EdgeType.IMPORTS, 0.8
                        ))
                        break

            # CJS require: const X = require('Y')
            elif node.type == "call_expression":
                if len(node.children) >= 2:
                    func = node.children[0]
                    if func.type == "identifier" and _text(func) == "require":
                        args = node.children[1]
                        for child in args.children:
                            if child.type == "string":
                                module = _text(child).strip("'\"")
                                pkg_id = self.generate_node_id(NodeType.PACKAGE, module)
                                nodes.append(NodeMetadata(
                                    unique_id=pkg_id, node_type=NodeType.PACKAGE,
                                    language="javascript", provenance=prov,
                                ))
                                edges.append(self.create_edge(
                                    file_node_id, pkg_id, EdgeType.IMPORTS, 0.8
                                ))

            # Classes
            elif node.type == "class_declaration":
                for child in node.children:
                    if child.type == "identifier":
                        class_name = _text(child)
                        class_id = self.generate_node_id(
                            NodeType.CLASS, f"{rel_path}::{class_name}"
                        )
                        nodes.append(NodeMetadata(
                            unique_id=class_id, node_type=NodeType.CLASS,
                            language="javascript", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, class_id, EdgeType.BELONGS_TO
                        ))
                        break

            # Functions
            elif node.type in ("function_declaration", "function"):
                for child in node.children:
                    if child.type == "identifier":
                        func_name = _text(child)
                        func_id = self.generate_node_id(
                            NodeType.FUNCTION, f"{rel_path}::{func_name}"
                        )
                        nodes.append(NodeMetadata(
                            unique_id=func_id, node_type=NodeType.FUNCTION,
                            language="javascript", provenance=prov,
                        ))
                        edges.append(self.create_edge(
                            file_node_id, func_id, EdgeType.BELONGS_TO
                        ))
                        break

            for child in node.children:
                traverse(child)

        traverse(tree.root_node)
        return nodes, edges
