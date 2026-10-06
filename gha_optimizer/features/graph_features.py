"""
Graph-derived features computed on the dependency graph of the repository **as it
was at each run's commit** (not a static graph, not today's checkout).

For a run at commit ``C`` of workflow ``W`` with changed files ``F``:

1. the tree at ``C`` is read from the local git mirror (no API calls);
2. every parseable source file (.py .js .jsx .ts .tsx .java .go) is parsed by the
   project's own tree-sitter parsers — results are cached per ``(path, blob sha)``
   so a file is parsed once however many commits contain it;
3. the workflow YAML *at C* becomes the CI graph, and the two are fused exactly as
   in production (``GraphFusion.fuse`` via ``build_dependency_graph``);
4. per workflow job key the features ``job_structurally_impacted``,
   ``cache_key_touched`` (a cache key restored by *that job*) and
   ``dep_graph_distance`` are derived from ``F``.

``any_changed_file_in_graph`` records whether any changed file was a graph node.
When it is 0 the "all jobs impacted" fallback fired, i.e. the structural feature
carries no information for that run; DATASET_REPORT.md quantifies how often.
"""
from __future__ import annotations

import logging
import pickle
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx
from sqlalchemy.orm import Session

from gha_optimizer.graph.builder import GraphBuilder
from gha_optimizer.graph.dependency_graph import (
    any_changed_file_in_graph,
    build_dependency_graph,
    cache_key_touched_for_job,
    graph_distances_from_files,
    impacted_jobs_for_diff,
)
from gha_optimizer.ingestion.mirror import RepoMirror
from gha_optimizer.ingestion.workflow_parser import parse_workflow_text
from gha_optimizer.server.db import models as m

logger = logging.getLogger(__name__)

SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go"}
EXCLUDED_DIRS = ("node_modules/", "vendor/", "third_party/", "dist/", "build/", ".git/", "__pycache__/")
MAX_PATH_LEN = 240  # Windows MAX_PATH guard for the temporary materialisation of a blob
MAX_SOURCE_BYTES = 1_000_000


def _wanted(path: str) -> bool:
    if Path(path).suffix not in SOURCE_SUFFIXES:
        return False
    padded = "/" + path
    return not any(f"/{d}" in padded for d in EXCLUDED_DIRS)


class ParseCache:
    """On-disk cache: ``(path, blob_sha) -> (nodes, edges)`` as plain tuples (fast to re-assemble)."""

    def __init__(self, path: Path | None):
        self._mem: dict[tuple[str, str], Any] = {}
        self._db: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path))
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS parse (path TEXT, blob TEXT, payload BLOB, PRIMARY KEY (path, blob))"
            )
            self._db.commit()
        self._pending = 0

    def get(self, path: str, blob: str) -> Any | None:
        key = (path, blob)
        if key in self._mem:
            return self._mem[key]
        if self._db is not None:
            row = self._db.execute("SELECT payload FROM parse WHERE path=? AND blob=?", key).fetchone()
            if row is not None:
                self._mem[key] = pickle.loads(row[0])
                return self._mem[key]
        return None

    def put(self, path: str, blob: str, entry: Any) -> None:
        self._mem[(path, blob)] = entry
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO parse VALUES (?,?,?)", (path, blob, pickle.dumps(entry, protocol=4))
            )
            self._pending += 1
            if self._pending >= 500:
                self._db.commit()
                self._pending = 0

    def close(self) -> None:
        if self._db is not None:
            self._db.commit()
            self._db.close()
            self._db = None


class GraphFeatureComputer:
    def __init__(self, mirror: RepoMirror, cache_path: Path | None = None):
        self.mirror = mirror
        self.cache = ParseCache(cache_path)
        self._tmp = Path(tempfile.mkdtemp(prefix="g_"))
        self._builder = GraphBuilder(self._tmp)  # only used for its registered parsers
        self.stats = {"files_parsed": 0, "files_cached": 0, "parse_errors": 0, "files_skipped_long_path": 0}

    def close(self) -> None:
        self.cache.close()

    # ------------------------------------------------------------------
    def _parse_blob(self, path: str, data: bytes) -> tuple[list, list]:
        parser = self._builder._parsers.get(Path(path).suffix)
        if parser is None or len(data) > MAX_SOURCE_BYTES:
            return [], []
        target = self._tmp / path
        if len(str(target)) > MAX_PATH_LEN:
            self.stats["files_skipped_long_path"] += 1
            return [], []
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        try:
            nodes, edges = parser.parse_file(target)
            return (
                [(n.unique_id, n.model_dump(exclude_none=True)) for n in nodes],
                [(e.source_id, e.target_id, e.edge_type.value, e.model_dump(exclude_none=True)) for e in edges],
            )
        except Exception as exc:  # a parser failure disables that file, as GraphBuilder does
            self.stats["parse_errors"] += 1
            logger.debug("parse failed for %s: %s", path, exc)
            return [], []
        finally:
            try:
                target.unlink()
            except OSError:
                pass

    def source_graph(self, commit: str) -> nx.MultiDiGraph:
        graph = nx.MultiDiGraph()
        files = [(p, b) for p, b in self.mirror.list_tree(commit) if _wanted(p)]
        reader = None
        try:
            for path, blob in files:
                entry = self.cache.get(path, blob)
                if entry is None:
                    if reader is None:
                        reader = self.mirror.blob_reader()
                    data = reader.read(blob)
                    entry = self._parse_blob(path, data) if data is not None else ([], [])
                    self.cache.put(path, blob, entry)
                    self.stats["files_parsed"] += 1
                else:
                    self.stats["files_cached"] += 1
                nodes, edges = entry
                for nid, attrs in nodes:
                    graph.add_node(nid, **attrs)
                for src, dst, key, attrs in edges:
                    graph.add_edge(src, dst, key=key, **attrs)
        finally:
            if reader is not None:
                reader.close()
        return graph

    def graph_at(self, commit: str, workflow_text: str) -> nx.MultiDiGraph | None:
        try:
            workflow = parse_workflow_text(workflow_text)
        except Exception as exc:
            logger.warning("workflow YAML at %s not parseable: %s", commit[:8], exc)
            return None
        return build_dependency_graph(workflow, self._tmp, source_graph=self.source_graph(commit))

    @staticmethod
    def features(g: nx.MultiDiGraph, changed_files: list[str], job_keys: list[str]) -> dict[str, dict[str, float]]:
        impacted = impacted_jobs_for_diff(g, changed_files)
        dists = graph_distances_from_files(g, changed_files, job_keys)
        in_graph = float(any_changed_file_in_graph(g, changed_files))
        return {
            j: {
                "job_structurally_impacted": float(j in impacted),
                "cache_key_touched": float(cache_key_touched_for_job(g, changed_files, j)),
                "dep_graph_distance": dists.get(j, -1.0),
                "any_changed_file_in_graph": in_graph,
            }
            for j in job_keys
        }


def compute_repo_graph_features(
    db: Session,
    repo: str,
    mirror: RepoMirror,
    cache_path: Path | None = None,
    workflow_path: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Compute and persist ``run_graph_features`` for every ingested run that lacks them.

    Idempotent (natural key ``(run_id, job_key)``).  Runs are grouped by commit so each
    commit's graph is assembled once.  Commits missing from the mirror are fetched by sha;
    if that fails the run simply has no graph features (they stay NULL downstream).
    """
    counters = {"runs_total": 0, "runs_computed": 0, "runs_skipped_existing": 0, "runs_no_commit": 0,
                "runs_no_workflow": 0, "rows_written": 0}
    q = db.query(m.WorkflowRun).filter(
        m.WorkflowRun.repo == repo, m.WorkflowRun.jobs_fetched_at.isnot(None), m.WorkflowRun.diff_source.isnot(None)
    )
    if workflow_path:
        q = q.filter(m.WorkflowRun.workflow_path == workflow_path)
    runs = q.order_by(m.WorkflowRun.commit_sha, m.WorkflowRun.gh_created_at).all()
    if limit:
        runs = runs[:limit]
    counters["runs_total"] = len(runs)
    computer = GraphFeatureComputer(mirror, cache_path)
    graph_cache: tuple[tuple[str, str], nx.MultiDiGraph | None] | None = None
    try:
        for i, run in enumerate(runs, 1):
            keys = sorted({k for (k,) in db.query(m.WorkflowJob.job_key).filter(
                m.WorkflowJob.run_id == run.run_id, m.WorkflowJob.job_key.isnot(None)).distinct()})
            have = {k for (k,) in db.query(m.RunGraphFeatures.job_key).filter_by(run_id=run.run_id)}
            todo = [k for k in keys if k not in have]
            if not todo:
                counters["runs_skipped_existing"] += 1
                continue
            blob = (
                db.query(m.WorkflowBlob).filter_by(repo=repo, blob_sha=run.workflow_blob_sha).one_or_none()
                if run.workflow_blob_sha and run.workflow_blob_sha != "unavailable" else None
            )
            if blob is None or not blob.content:
                counters["runs_no_workflow"] += 1
                continue
            if not mirror.fetch_sha(run.commit_sha):
                counters["runs_no_commit"] += 1
                continue
            gkey = (run.commit_sha, run.workflow_blob_sha)
            if graph_cache is None or graph_cache[0] != gkey:
                graph_cache = (gkey, computer.graph_at(run.commit_sha, blob.content))
            g = graph_cache[1]
            if g is None:
                counters["runs_no_workflow"] += 1
                continue
            files = [f for (f,) in db.query(m.RunChangedFile.filename).filter_by(run_id=run.run_id)]
            feats = computer.features(g, files, todo)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for k, f in feats.items():
                db.add(m.RunGraphFeatures(
                    run_id=run.run_id, job_key=k,
                    job_structurally_impacted=int(f["job_structurally_impacted"]),
                    cache_key_touched=int(f["cache_key_touched"]),
                    dep_graph_distance=f["dep_graph_distance"],
                    any_changed_file_in_graph=int(f["any_changed_file_in_graph"]),
                    graph_nodes=g.number_of_nodes(), graph_edges=g.number_of_edges(), computed_at=now,
                ))
                counters["rows_written"] += 1
            db.commit()
            counters["runs_computed"] += 1
            if i % 25 == 0:
                logger.info("graph features: %d/%d runs (%s)", i, len(runs), computer.stats)
    finally:
        computer.close()
    counters.update({f"parse_{k}": v for k, v in computer.stats.items()})
    return counters
