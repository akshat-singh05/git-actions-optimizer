"""Graph features are computed on the graph of the repository AT EACH COMMIT.

A real (tiny) git repository is built with two commits; the same changed file yields
different structural features at the two commits because the tree differs — which is
exactly what a static graph could not do.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gha_optimizer.features.graph_features import GraphFeatureComputer, ParseCache
from gha_optimizer.ingestion.mirror import RepoMirror

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

WORKFLOW = """\
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@v4
        with:
          path: ~/.cache/pip
          key: pip-${{ hashFiles('requirements.txt') }}
      - run: pytest tests/unit
  build:
    needs: unit
    runs-on: ubuntu-latest
    steps:
      - run: python -m build
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture()
def two_commit_mirror(tmp_path):
    work = tmp_path / "work"
    (work / ".github" / "workflows").mkdir(parents=True)
    (work / "src").mkdir()
    (work / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
    (work / "src" / "app.py").write_text("import os\n\ndef run():\n    return os.getcwd()\n", encoding="utf-8")
    (work / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "A")
    sha_a = git(work, "rev-parse", "HEAD")
    (work / "tests" / "unit").mkdir(parents=True)
    (work / "tests" / "unit" / "test_app.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "B")
    sha_b = git(work, "rev-parse", "HEAD")

    mirror = RepoMirror("o/r", root=tmp_path / "mirrors")
    mirror.path.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(mirror.path)], check=True, capture_output=True)
    return mirror, sha_a, sha_b


JOBS = ["lint", "unit", "build"]


def test_structural_features_differ_between_commits(two_commit_mirror):
    mirror, sha_a, sha_b = two_commit_mirror
    comp = GraphFeatureComputer(mirror)
    wf_text = mirror.read_file(sha_a, ".github/workflows/ci.yml")[1].decode()
    changed = ["tests/unit/test_app.py"]

    g_a = comp.graph_at(sha_a, wf_text)
    f_a = comp.features(g_a, changed, JOBS)
    # at commit A the test file does not exist: the graph cannot place it -> conservative fallback
    assert {f["any_changed_file_in_graph"] for f in f_a.values()} == {0.0}
    assert {j for j, f in f_a.items() if f["job_structurally_impacted"] == 1.0} == set(JOBS)

    g_b = comp.graph_at(sha_b, wf_text)
    f_b = comp.features(g_b, changed, JOBS)
    # at commit B it exists and `pytest tests/unit` links it to job `unit` (and, via needs, `build`)
    assert {f["any_changed_file_in_graph"] for f in f_b.values()} == {1.0}
    assert {j for j, f in f_b.items() if f["job_structurally_impacted"] == 1.0} == {"unit", "build"}
    assert f_b["lint"]["job_structurally_impacted"] == 0.0
    # distance is over the UNDIRECTED projection: file -> unit is 1 hop; every other job of the same
    # workflow is reachable through the shared Workflow node (file -> unit -> Workflow -> lint = 3)
    assert f_b["unit"]["dep_graph_distance"] == 1.0
    assert f_b["lint"]["dep_graph_distance"] == 3.0


def test_cache_key_touched_is_per_job(two_commit_mirror):
    mirror, _, sha_b = two_commit_mirror
    comp = GraphFeatureComputer(mirror)
    g = comp.graph_at(sha_b, mirror.read_file(sha_b, ".github/workflows/ci.yml")[1].decode())
    f = comp.features(g, ["requirements.txt"], JOBS)
    assert f["unit"]["cache_key_touched"] == 1.0      # unit restores pip-${{ hashFiles('requirements.txt') }}
    assert f["lint"]["cache_key_touched"] == 0.0 and f["build"]["cache_key_touched"] == 0.0


def test_parse_cache_reuses_unchanged_blobs_across_commits(two_commit_mirror, tmp_path):
    mirror, sha_a, sha_b = two_commit_mirror
    comp = GraphFeatureComputer(mirror, cache_path=tmp_path / "cache.sqlite")
    comp.source_graph(sha_a)
    parsed_after_a = comp.stats["files_parsed"]
    comp.source_graph(sha_b)
    assert comp.stats["files_parsed"] == parsed_after_a + 1      # only the new test file was parsed
    assert comp.stats["files_cached"] >= 1                        # src/app.py came from the cache
    comp.close()
    again = ParseCache(tmp_path / "cache.sqlite")                 # persisted on disk
    assert again.get("src/app.py", mirror.blob_sha(sha_b, "src/app.py")) is not None
    again.close()


def test_mirror_reads_objects_without_a_checkout(two_commit_mirror):
    mirror, sha_a, sha_b = two_commit_mirror
    assert mirror.has_commit(sha_a) and mirror.has_commit(sha_b)
    assert mirror.read_file(sha_a, "tests/unit/test_app.py") is None        # absent at A
    assert mirror.read_file(sha_b, "tests/unit/test_app.py") is not None    # present at B
    assert ("src/app.py" in {p for p, _ in mirror.list_tree(sha_b)})
