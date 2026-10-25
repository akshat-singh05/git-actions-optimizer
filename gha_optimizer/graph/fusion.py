"""
Graph Fusion Engine - connects source files, CI/CD and caches.

Fusion links ``File`` nodes to the ``WorkflowJob`` nodes (and ``CacheKey`` nodes) that *read*
them, using only what the workflow itself states.  Every rule is static and labelled with its
own confidence and ``creation_source``; none is learned and none is tuned to a dataset.

Rules, strongest evidence first
  1. ``paths_filter_gate``      (0.95) a job gated on ``needs.<job>.outputs.<name>`` where ``<job>`` runs
                                 ``dorny/paths-filter``: the files matching that filter's globs.  The gate is
                                 a precondition for the job to run at all, so for a gated job it SUPERSEDES
                                 rules 2-4 (a gated job is not also linked to every file its toolchain reads).
  2. ``step_path_reference``    (0.80) a path / directory argument of a ``run:`` command that exists in
                                 the tree (``pytest tests/unit``, ``./scripts/x.sh``).  A file links
                                 straight to the job; a directory goes through a ``Directory`` node.
  3. ``toolchain_command``      (0.50) the command drives a language toolchain (pytest, go test, mvn, ...)
                                 so it reads that language's sources under its working directory.  An
                                 explicit OVER-approximation: it cannot tell which modules a build touches.
  4. ``setup_action_toolchain`` (0.40) the job runs ``actions/setup-go`` etc. but no command was recognised.
  5. ``cache_key_hashfiles`` / ``implicit_cache_default`` (0.90 / 0.80) File -> CacheKey for the globs
                                 a cache key is a hash of.

Coarse scopes (rules 3-4) are a ``BuildTarget`` node (``File -BELONGS_TO-> BuildTarget -DEPENDS_ON-> Job``)
rather than one edge per file and job.

What fusion does *not* know: modules selected at run time by a script (e.g. Maven impact analysis),
``make`` targets (the Makefile is not read), imports between source files.  A job with no edge from any
file stays unlinked and downstream features report it as unknown rather than "unaffected".
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import networkx as nx

from gha_optimizer.graph.ci_semantics import (
    SETUP_ACTION_TOOLCHAIN,
    TOOLCHAINS,
    PathIndex,
    classify_segment,
    command_segments,
    has_glob,
    needs_output_refs,
    normalize_path,
    parse_paths_filter,
    path_candidates,
    step_output_refs,
)
from gha_optimizer.graph.models import EdgeType, NodeType, ProvenanceRecord

logger = logging.getLogger(__name__)

_FILE = NodeType.FILE.value
_JOB = NodeType.WORKFLOW_JOB.value
_STEP = NodeType.WORKFLOW_STEP.value
_CACHE = NodeType.CACHE_KEY.value
_DIR = NodeType.DIRECTORY.value
_TARGET = NodeType.BUILD_TARGET.value

CONF_PATHS_FILTER = 0.95
CONF_PATH_REFERENCE = 0.80
CONF_TOOLCHAIN = 0.50
CONF_SETUP_ACTION = 0.40
CONF_CACHE_HASHFILES = 0.90
CONF_CACHE_IMPLICIT = 0.80


class GraphFusion:
    """Merges the source-file graph with the workflow graph and adds cross-domain edges."""

    @staticmethod
    def fuse(static_graph: nx.MultiDiGraph, ci_graph: nx.MultiDiGraph) -> nx.MultiDiGraph:
        merged = nx.compose(static_graph, ci_graph)
        index = PathIndex(n[len(_FILE) + 2:] for n, d in merged.nodes(data=True) if d.get("node_type") == _FILE)
        jobs = _job_facts(merged)

        gate_edges, gated = GraphFusion._link_paths_filter_gates(merged, index, jobs)
        ungated = {k: j for k, j in jobs.items() if k not in gated}
        stats = {
            "paths_filter_edges": gate_edges,
            "path_reference_edges": GraphFusion._link_path_references(merged, index, ungated),
            "toolchain_edges": GraphFusion._link_toolchains(merged, index, ungated),
            "cache_edges": GraphFusion._link_caches(merged, index),
        }
        stats["gated_jobs"] = len(gated)
        stats["jobs_total"] = len(jobs)
        stats["files_total"] = len(index)
        merged.graph["fusion_stats"] = stats
        logger.info("Graph fusion: %d nodes, %d edges, %s", merged.number_of_nodes(), merged.number_of_edges(), stats)
        return merged

    # ─── rule 1: dorny/paths-filter gates ───────────────────────────

    @staticmethod
    def _link_paths_filter_gates(
        g: nx.MultiDiGraph, index: PathIndex, jobs: dict[str, "_Job"]
    ) -> tuple[int, set[str]]:
        """Returns ``(edges added, keys of the jobs gated by a paths-filter output)``."""
        # (job key, output name) -> (job key, filter name, globs)
        gates: dict[tuple[str, str], tuple[str, str, list[str]]] = {}
        for jkey, job in jobs.items():
            filters_by_step: dict[str, dict[str, list[str]]] = {}
            for step in job.steps:
                if step["uses"].startswith("dorny/paths-filter"):
                    parsed = parse_paths_filter(step["with"].get("filters"))
                    if parsed:
                        filters_by_step[step["id"] or ""] = parsed
            if not filters_by_step:
                continue
            for out_name, expr in job.outputs.items():
                for step_id, filter_name in step_output_refs(expr):
                    globs = filters_by_step.get(step_id, {}).get(filter_name)
                    if globs:
                        gates[(jkey, out_name)] = (jkey, filter_name, globs)
            for step_id, parsed in filters_by_step.items():  # an output that is not re-exported
                for name, globs in parsed.items():
                    gates.setdefault((jkey, name), (jkey, name, globs))

        added = 0
        gated: set[str] = set()
        prov = _prov("paths_filter_gate", CONF_PATHS_FILTER)
        for jkey, job in jobs.items():
            texts = list(job.conditions) + [s["if"] for s in job.steps if s["if"]] + [str(v) for v in job.with_.values()]
            refs = {ref for t in texts for ref in needs_output_refs(t)}
            for ref in sorted(refs):
                if ref not in gates:
                    continue
                gated.add(jkey)
                fjob, fname, globs = gates[ref]
                scope = f"{_TARGET}::paths-filter:{fjob}.{fname}"
                if scope not in g:
                    g.add_node(scope, node_type=_TARGET, attributes={"kind": "paths_filter", "globs": globs}, tags=[])
                    members = {f for pat in globs for f in index.glob(pat)}
                    added += _link_files(g, members, scope, EdgeType.BELONGS_TO, prov)
                added += _link(g, scope, job.node, EdgeType.REFERENCES, prov)
        return added, gated

    # ─── rule 2: explicit path arguments ────────────────────────────

    @staticmethod
    def _link_path_references(g: nx.MultiDiGraph, index: PathIndex, jobs: dict[str, "_Job"]) -> int:
        added = 0
        prov = _prov("step_path_reference", CONF_PATH_REFERENCE)
        for job in jobs.values():
            for step in job.steps:
                wd = normalize_path(step["wd"] or "")
                for seg in command_segments(step["run"]):
                    is_test = any(kind == "test" for _, kind in classify_segment(seg))
                    for cand in path_candidates(seg):
                        resolved = _resolve(index, cand, wd)
                        if resolved is None:
                            continue
                        kind, value = resolved
                        etype = EdgeType.TESTS if is_test else EdgeType.REFERENCES
                        if kind == "file":
                            added += _link(g, f"{_FILE}::{value}", job.node, etype, prov)
                        elif kind == "dir":
                            dnode = f"{_DIR}::{value}"
                            if dnode not in g:
                                g.add_node(dnode, node_type=_DIR, tags=[], attributes={})
                                added += _link_files(g, index.under(value), dnode, EdgeType.BELONGS_TO, prov)
                            added += _link(g, dnode, job.node, etype, prov)
                        else:  # glob
                            added += _link_files(g, value, job.node, etype, prov)
        return added

    # ─── rules 3-4: toolchain scope ─────────────────────────────────

    @staticmethod
    def _link_toolchains(g: nx.MultiDiGraph, index: PathIndex, jobs: dict[str, "_Job"]) -> int:
        added = 0
        prov_cmd = _prov("toolchain_command", CONF_TOOLCHAIN)
        prov_setup = _prov("setup_action_toolchain", CONF_SETUP_ACTION)
        for job in jobs.values():
            recognised: set[tuple[str, str, str]] = set()  # (toolchain, extent, scope)
            for step in job.steps:
                wd = normalize_path(step["wd"] or "")
                for seg in command_segments(step["run"]):
                    kinds = classify_segment(seg)
                    explicit = any(_resolve(index, c, wd) is not None for c in path_candidates(seg))
                    for tc, kind in kinds:
                        if kind == "install":
                            recognised.add((tc, "manifests", wd))
                        elif kind == "check" and explicit:
                            continue  # a linter/checker reads the paths it was given (rule 2 links those)
                        else:
                            recognised.add((tc, "all", wd))
            for tc, extent, scope in sorted(recognised):
                added += _link(g, _toolchain_target(g, index, tc, extent, scope), job.node, EdgeType.DEPENDS_ON, prov_cmd)
            if not recognised:
                for step in job.steps:
                    action = step["uses"].split("@", 1)[0]
                    setup_tc = SETUP_ACTION_TOOLCHAIN.get(action)
                    if setup_tc:
                        scope = normalize_path(step["wd"] or "")
                        added += _link(g, _toolchain_target(g, index, setup_tc, "all", scope), job.node,
                                       EdgeType.DEPENDS_ON, prov_setup)
        return added

    # ─── rule 5: caches ─────────────────────────────────────────────

    @staticmethod
    def _link_caches(g: nx.MultiDiGraph, index: PathIndex) -> int:
        added = 0
        for cache_id, data in list(g.nodes(data=True)):
            if data.get("node_type") != _CACHE:
                continue
            attrs = data.get("attributes") or {}
            implicit = bool(attrs.get("implicit"))
            prov = _prov("implicit_cache_default" if implicit else "cache_key_hashfiles",
                         CONF_CACHE_IMPLICIT if implicit else CONF_CACHE_HASHFILES)
            for pat in attrs.get("hash_globs") or []:
                added += _link_files(g, index.glob(pat), cache_id, EdgeType.INVALIDATES_CACHE, prov)
        return added


# ─── helpers ────────────────────────────────────────────────────────

class _Job:
    __slots__ = ("node", "steps", "conditions", "outputs", "with_")

    def __init__(self, node: str, steps: list[dict[str, Any]], conditions: list[str],
                 outputs: dict[str, str], with_: dict[str, Any]):
        self.node, self.steps, self.conditions, self.outputs, self.with_ = node, steps, conditions, outputs, with_


def _job_facts(g: nx.MultiDiGraph) -> dict[str, _Job]:
    jobs: dict[str, _Job] = {}
    for node, data in g.nodes(data=True):
        if data.get("node_type") != _JOB:
            continue
        attrs = data.get("attributes") or {}
        steps = []
        for succ in g.successors(node):
            sdata = g.nodes[succ]
            if sdata.get("node_type") != _STEP:
                continue
            sa = sdata.get("attributes") or {}
            steps.append({
                "run": sa.get("run") or "", "uses": sa.get("uses") or "", "with": sa.get("with") or {},
                "id": sa.get("id"), "if": sa.get("if"), "wd": sa.get("working_directory"),
                "_order": succ,
            })
        steps.sort(key=lambda s: s["_order"])
        jobs[node.split("::", 1)[1]] = _Job(
            node, steps, attrs.get("conditions") or [], attrs.get("outputs") or {}, attrs.get("with") or {}
        )
    return jobs


def _prov(source: str, confidence: float) -> dict[str, Any]:
    """One provenance dict shared by every edge a rule creates (cheap on 10^5-edge graphs)."""
    return ProvenanceRecord(origin_parser="GraphFusion", creation_source=source, confidence=confidence).model_dump()


def _link(g: nx.MultiDiGraph, source: str, target: str, etype: EdgeType, prov: dict[str, Any]) -> int:
    if g.has_edge(source, target, key=etype.value):
        return 0
    conf = prov["confidence"]
    g.add_edge(source, target, key=etype.value, edge_type=etype.value, source_id=source, target_id=target,
               weight=conf, confidence=conf, provenance=prov)
    return 1


def _link_files(g: nx.MultiDiGraph, files: Iterable[str], target: str, etype: EdgeType, prov: dict[str, Any]) -> int:
    return sum(_link(g, f"{_FILE}::{f}", target, etype, prov) for f in files)


def _resolve(index: PathIndex, cand: str, wd: str) -> tuple[str, Any] | None:
    """``('file'|'dir', path)`` or ``('glob', [files])`` for a command argument, trying it relative
    to the working directory first; None if it names nothing in the tree."""
    for base in ([wd, ""] if wd else [""]):
        path = normalize_path(f"{base}/{cand}" if base else cand)
        if not path:
            continue
        if has_glob(path):
            files = index.glob(path)
            if files:
                return "glob", files
        elif index.is_file(path):
            return "file", path
        elif index.has_dir(path):
            return "dir", path
    return None


def _toolchain_target(g: nx.MultiDiGraph, index: PathIndex, toolchain: str, extent: str, scope: str) -> str:
    """The ``BuildTarget`` node holding every file ``toolchain`` reads under ``scope`` (created once)."""
    node = f"{_TARGET}::{toolchain}:{extent}@{scope or '.'}"
    if node not in g:
        g.add_node(node, node_type=_TARGET, tags=[],
                   attributes={"kind": "toolchain", "toolchain": toolchain, "extent": extent, "scope": scope or "."})
        tc = TOOLCHAINS[toolchain]
        prov = _prov("toolchain_scope", CONF_TOOLCHAIN)
        members = [p for p in index.under(scope)
                   if tc.is_manifest(p) or (extent == "all" and tc.is_source(p))]
        _link_files(g, members, node, EdgeType.BELONGS_TO, prov)
    return node
