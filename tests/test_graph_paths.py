"""File node ids must use '/' on every OS: GitHub reports changed files that way.

Regression for a Windows bug: parsers produced ``File::pkg\\b.py`` so
``impacted_jobs_for_diff`` never found a changed file and silently returned
*all* jobs (the conservative fallback), making every graph feature degenerate.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from gha_optimizer.graph.dependency_graph import build_dependency_graph, impacted_jobs_for_diff
from gha_optimizer.ingestion.workflow_parser import parse_workflow


def _repo(tmp_path: Path):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "sub" / "mod.py").write_text("import os\n\ndef f():\n    return os.getcwd()\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(yaml.dump({"name": "CI", "on": ["push"], "jobs": {
        "test": {"runs-on": "ubuntu-latest", "steps": [{"run": "pytest pkg/"}]},
        "lint": {"runs-on": "ubuntu-latest", "steps": [{"run": "ruff check ."}]},
    }}), encoding="utf-8")
    return wf


def test_file_nodes_use_forward_slashes(tmp_path):
    wf = _repo(tmp_path)
    g = build_dependency_graph(parse_workflow(wf), tmp_path, ["pkg/sub/mod.py", "main.go"])
    files = {n for n in g if n.startswith("File::")}
    assert files == {"File::pkg/sub/mod.py", "File::main.go"}
    assert not any("\\" in n for n in g)


def test_a_changed_file_is_found_in_the_graph_by_its_github_path(tmp_path):
    wf = _repo(tmp_path)
    g = build_dependency_graph(parse_workflow(wf), tmp_path, ["pkg/sub/mod.py"])
    # the test-command edge pytest pkg/ -> job 'test' only exists if the lookup finds the node
    assert "test" in impacted_jobs_for_diff(g, ["pkg/sub/mod.py"])
    assert "File::pkg/sub/mod.py" in g
