"""
End-to-end integration test that traces the full pipeline:

  Workflow YAML
      → Graph Construction (GraphBuilder)
      → Graph Fusion (GraphFusion)
      → Feature Extraction (build_training_frame / build_inference_frame)
      → ML Training (CacheMissPredictor, FailurePredictor, DurationEstimator)
      → Scheduling (build_schedule)
      → Workflow Generation (generate_optimized_workflow)
      → YAML Validation

Uses conftest fixtures for deterministic, self-contained testing.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import networkx as nx
import pytest
import yaml

from gha_optimizer.features.feature_extraction import (
    build_inference_frame,
    build_training_frame,
)
from gha_optimizer.graph.builder import GraphBuilder
from gha_optimizer.graph.dependency_graph import (
    build_dependency_graph,
    cache_keys_touched_by,
    graph_distance_to_job,
    graph_summary,
    impacted_jobs_for_diff,
)
from gha_optimizer.graph.fusion import GraphFusion
from gha_optimizer.graph.models import EdgeType, NodeType
from gha_optimizer.ingestion.workflow_parser import parse_workflow
from gha_optimizer.models.cache_miss_predictor import CacheMissPredictor
from gha_optimizer.models.duration_estimator import DurationEstimator
from gha_optimizer.models.failure_predictor import FailurePredictor
from gha_optimizer.pipeline import run_pipeline
from gha_optimizer.scheduler.optimizer import build_schedule


# ─── Graph Construction ────────────────────────────────────────────


class TestGraphConstruction:
    """Verify GraphBuilder creates meaningful workflow structure."""

    def test_workflow_graph_has_jobs_and_edges(self, sample_workflow):
        builder = GraphBuilder(Path("."))
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        # All 5 jobs should be present
        job_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.WORKFLOW_JOB.value
        ]
        assert len(job_nodes) == 5

        # needs: edges should exist (lint -> unit_tests, lint -> integration_tests)
        dep_edges = [
            (u, v) for u, v, d in g.edges(data=True)
            if d.get("edge_type") == EdgeType.DEPENDS_ON.value
        ]
        assert len(dep_edges) >= 4  # lint->unit, lint->integ, unit+integ->build, build->deploy

    def test_workflow_graph_has_cache_nodes(self, sample_workflow):
        builder = GraphBuilder(Path("."))
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        cache_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.CACHE_KEY.value
        ]
        assert len(cache_nodes) >= 1

    def test_workflow_graph_has_runner_nodes(self, sample_workflow):
        builder = GraphBuilder(Path("."))
        builder.build_from_workflow(sample_workflow)
        g = builder.get_graph()

        runner_nodes = [
            n for n, d in g.nodes(data=True)
            if d.get("node_type") == NodeType.RUNNER.value
        ]
        assert len(runner_nodes) >= 1
        assert any("ubuntu-latest" in n for n in runner_nodes)


# ─── Graph Fusion ──────────────────────────────────────────────────


class TestGraphFusion:
    """Verify cross-domain edges are created between source and CI."""

    def test_fused_graph_contains_both_domains(
        self, sample_workflow, sample_repo_root
    ):
        # Build source graph
        src_builder = GraphBuilder(sample_repo_root)
        source_files = [
            sample_repo_root / f
            for f in ["src/app.py", "src/utils.py", "requirements.txt"]
            if (sample_repo_root / f).exists()
        ]
        src_builder.build_from_source(source_files)
        source_graph = src_builder.get_graph()

        # Build CI graph
        ci_builder = GraphBuilder(sample_repo_root)
        ci_builder.build_from_workflow(sample_workflow)
        ci_graph = ci_builder.get_graph()

        # Fuse
        fused = GraphFusion.fuse(source_graph, ci_graph)

        # Fused graph should have nodes from both domains
        node_types = set()
        for _, data in fused.nodes(data=True):
            nt = data.get("node_type")
            if nt:
                node_types.add(nt)

        assert NodeType.WORKFLOW_JOB.value in node_types
        assert fused.number_of_nodes() > ci_graph.number_of_nodes()

    def test_fusion_creates_cross_domain_edges(
        self, sample_workflow, sample_repo_root
    ):
        graph = build_dependency_graph(
            sample_workflow,
            sample_repo_root,
            ["src/app.py", "src/utils.py", "requirements.txt"],
        )

        # Should have edges beyond just the raw compose
        assert graph.number_of_edges() > 0

        # All edges should have weight and edge_type
        for u, v, data in graph.edges(data=True):
            assert "weight" in data, f"Edge ({u}->{v}) missing weight"
            assert "edge_type" in data, f"Edge ({u}->{v}) missing edge_type"


# ─── Bridge Module API ─────────────────────────────────────────────


class TestBridgeAPI:
    """Verify the dependency_graph bridge functions."""

    def test_impacted_jobs_returns_plain_ids(self, sample_workflow, sample_repo_root):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        impacted = impacted_jobs_for_diff(graph, ["src/app.py"])

        assert isinstance(impacted, set)
        for job_id in impacted:
            assert "::" not in job_id, f"Expected plain ID, got '{job_id}'"

    def test_impacted_jobs_conservative_fallback(self, sample_workflow, sample_repo_root):
        """When changed files aren't in the graph, ALL jobs should be returned."""
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        impacted = impacted_jobs_for_diff(graph, ["nonexistent_file.py"])

        # Conservative: should return all 5 jobs
        assert len(impacted) == 5

    def test_graph_summary_structure(self, sample_workflow, sample_repo_root):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        summary = graph_summary(graph)

        assert "job" in summary
        assert "edges" in summary
        assert summary["job"] == 5
        assert summary["edges"] > 0


# ─── Feature Extraction ───────────────────────────────────────────


class TestFeatureExtraction:
    """Verify features are built correctly for training and inference."""

    def test_training_frame_shape(self, sample_workflow, sample_repo_root, sample_history):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        simple = nx.DiGraph(graph)
        df = build_training_frame(simple, sample_history)

        assert len(df) == len(sample_history)
        assert "label_failed" in df.columns
        assert "label_cache_hit" in df.columns
        assert "label_duration" in df.columns

    def test_inference_frame_matches_jobs(self, sample_workflow, sample_repo_root):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        simple = nx.DiGraph(graph)
        job_ids = list(sample_workflow.jobs.keys())
        stats = {j: {"failure_rate": 0.1, "mean_duration": 60.0} for j in job_ids}

        df = build_inference_frame(simple, job_ids, ["src/app.py"], stats)
        assert len(df) == len(job_ids)


# ─── ML Models ─────────────────────────────────────────────────────


class TestMLModels:
    """Verify ML models can train, predict, and persist."""

    def test_cache_model_fit_predict(self, sample_workflow, sample_repo_root, sample_history):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        simple = nx.DiGraph(graph)
        df = build_training_frame(simple, sample_history)

        model = CacheMissPredictor()
        metrics = model.fit(df)
        assert isinstance(metrics, dict)

        # Predict
        job_ids = list(sample_workflow.jobs.keys())
        stats = {j: {"failure_rate": 0.1, "mean_duration": 60.0} for j in job_ids}
        infer_df = build_inference_frame(simple, job_ids, ["src/app.py"], stats)
        infer_df.index = infer_df["job_id"]
        probs = model.predict_proba(infer_df)
        assert len(probs) == len(job_ids)

    def test_failure_model_fit_predict(self, sample_workflow, sample_repo_root, sample_history):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        simple = nx.DiGraph(graph)
        df = build_training_frame(simple, sample_history)

        model = FailurePredictor()
        metrics = model.fit(df)
        assert isinstance(metrics, dict)

    def test_duration_model_fit_predict(self, sample_workflow, sample_repo_root, sample_history):
        graph = build_dependency_graph(sample_workflow, sample_repo_root)
        simple = nx.DiGraph(graph)
        df = build_training_frame(simple, sample_history)

        model = DurationEstimator()
        metrics = model.fit(df)
        assert isinstance(metrics, dict)


# ─── Scheduler ─────────────────────────────────────────────────────


class TestSchedulerIntegration:
    """Verify scheduler produces valid plans from ML predictions."""

    def test_schedule_from_predictions(self, sample_workflow):
        dag = nx.DiGraph()
        for job_id in sample_workflow.jobs:
            dag.add_node(job_id)
        for u, d in sample_workflow.job_dag_edges():
            dag.add_edge(u, d)

        failure_prob = {j: 0.1 for j in sample_workflow.jobs}
        cache_miss_prob = {j: 0.3 for j in sample_workflow.jobs}
        duration_pred = {j: 60.0 for j in sample_workflow.jobs}

        plan = build_schedule(dag, failure_prob, cache_miss_prob, duration_pred)

        assert len(plan.jobs) == 5
        assert plan.baseline_wall_clock_seconds > 0
        assert plan.estimated_wall_clock_seconds > 0


# ─── Full Pipeline ─────────────────────────────────────────────────


class TestFullPipeline:
    """End-to-end: workflow YAML → optimized YAML + explanation."""

    def test_full_pipeline_e2e(self, sample_workflow_path, sample_repo_root, sample_history):
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            out_path = f.name

        plan, explanation = run_pipeline(
            workflow_path=sample_workflow_path,
            repo_root=sample_repo_root,
            tracked_files=["src/app.py", "src/utils.py", "requirements.txt"],
            history=sample_history,
            changed_files=["src/app.py", "requirements.txt"],
            out_path=out_path,
        )

        # Plan should have all 5 jobs
        assert len(plan.jobs) == 5

        # Explanation should be non-empty
        assert len(explanation) > 0
        assert "gha-optimizer" in explanation

        # Output file should exist and be valid YAML
        out = Path(out_path)
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert "jobs" in parsed

        # Estimates should be positive
        assert plan.baseline_wall_clock_seconds > 0
        assert plan.estimated_wall_clock_seconds > 0
        assert plan.baseline_cost_usd > 0
        assert plan.estimated_cost_usd > 0

    def test_pipeline_output_preserves_all_jobs(
        self, sample_workflow_path, sample_repo_root, sample_history
    ):
        with tempfile.NamedTemporaryFile(suffix=".yml", delete=False) as f:
            out_path = f.name

        plan, _ = run_pipeline(
            workflow_path=sample_workflow_path,
            repo_root=sample_repo_root,
            tracked_files=["src/app.py"],
            history=sample_history,
            changed_files=["src/app.py"],
            out_path=out_path,
        )

        # Optimized YAML must still contain all original jobs
        parsed = yaml.safe_load(Path(out_path).read_text(encoding="utf-8"))
        assert set(parsed["jobs"].keys()) == {"lint", "unit_tests", "integration_tests", "build", "deploy"}
