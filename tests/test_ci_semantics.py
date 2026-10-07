"""Pure helpers behind graph fusion: globs, command classification, path arguments, cache inputs."""
from __future__ import annotations

from gha_optimizer.graph.ci_semantics import (
    PathIndex,
    classify_segment,
    command_segments,
    glob_matches,
    hash_globs_from_key,
    implicit_cache,
    needs_output_refs,
    parse_paths_filter,
    path_candidates,
)


def test_glob_double_star_matches_zero_or_more_directories():
    assert glob_matches("**/pom.xml", "pom.xml")
    assert glob_matches("**/pom.xml", "a/b/pom.xml")
    assert glob_matches("src/**/*.py", "src/x.py") and glob_matches("src/**/*.py", "src/a/b/x.py")
    assert not glob_matches("src/**/*.py", "tests/x.py")


def test_glob_single_star_stays_in_one_segment():
    assert glob_matches("*.md", "README.md")
    assert not glob_matches("*.md", "docs/README.md")
    assert glob_matches("a/{b,c}/x.txt", "a/c/x.txt") and not glob_matches("a/{b,c}/x.txt", "a/d/x.txt")
    assert glob_matches("./docs/**", "docs/a/b.md")


def test_path_index_directory_and_glob_lookup():
    idx = PathIndex(["src/a.py", "src/sub/b.py", "srcx/c.py", "tests/t.py", "pom.xml", "m/pom.xml"])
    assert idx.under("src") == ["src/a.py", "src/sub/b.py"]          # 'srcx' is not under 'src'
    assert idx.has_dir("src/sub") and not idx.has_dir("sr")
    assert idx.glob("**/pom.xml") == ["m/pom.xml", "pom.xml"]
    assert idx.glob("tests") == ["tests/t.py"]                        # a bare directory means everything below it
    assert idx.is_file("pom.xml") and not idx.is_file("src")


def test_command_segments_split_and_join_continuations():
    segs = command_segments("pip install -r r.txt && pytest \\\n  tests/unit ; echo done\n# note\nruff check .")
    assert segs == ["pip install -r r.txt", "pytest tests/unit", "echo done", "ruff check ."]


def test_classify_segment_kinds():
    assert ("python", "test") in classify_segment("poetry run pytest -x tests")
    assert ("python", "check") in classify_segment("ruff check src")
    assert ("python", "install") in classify_segment("pip install -r requirements.txt")
    assert ("go", "test") in classify_segment("go test ./... -race")
    assert ("js", "install") in classify_segment("npm ci")
    assert ("js", "test") in classify_segment("pnpm run test:unit")
    assert classify_segment("echo hello") == set()


def test_maven_dependency_goals_only_read_manifests():
    assert classify_segment("./mvnw -q dependency:go-offline") == {("java", "install")}
    assert ("java", "build") in classify_segment("./mvnw clean install -pl integration-tests/foo")


def test_path_candidates_keep_paths_drop_options_variables_and_subcommands():
    assert "tests/unit" in path_candidates("pytest tests/unit -k foo")
    assert path_candidates("go test ./...") == []                     # `./...` means the whole module, not a path
    # the command word (even `./mvnw`) is not an argument; the comma-separated -pl module list is split
    assert path_candidates("./mvnw -pl integration-tests/a,docs/b test") == ["integration-tests/a", "docs/b"]
    assert path_candidates("echo tests/unit") == []
    assert path_candidates("make test") == []                         # `test` is a sub-command, not a directory
    assert path_candidates("cat $GITHUB_WORKSPACE/x.txt ${{ matrix.f }} https://a/b ~/x /abs/p") == []
    assert path_candidates("./scripts/run.sh --config=conf/app.yml") == ["scripts/run.sh", "conf/app.yml"]


def test_hash_globs_from_key_reads_every_argument():
    key = "mypy-${{ runner.os }}-${{ hashFiles('pyproject.toml', 'poetry.lock') }}-${{ hashFiles(\"**/pom.xml\") }}"
    assert hash_globs_from_key(key) == ["pyproject.toml", "poetry.lock", "**/pom.xml"]
    assert hash_globs_from_key("static-key") == []


def test_implicit_cache_follows_each_action_documented_default():
    assert implicit_cache("actions/setup-go@v5", {})["hash_globs"] == ["go.sum"]          # caches by default
    assert implicit_cache("actions/setup-go@v5", {"cache": False}) is None
    assert implicit_cache("actions/setup-python@v5", {}) is None                             # opt-in
    assert implicit_cache("actions/setup-python@v5", {"cache": "poetry"})["hash_globs"] == ["**/poetry.lock"]
    assert "**/pom.xml" in implicit_cache("actions/setup-java@v4", {"cache": "maven"})["hash_globs"]
    gradle = implicit_cache("actions/setup-java@v4", {"cache": "gradle"})
    assert gradle["resolved"] is False                                                       # key inputs not documented here
    assert "**/uv.lock" in implicit_cache("astral-sh/setup-uv@v7", {})["hash_globs"]
    assert implicit_cache("astral-sh/setup-uv@v7", {"enable-cache": "false"}) is None
    assert implicit_cache("actions/setup-go@v5", {"cache-dependency-path": "a/go.sum\nb/go.sum"})["hash_globs"] == [
        "a/go.sum", "b/go.sum"]
    assert implicit_cache("actions/checkout@v4", {}) is None


def test_parse_paths_filter_resolves_yaml_anchors():
    text = "workflow: &wf\n  - '.github/**'\nproject:\n  - *wf\n  - 'pyproject.toml'\nsrc:\n  - 'src/**/*.py'\n"
    parsed = parse_paths_filter(text)
    assert parsed["project"] == [".github/**", "pyproject.toml"]
    assert parsed["src"] == ["src/**/*.py"]
    assert parse_paths_filter(".github/filters.yml") == {}            # a file reference, not inline


def test_needs_output_refs():
    assert needs_output_refs("needs.changes.outputs.src == 'true' || needs.a.outputs.b-c") == {
        ("changes", "src"), ("a", "b-c")}
