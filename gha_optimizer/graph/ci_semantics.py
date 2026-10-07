"""
Static CI semantics used by graph fusion: what a workflow step *reads*.

Pure functions, no graph or I/O.  Everything here is a documented fact about a tool
or an action (not a learned or tuned value) and every rule is deliberately coarse and
labelled as such by the confidence the fusion layer attaches to it:

* ``glob_to_regex`` / ``PathIndex`` - GitHub-style glob matching over a repository's file list.
* ``classify_segment`` - which language toolchain a shell command drives, and how
  (``test``/``build`` read the whole source set, ``check`` reads only the paths it is given,
  ``install`` reads only dependency manifests).
* ``path_candidates`` - path-like arguments of a command (``pytest tests/unit``, ``-pl a/b``).
* ``hash_globs_from_key`` / ``implicit_cache`` - which files a cache key is a hash of, for
  explicit ``actions/cache`` keys and for the built-in caches of the ``setup-*`` actions.
* ``parse_paths_filter`` - the file groups declared to ``dorny/paths-filter``.
"""
from __future__ import annotations

import bisect
import re
import shlex
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, Iterable

import yaml

# ---------------------------------------------------------------------------
# Globs and the repository path index
# ---------------------------------------------------------------------------

_GLOB_CHARS = "*?[{"


def has_glob(pattern: str) -> bool:
    return any(c in pattern for c in _GLOB_CHARS)


def normalize_path(p: str) -> str:
    """``./a//b/`` -> ``a/b``; ``.`` -> ``''``."""
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p).rstrip("/")
    return "" if p == "." else p


@lru_cache(maxsize=4096)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """picomatch/``hashFiles`` flavour: ``**`` crosses directories (``**/`` may match zero
    directories), ``*`` and ``?`` stay inside one path segment, ``{a,b}`` alternates."""
    pat = normalize_path(pattern)
    out: list[str] = []
    i, depth = 0, 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        elif pat[i] == "{":
            out.append("(?:")
            depth += 1
            i += 1
        elif pat[i] == "}" and depth:
            out.append(")")
            depth -= 1
            i += 1
        elif pat[i] == "," and depth:
            out.append("|")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    out.append(")" * depth)
    return re.compile("^" + "".join(out) + "$")


def glob_matches(pattern: str, path: str) -> bool:
    return bool(glob_to_regex(pattern).match(normalize_path(path)))


class PathIndex:
    """Sorted file list with directory-prefix and glob lookups (no per-query full scan for
    the common ``dir/**`` shapes)."""

    def __init__(self, paths: Iterable[str]):
        self.paths: list[str] = sorted(set(paths))
        self._set = set(self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    def is_file(self, p: str) -> bool:
        return p in self._set

    def under(self, prefix: str) -> list[str]:
        prefix = normalize_path(prefix)
        if not prefix:
            return list(self.paths)
        lo = bisect.bisect_left(self.paths, prefix + "/")
        out = []
        for i in range(lo, len(self.paths)):
            if not self.paths[i].startswith(prefix + "/"):
                break
            out.append(self.paths[i])
        return out

    def has_dir(self, prefix: str) -> bool:
        prefix = normalize_path(prefix)
        if not prefix:
            return bool(self.paths)
        i = bisect.bisect_left(self.paths, prefix + "/")
        return i < len(self.paths) and self.paths[i].startswith(prefix + "/")

    def glob(self, pattern: str) -> list[str]:
        pattern = normalize_path(pattern)
        if not has_glob(pattern):
            if pattern in self._set:
                return [pattern]
            return self.under(pattern)  # a bare directory pattern means "everything below it"
        literal = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
        base = literal.rsplit("/", 1)[0] if "/" in literal else ""
        rx = glob_to_regex(pattern)
        return [p for p in self.under(base) if rx.match(p)]


# ---------------------------------------------------------------------------
# Toolchains
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Toolchain:
    name: str
    source_exts: frozenset[str]
    manifest_names: frozenset[str]
    manifest_globs: tuple[str, ...] = ()
    source_dirs: tuple[str, ...] = ()  # path segments whose whole subtree counts as source (maven layout)

    def is_manifest(self, path: str) -> bool:
        name = PurePosixPath(path).name
        return name in self.manifest_names or any(glob_matches(g, name) for g in self.manifest_globs)

    def is_source(self, path: str) -> bool:
        if PurePosixPath(path).suffix in self.source_exts:
            return True
        padded = "/" + path
        return any(f"/{d}/" in padded for d in self.source_dirs)


TOOLCHAINS: dict[str, Toolchain] = {
    "python": Toolchain(
        "python", frozenset({".py", ".pyi", ".pyx"}),
        frozenset({"pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py", "poetry.lock", "uv.lock",
                   "Pipfile", "Pipfile.lock", "pytest.ini", "conftest.py"}),
        ("requirements*.txt", "constraints*.txt"),
    ),
    "go": Toolchain("go", frozenset({".go"}), frozenset({"go.mod", "go.sum", "go.work", "go.work.sum"})),
    "java": Toolchain(
        "java", frozenset({".java", ".kt", ".groovy", ".scala", ".gradle", ".kts"}),
        frozenset({"pom.xml", "gradle.properties", "gradle-wrapper.properties", "maven-wrapper.properties",
                   "extensions.xml", "mvnw", "gradlew"}),
        source_dirs=("src/main", "src/test"),
    ),
    "js": Toolchain(
        "js", frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".css", ".scss", ".html"}),
        frozenset({"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml",
                   "tsconfig.json"}),
    ),
}

# ``setup-*`` actions imply the job drives that language (weak evidence; see fusion)
SETUP_ACTION_TOOLCHAIN = {
    "actions/setup-go": "go",
    "actions/setup-python": "python",
    "astral-sh/setup-uv": "python",
    "actions/setup-java": "java",
    "actions/setup-node": "js",
    "pnpm/action-setup": "js",
}

# kind: test/build read the toolchain's whole source set under the working directory;
# check reads the paths it is given (whole source set only when it is given none);
# install reads dependency manifests only.
_RULES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(p), tc, kind) for p, tc, kind in [
        (r"\bpytest\b|-m\s+pytest\b|\b(?:tox|nox)\b|\bcoverage\s+run\b", "python", "test"),
        (r"\b(?:ruff|mypy|flake8|black|isort|pylint|pyright|bandit|pycodestyle|codespell)\b", "python", "check"),
        (r"\bpoetry\s+(?:run|build|check|publish|export)\b|\buv\s+(?:run|build)\b|-m\s+build\b", "python", "build"),
        (r"\bpoetry\s+(?:install|lock|sync)\b|\buv\s+(?:sync|lock|pip|add)\b|\bpip3?\s+install\b|-m\s+pip\s+install\b",
         "python", "install"),
        (r"\bgo\s+test\b|\bgotestsum\b", "go", "test"),
        (r"\bgo\s+(?:build|install|generate|run)\b", "go", "build"),
        (r"\bgo\s+vet\b|\bgolangci-lint\b|\bstaticcheck\b|\bgofmt\b|\bgoimports\b", "go", "check"),
        (r"\bgo\s+(?:mod\s+(?:download|tidy|verify|vendor)|get)\b", "go", "install"),
        (r"\bgradlew?\b", "java", "build"),
        (r"(?:^|[\s/])mvnw?\b", "java", "build"),
        (r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test(?::[\w:-]+)?\b|\b(?:jest|vitest|mocha|cypress|playwright)\b",
         "js", "test"),
        (r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:build|start|dev|compile)\b|\b(?:webpack|vite|rollup|esbuild|parcel)\b",
         "js", "build"),
        (r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:lint|format|typecheck|check)\b|\b(?:eslint|tsc|prettier|stylelint)\b",
         "js", "check"),
        (r"\b(?:npm|pnpm|yarn)\s+(?:ci|install|i|add)\b", "js", "install"),
    ]
]
_MVN_INSTALL_ONLY = re.compile(r"\b(?:dependency|help):[\w-]+")
_MVN_LIFECYCLE = re.compile(r"\b(?:test|verify|install|package|compile|clean|surefire|failsafe|deploy)\b")

_SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def command_segments(run: str) -> list[str]:
    """Split a ``run:`` script into simple commands (continuations joined; no heredoc handling)."""
    text = re.sub(r"\\\r?\n", " ", run or "")
    segments = (re.sub(r"\s+", " ", seg).strip() for seg in _SEG_SPLIT.split(text))
    return [seg for seg in segments if seg and not seg.startswith("#")]


def classify_segment(segment: str) -> set[tuple[str, str]]:
    """``{(toolchain, kind)}`` driven by one command; empty if no known tool."""
    found: set[tuple[str, str]] = set()
    for rx, tc, kind in _RULES:
        if rx.search(segment):
            found.add((tc, kind))
    if ("java", "build") in found and _MVN_INSTALL_ONLY.search(segment) and not _MVN_LIFECYCLE.search(segment):
        found.discard(("java", "build"))
        found.add(("java", "install"))
    return found


# ---------------------------------------------------------------------------
# Path-like arguments of a command
# ---------------------------------------------------------------------------

# bare words that name a directory in some repos but are almost always a sub-command here
_SUBCOMMANDS = frozenset({
    "test", "tests-all", "build", "install", "run", "check", "lint", "clean", "verify", "package", "format", "fmt",
    "vet", "generate", "all", "ci", "compile", "deploy", "start", "dev", "docs", "doc", "release", "publish", "help",
    "version", "init", "update", "sync", "lock", "export", "ui", "api", "web",
})


def path_candidates(segment: str) -> list[str]:
    """Normalised path-like tokens of a command (relative to repo root or working directory).

    A token qualifies if it contains ``/`` or looks like ``name.ext`` (explicit path), or is a bare
    word that is not a common sub-command (callers still require it to exist in the tree).
    Variable / expression / URL tokens, options and absolute paths are dropped.
    """
    if re.match(r"\s*(?:echo|printf)\b", segment):
        return []
    segment = re.sub(r"\$\{\{.*?\}\}", "$EXPR", segment)  # an expression is not a path (and shlex would split it)
    try:
        toks = shlex.split(segment, posix=True)
    except ValueError:
        toks = segment.split()
    out: list[str] = []
    for i, tok in enumerate(toks):
        if tok.startswith("-") and "=" in tok:
            tok = tok.split("=", 1)[1]
        elif tok.startswith("-"):
            continue
        for piece in tok.split(","):
            if (not piece or "$" in piece or "{{" in piece or "://" in piece or "@" in piece
                    or piece.startswith(("/", "~", "!"))):
                continue
            piece = re.sub(r"/\.\.\.$", "", piece)
            piece = normalize_path(piece)
            if not piece or piece in {"..", "..."} or ".." in piece.split("/"):
                continue
            explicit = "/" in piece or re.search(r"\.\w+$", piece)
            if i == 0 and not explicit:
                continue  # the command word itself
            if not explicit and piece.lower() in _SUBCOMMANDS:
                continue
            out.append(piece)
    return out


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_HASHFILES = re.compile(r"hashFiles\(([^)]*)\)")
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def hash_globs_from_key(key: str) -> list[str]:
    """All glob arguments of every ``hashFiles(...)`` call in a cache key expression."""
    globs: list[str] = []
    for call in _HASHFILES.findall(key or ""):
        for a, b in _QUOTED.findall(call):
            for line in (a or b).splitlines():
                line = line.strip()
                if line and not line.startswith("!"):
                    globs.append(line)
    return globs


# action -> (cache-enabling rule, default dependency globs by manager).  Defaults are the documented
# ones of each action (setup-uv action.yml, setup-java docs, setup-python/-node README, setup-go README).
_SETUP_UV_GLOBS = [
    "**/*requirements*.txt", "**/*requirements*.in", "**/*constraints*.txt", "**/*constraints*.in",
    "**/pyproject.toml", "**/uv.lock", "**/*.py.lock",
]
_PYTHON_GLOBS = {"pip": ["**/requirements.txt"], "pipenv": ["**/Pipfile.lock"], "poetry": ["**/poetry.lock"]}
_NODE_GLOBS = {"npm": ["**/package-lock.json"], "yarn": ["**/yarn.lock"], "pnpm": ["**/pnpm-lock.yaml"]}
_JAVA_GLOBS = {"maven": ["**/pom.xml", "**/.mvn/wrapper/maven-wrapper.properties", "**/.mvn/extensions.xml"]}


def _paths_of(value: Any) -> list[str]:
    return [p.strip() for p in str(value).splitlines() if p.strip()] if value else []


def implicit_cache(uses: str, with_: dict[str, Any] | None) -> dict[str, Any] | None:
    """Built-in cache of a ``setup-*`` action: ``{"label", "action", "hash_globs", "resolved"}``.

    ``resolved`` False means the action does cache but which files feed its key is not documented
    here (gradle, sbt, ...) so the cache must be treated as *unknown*, never as "untouched".
    ``None`` when the step does not cache."""
    action = uses.split("@", 1)[0]
    w = with_ or {}
    override = _paths_of(w.get("cache-dependency-path"))
    if action == "actions/setup-go":
        if str(w.get("cache", "true")).lower() == "false":
            return None
        return {"label": "setup-go", "action": action, "hash_globs": override or ["go.sum"], "resolved": True}
    if action == "actions/setup-python" and w.get("cache"):
        mgr = str(w["cache"])
        globs = override or _PYTHON_GLOBS.get(mgr, [])
        return {"label": f"setup-python:{mgr}", "action": action, "hash_globs": globs, "resolved": bool(globs)}
    if action == "actions/setup-node" and w.get("cache"):
        mgr = str(w["cache"])
        globs = override or _NODE_GLOBS.get(mgr, [])
        return {"label": f"setup-node:{mgr}", "action": action, "hash_globs": globs, "resolved": bool(globs)}
    if action == "actions/setup-java" and w.get("cache"):
        mgr = str(w["cache"])
        globs = override or _JAVA_GLOBS.get(mgr, [])
        return {"label": f"setup-java:{mgr}", "action": action, "hash_globs": globs, "resolved": bool(globs)}
    if action == "astral-sh/setup-uv":
        if str(w.get("enable-cache", "auto")).lower() == "false":
            return None
        globs = _paths_of(w.get("cache-dependency-glob")) or _SETUP_UV_GLOBS
        return {"label": "setup-uv", "action": action, "hash_globs": globs, "resolved": True}
    if action == "golangci/golangci-lint-action":
        return {"label": "golangci-lint", "action": action, "hash_globs": [], "resolved": False}
    return None


# ---------------------------------------------------------------------------
# dorny/paths-filter
# ---------------------------------------------------------------------------

def _flatten_patterns(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):  # `- added|modified: 'x/**'`
        return [p for v in value.values() for p in _flatten_patterns(v)]
    if isinstance(value, list):
        return [p for v in value for p in _flatten_patterns(v)]
    return []


def parse_paths_filter(filters: Any) -> dict[str, list[str]]:
    """``{filter name: [globs]}`` from the (inline YAML) ``filters`` input; YAML anchors are
    resolved by the parser.  A filter file path (not inline) yields ``{}``."""
    if isinstance(filters, str):
        if "\n" not in filters and ":" not in filters:
            return {}
        try:
            filters = yaml.safe_load(filters)
        except yaml.YAMLError:
            return {}
    if not isinstance(filters, dict):
        return {}
    return {str(k): [p for p in _flatten_patterns(v) if isinstance(p, str)] for k, v in filters.items()}


_NEEDS_OUTPUT = re.compile(r"needs\.([\w-]+)\.outputs\.([\w-]+)")
_STEP_OUTPUT = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")


def needs_output_refs(text: str) -> set[tuple[str, str]]:
    """``{(job key, output name)}`` referenced as ``needs.<job>.outputs.<name>`` in an expression."""
    return set(_NEEDS_OUTPUT.findall(text or ""))


def step_output_refs(text: str) -> list[tuple[str, str]]:
    return _STEP_OUTPUT.findall(text or "")
