"""Mirroring private repositories: the token must reach git, but only through its environment."""
from __future__ import annotations

import base64
import types
import subprocess

import pytest

from gha_optimizer.ingestion.mirror import RepoMirror, git_env


def test_git_env_without_a_token_adds_no_credentials():
    env = git_env(None)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in env or "extraheader" not in env.get("GIT_CONFIG_KEY_0", "")


def test_git_env_with_a_token_sets_a_github_scoped_basic_auth_header():
    env = git_env("tok-123")
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    scheme, cred = env["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: ").split(" ", 1)
    assert scheme == "basic" and base64.b64decode(cred).decode() == "x-access-token:tok-123"


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append({"args": list(args), "env": kwargs.get("env")})
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_clone_authenticates_through_the_environment_not_the_url_or_arguments(captured, tmp_path):
    RepoMirror("o/private", root=tmp_path, token="tok-123").ensure()
    clone = captured[0]
    assert clone["args"][:3] == ["git", "clone", "--bare"]
    assert "tok-123" not in " ".join(clone["args"])                      # not in the URL, not on the command line
    assert clone["args"][-2] == "https://github.com/o/private.git"
    assert "AUTHORIZATION: basic" in clone["env"]["GIT_CONFIG_VALUE_0"]


def test_clone_without_a_token_stays_anonymous(captured, tmp_path):
    RepoMirror("o/public", root=tmp_path).ensure()
    assert "extraheader" not in captured[0]["env"].get("GIT_CONFIG_KEY_0", "")


def test_fetches_of_an_existing_mirror_use_the_same_credentials(captured, tmp_path):
    m = RepoMirror("o/private", root=tmp_path, token="tok-123")
    m.path.mkdir(parents=True)
    (m.path / "HEAD").write_text("ref: refs/heads/main\n")
    m.ensure(refresh=True)
    assert captured and all("AUTHORIZATION: basic" in c["env"]["GIT_CONFIG_VALUE_0"] for c in captured)


def test_ingestor_hands_the_client_token_to_its_mirror(monkeypatch):
    """The ingestion service builds the mirror with the same token it uses for the REST API."""
    import gha_optimizer.ingestion.ingest as ingest_module

    seen = {}

    class FakeMirror:
        def __init__(self, repo, root=None, token=None, **kw):
            seen["token"] = token

        def ensure(self, refresh=True):
            raise RuntimeError("stop after construction")

    monkeypatch.setattr(ingest_module, "RepoMirror", FakeMirror)
    ing = ingest_module.RepoIngestor.__new__(ingest_module.RepoIngestor)
    ing.repo, ing.mirror = "o/r", None

    class Client:
        token = "tok-abc"

        def rate_limit_status(self):
            return {}

        def get_repo(self):
            return {}

        def list_workflows(self):
            return []

    class Opts:
        use_mirror, mirror_root, mirror_refresh = True, "root", False

    class Q:
        def filter_by(self, **kw):
            return self

        def one_or_none(self):
            return types.SimpleNamespace()

    class DB:
        def query(self, *a):
            return Q()

        def add(self, *a):
            pass

        def commit(self):
            pass

    ing.client, ing.opts, ing.db = Client(), Opts(), DB()
    with pytest.raises(RuntimeError, match="stop after construction"):
        ing._setup()
    assert seen["token"] == "tok-abc"
