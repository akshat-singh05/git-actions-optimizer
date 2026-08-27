"""
Graph Builder — constructs the heterogeneous MultiDiGraph from source code
and workflow YAML.

Orchestrates language-specific parsers (Python, JS, Java, Go) for source
code, and directly parses workflow YAML into CI/CD graph nodes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import networkx as nx

from gha_optimizer.graph.models import (
    EdgeMetadata,
    EdgeType,
    NodeMetadata,
    NodeType,
    ProvenanceRecord,
)
from gha_optimizer.ingestion.workflow_parser import WorkflowSpec

logger = logging.getLogger(__name__)


class GraphBuilder:
    """Orchestrates building the MultiDiGraph from parsers and CI/CD ingestion."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.graph = nx.MultiDiGraph()
        self._parsers: Dict[str, object] = {}
        self._parser_errors: Dict[str, str] = {}
        self._load_parsers()

    # ─── Parser registry ────────────────────────────────────────────

    def _load_parsers(self) -> None:
        """Load all available language parsers with honest error reporting."""
        # Python
        try:
            from gha_optimizer.graph.parsers.python import PythonParser
            self._parsers[".py"] = PythonParser(self.repo_root)
        except Exception as e:
            self._parser_errors[".py"] = str(e)
            logger.warning("Python parser unavailable: %s", e)

        # JavaScript / TypeScript
        try:
            from gha_optimizer.graph.parsers.javascript import JavaScriptParser
            parser = JavaScriptParser(self.repo_root)
            self._parsers[".js"] = parser
            self._parsers[".ts"] = parser
            self._parsers[".jsx"] = parser
            self._parsers[".tsx"] = parser
        except Exception as e:
            self._parser_errors[".js"] = str(e)
            logger.warning("JavaScript parser unavailable: %s", e)

        # Java
        try:
            from gha_optimizer.graph.parsers.java import JavaParser
            self._parsers[".java"] = JavaParser(self.repo_root)
        except Exception as e:
            self._parser_errors[".java"] = str(e)
            logger.warning("Java parser unavailable: %s", e)

        # Go
        try:
            from gha_optimizer.graph.parsers.go import GoParser
            self._parsers[".go"] = GoParser(self.repo_root)
        except Exception as e:
            self._parser_errors[".go"] = str(e)
            logger.warning("Go parser unavailable: %s", e)

    def get_parser_capabilities(self) -> Dict[str, dict]:
        """Report which parsers are available and their capabilities."""
        from gha_optimizer.schemas import ParserCapability
        result = {}
        for ext, parser in self._parsers.items():
            cap = getattr(parser, "capabilities", None)
            if cap:
                result[ext] = cap.model_dump()
            else:
                result[ext] = ParserCapability(
                    language=ext, available=True
                ).model_dump()
        for ext, err in self._parser_errors.items():
            if ext not in result:
                result[ext] = {"language": ext, "available": False,
                               "error_message": err}
        return result

    # ─── Node / Edge helpers ────────────────────────────────────────

    def add_node(self, metadata: NodeMetadata) -> None:
        """Add a node to the graph with full metadata."""
        self.graph.add_node(
            metadata.unique_id,
            **metadata.model_dump(exclude_none=True)
        )

    def add_edge(self, metadata: EdgeMetadata) -> None:
        """Add an edge to the graph with full metadata."""
        self.graph.add_edge(
            metadata.source_id,
            metadata.target_id,
            key=metadata.edge_type.value,
            **metadata.model_dump(exclude_none=True)
        )

    @staticmethod
    def _node_id(node_type: NodeType, name: str) -> str:
        """Standardised node ID format."""
        return f"{node_type.value}::{name}"

    def _make_provenance(self, parser: str = "WorkflowParser",
                         source: str = "workflow_yaml",
                         source_file: Optional[str] = None) -> ProvenanceRecord:
        return ProvenanceRecord(
            origin_parser=parser,
            creation_source=source,
            source_file=source_file,
            confidence=1.0,
        )

    # ─── Source code parsing ────────────────────────────────────────

    def build_from_source(self, file_paths: List[Path]) -> int:
        """Parse source files and add AST components to the graph.

        Returns the number of files successfully parsed.
        """
        parsed = 0
        for path in file_paths:
            suffix = path.suffix
            parser = self._parsers.get(suffix)
            if parser is None:
                continue
            try:
                nodes, edges = parser.parse_file(path)
                for node in nodes:
                    self.add_node(node)
                for edge in edges:
                    self.add_edge(edge)
                parsed += 1
            except Exception as exc:
                logger.warning("Parser failed on %s: %s", path, exc)
        logger.info("Parsed %d/%d source files", parsed, len(file_paths))
        return parsed

    # ─── Workflow → Graph (RULE 3) ──────────────────────────────────

    def build_from_workflow(self, workflow: WorkflowSpec,
                            workflow_file: Optional[str] = None) -> None:
        """
        Parse a WorkflowSpec into graph nodes and edges.

        Creates nodes for: Workflow, Trigger, Job, Step, Runner, Cache,
        Artifact, Environment, Action.

        Creates edges for: TRIGGERS, EXECUTES, DEPENDS_ON, RUNS_ON,
        RESTORES_CACHE, PRODUCES, CONSUMES, USES, BELONGS_TO.
        """
        prov = self._make_provenance(source_file=workflow_file)

        # ── Workflow node ───────────────────────────────────────────
        wf_id = self._node_id(NodeType.WORKFLOW, workflow.name)
        self.add_node(NodeMetadata(
            unique_id=wf_id,
            node_type=NodeType.WORKFLOW,
            provenance=prov,
            tags=["ci"],
        ))

        # ── Trigger nodes ───────────────────────────────────────────
        for event_name, trigger_filter in workflow.triggers.items():
            trigger_id = self._node_id(
                NodeType.WORKFLOW_TRIGGER,
                f"{workflow.name}::{event_name}"
            )
            self.add_node(NodeMetadata(
                unique_id=trigger_id,
                node_type=NodeType.WORKFLOW_TRIGGER,
                provenance=prov,
                tags=[event_name],
            ))
            # WORKFLOW_TRIGGERED_BY → use TRIGGERS edge
            self.add_edge(EdgeMetadata(
                source_id=trigger_id,
                target_id=wf_id,
                edge_type=EdgeType.TRIGGERS,
                provenance=prov,
            ))

        # ── Job nodes ───────────────────────────────────────────────
        for job_id, job_spec in workflow.jobs.items():
            job_node_id = self._node_id(NodeType.WORKFLOW_JOB, job_id)
            self.add_node(NodeMetadata(
                unique_id=job_node_id,
                node_type=NodeType.WORKFLOW_JOB,
                provenance=prov,
                tags=[job_id],
            ))

            # Workflow → Job (EXECUTES)
            self.add_edge(EdgeMetadata(
                source_id=wf_id,
                target_id=job_node_id,
                edge_type=EdgeType.EXECUTES,
                provenance=prov,
            ))

            # Job dependency edges (needs:)
            for dep_job_id in job_spec.needs:
                dep_node_id = self._node_id(NodeType.WORKFLOW_JOB, dep_job_id)
                self.add_edge(EdgeMetadata(
                    source_id=dep_node_id,
                    target_id=job_node_id,
                    edge_type=EdgeType.DEPENDS_ON,
                    provenance=prov,
                ))

            # Runner node
            runner_id = self._node_id(NodeType.RUNNER, job_spec.runs_on)
            if runner_id not in self.graph:
                self.add_node(NodeMetadata(
                    unique_id=runner_id,
                    node_type=NodeType.RUNNER,
                    provenance=prov,
                ))
            self.add_edge(EdgeMetadata(
                source_id=job_node_id,
                target_id=runner_id,
                edge_type=EdgeType.RUNS_ON,
                provenance=prov,
            ))

            # Environment node
            if job_spec.environment:
                env_id = self._node_id(NodeType.ENVIRONMENT, job_spec.environment)
                if env_id not in self.graph:
                    self.add_node(NodeMetadata(
                        unique_id=env_id,
                        node_type=NodeType.ENVIRONMENT,
                        provenance=prov,
                    ))
                self.add_edge(EdgeMetadata(
                    source_id=job_node_id,
                    target_id=env_id,
                    edge_type=EdgeType.USES,
                    provenance=prov,
                ))

            # ── Step nodes ──────────────────────────────────────────
            for step_idx, step in enumerate(job_spec.steps):
                step_name = step.get("name") or step.get("uses") or step.get("run", "")[:60]
                step_label = f"{job_id}::step_{step_idx}::{step_name}"
                step_node_id = self._node_id(NodeType.WORKFLOW_STEP, step_label)
                self.add_node(NodeMetadata(
                    unique_id=step_node_id,
                    node_type=NodeType.WORKFLOW_STEP,
                    provenance=prov,
                    tags=[step_name[:80]],
                ))

                # Job → Step (BELONGS_TO / contains)
                self.add_edge(EdgeMetadata(
                    source_id=job_node_id,
                    target_id=step_node_id,
                    edge_type=EdgeType.EXECUTES,
                    provenance=prov,
                ))

                # Step uses an Action
                uses = step.get("uses", "")
                if uses:
                    action_id = self._node_id(NodeType.EXTERNAL_DEPENDENCY, uses)
                    if action_id not in self.graph:
                        self.add_node(NodeMetadata(
                            unique_id=action_id,
                            node_type=NodeType.EXTERNAL_DEPENDENCY,
                            provenance=prov,
                            tags=["action"],
                        ))
                    self.add_edge(EdgeMetadata(
                        source_id=step_node_id,
                        target_id=action_id,
                        edge_type=EdgeType.USES,
                        provenance=prov,
                    ))

                # Cache detection
                if "actions/cache" in uses:
                    with_block = step.get("with", {})
                    cache_key = with_block.get("key", "")
                    cache_path = with_block.get("path", "")
                    cache_label = cache_key or cache_path or f"cache_{job_id}"
                    cache_node_id = self._node_id(NodeType.CACHE_KEY, cache_label)
                    if cache_node_id not in self.graph:
                        self.add_node(NodeMetadata(
                            unique_id=cache_node_id,
                            node_type=NodeType.CACHE_KEY,
                            provenance=prov,
                            tags=["cache"],
                        ))
                    self.add_edge(EdgeMetadata(
                        source_id=job_node_id,
                        target_id=cache_node_id,
                        edge_type=EdgeType.RESTORES_CACHE,
                        provenance=prov,
                    ))

                # Artifact upload detection
                if "actions/upload-artifact" in uses:
                    art_name = step.get("with", {}).get("name", f"artifact_{job_id}")
                    art_id = self._node_id(NodeType.ARTIFACT, art_name)
                    if art_id not in self.graph:
                        self.add_node(NodeMetadata(
                            unique_id=art_id,
                            node_type=NodeType.ARTIFACT,
                            provenance=prov,
                        ))
                    self.add_edge(EdgeMetadata(
                        source_id=job_node_id,
                        target_id=art_id,
                        edge_type=EdgeType.PRODUCES,
                        provenance=prov,
                    ))

                # Artifact download detection
                if "actions/download-artifact" in uses:
                    art_name = step.get("with", {}).get("name", f"artifact_{job_id}")
                    art_id = self._node_id(NodeType.ARTIFACT, art_name)
                    if art_id not in self.graph:
                        self.add_node(NodeMetadata(
                            unique_id=art_id,
                            node_type=NodeType.ARTIFACT,
                            provenance=prov,
                        ))
                    self.add_edge(EdgeMetadata(
                        source_id=art_id,
                        target_id=job_node_id,
                        edge_type=EdgeType.CONSUMES,
                        provenance=prov,
                    ))

        logger.info(
            "Built workflow graph for '%s': %d nodes, %d edges",
            workflow.name,
            self.graph.number_of_nodes(),
            self.graph.number_of_edges(),
        )

    # ─── Convenience ────────────────────────────────────────────────

    def get_graph(self) -> nx.MultiDiGraph:
        """Return the constructed graph."""
        return self.graph
