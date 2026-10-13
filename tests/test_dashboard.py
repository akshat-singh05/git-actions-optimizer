"""Dashboard service, disk cache, progress, API and evidence reader - with a stubbed pipeline (no database, no models).

Nothing here is a result: the stubbed pipeline returns placeholder payloads so that the plumbing can be tested.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gha_optimizer import evidence
from gha_optimizer.dashboard_app import create_dashboard_app
from gha_optimizer.dashboard_service import RESULT_VERSION, STAGES, DashboardService, _suggest_changes
from gha_optimizer.server.api.v1 import dashboard as dashboard_api


class StubService(DashboardService):
    """Pipeline replaced by a stub; the fingerprint is a settable string instead of database queries."""

    def __init__(self, tmp_path, **kw):
        super().__init__(cache_dir=tmp_path / "cache", docs_dir=tmp_path / "docs", **kw)
        self.fp = "fp-1"
        self.runs = 0
        self.fail = False

    def fingerprint(self, repo):
        return self.fp

    def repos(self):
        return [{"repo": "o/r", "workflow": "ci.yml", "runs": 3, "ready": "o/r" in self._results, "declined": False, "state": "idle"}]

    def _run(self, repo, progress):
        self.runs += 1
        for i in range(3):
            progress(i, f"stage {i}")
            time.sleep(0.01)
        if self.fail:
            raise RuntimeError("boom")
        return {"declined": False, "repo": repo, "summary": {"jobs": 1}, "marker": self.runs}, None


def wait(svc, repo):
    for _ in range(400):
        if svc.status(repo)["state"] not in ("queued", "running"):
            return svc.status(repo)
        time.sleep(0.01)
    raise AssertionError("run did not finish")


# ---- progress and disk cache ---------------------------------------------------------------------------------

def test_a_run_reports_its_stages_and_ends_done(tmp_path):
    svc = StubService(tmp_path)
    assert svc.start("o/r")["state"] in ("queued", "running")
    st = wait(svc, "o/r")
    assert st["state"] == "done" and st["stages"] == STAGES and set(st["stage_seconds"]) >= {"0", "1", "2"}
    assert svc.result("o/r")["marker"] == 1


def test_starting_twice_runs_the_pipeline_once(tmp_path):
    svc = StubService(tmp_path)
    svc.start("o/r"); svc.start("o/r")
    wait(svc, "o/r"); svc.start("o/r")
    assert svc.runs == 1


def test_result_survives_a_restart_only_while_the_fingerprint_matches(tmp_path):
    first = StubService(tmp_path)
    first.start("o/r"); wait(first, "o/r")
    again = StubService(tmp_path)                                # a "restart": new object, same cache directory
    st = again.start("o/r")
    assert st["state"] == "done" and st["from_disk_cache"] and again.runs == 0 and again.result("o/r")["marker"] == 1
    changed = StubService(tmp_path)
    changed.fp = "fp-2"                                          # new runs ingested / new tip commit / new library version
    changed.start("o/r"); wait(changed, "o/r")
    assert changed.runs == 1 and not changed.status("o/r")["from_disk_cache"]


def test_a_cache_written_by_another_result_version_is_ignored(tmp_path):
    svc = StubService(tmp_path)
    svc.start("o/r"); wait(svc, "o/r")
    path = svc._cache_path("o/r")
    path.write_text(path.read_text().replace(f'"version": {RESULT_VERSION}', '"version": -1'), encoding="utf-8")
    other = StubService(tmp_path)
    other.start("o/r"); wait(other, "o/r")
    assert other.runs == 1


def test_a_corrupt_cache_file_is_ignored_not_trusted(tmp_path):
    svc = StubService(tmp_path)
    svc.cache_dir.mkdir(parents=True)
    svc._cache_path("o/r").write_text("{not json", encoding="utf-8")
    svc.start("o/r"); wait(svc, "o/r")
    assert svc.runs == 1 and svc.result("o/r")["marker"] == 1


def test_a_failing_run_is_reported_as_an_error_with_its_message(tmp_path):
    svc = StubService(tmp_path)
    svc.fail = True
    svc.start("o/r")
    st = wait(svc, "o/r")
    assert st["state"] == "error" and "boom" in st["error"] and svc.result("o/r") is None
    svc.fail = False
    svc.start("o/r", force=True); assert wait(svc, "o/r")["state"] == "done"


def test_what_if_without_a_context_says_so(tmp_path):
    svc = StubService(tmp_path)
    with pytest.raises(KeyError):
        svc.what_if("o/r", ["a.py"])
    assert svc.files("o/r")["available"] is False


# ---- scenarios -----------------------------------------------------------------------------------------------

def test_scenarios_are_built_only_from_files_that_exist():
    tree = ["README.md", "docs/guide.md", "poetry.lock", "src/pkg/core.py", "tests/test_core.py", ".github/workflows/ci.yml", "LICENSE"]
    got = {s["label"]: s["files"] for s in _suggest_changes(tree)}
    assert got["Docs only"] == ["README.md"] and got["A dependency file"] == ["poetry.lock"]
    assert got["CI configuration"] == [".github/workflows/ci.yml"] and got["A test file"] == ["tests/test_core.py"]
    assert got["A source file"] == ["src/pkg/core.py"]
    assert all(f in tree for s in _suggest_changes(tree) for f in s["files"])
    assert _suggest_changes(["LICENSE"]) == []                   # nothing to suggest rather than an invented file


# ---- evidence reader -----------------------------------------------------------------------------------------

REPORT = """# Report

## Model vs baseline

| repo | positives | PR-AUC |
|---|---|---|
| o/r | 10 | 0.5 |
| o/other | 20 | 0.4 |

## Another

| thing | value |
|---|---|
| x | 1 |

| repo | n |
|---|---|
| **o/r** | 3 |
"""


def test_tables_are_parsed_with_their_headings_and_filtered_by_repository():
    tables = evidence.parse_tables(REPORT)
    assert [t.heading for t in tables] == ["Model vs baseline", "Another", "Another"]
    mine = evidence.rows_for_repo(REPORT, "o/r")
    assert [t.rows for t in mine] == [[["o/r", "10", "0.5"]], [["**o/r**", "3"]]]       # o/other is not o/r
    assert evidence.rows_for_repo(REPORT, "o/missing") == []


def test_only_plain_report_names_can_be_read(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "MODEL_EVAL.md").write_text(REPORT, encoding="utf-8")
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    assert evidence.read_doc("MODEL_EVAL.md", docs) == REPORT
    for bad in ("../secret.md", "..\\secret.md", "sub/../MODEL_EVAL.md", "MODEL_EVAL.txt", "", "a b.md"):
        assert evidence.read_doc(bad, docs) is None
    assert evidence.list_docs(docs) == ["MODEL_EVAL.md"]
    found = evidence.evidence_for_repo("o/r", docs)
    assert [f["report"] for f in found] == ["MODEL_EVAL.md"] and found[0]["tables"][0]["rows"] == [["o/r", "10", "0.5"]]


# ---- API ------------------------------------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODEL_EVAL.md").write_text(REPORT, encoding="utf-8")
    svc = StubService(tmp_path, default_repo="o/r")
    dashboard_api.configure(svc)
    yield TestClient(create_dashboard_app(svc)), svc
    dashboard_api._service = None


def test_api_run_status_result_flow(client):
    c, svc = client
    assert c.get("/api/v1/dashboard/repos").json()["default"] == "o/r"
    assert c.get("/api/v1/dashboard/result", params={"repo": "o/r"}).status_code == 404       # nothing computed yet
    assert c.post("/api/v1/dashboard/run", params={"repo": "o/r"}).json()["state"] in ("queued", "running", "done")
    wait(svc, "o/r")
    assert c.get("/api/v1/dashboard/status", params={"repo": "o/r"}).json()["state"] == "done"
    assert c.get("/api/v1/dashboard/result", params={"repo": "o/r"}).json()["summary"] == {"jobs": 1}


def test_api_whatif_errors_are_explicit(client):
    c, _ = client
    r = c.post("/api/v1/dashboard/whatif", json={"repo": "o/r", "changed_files": ["a.py"]})
    assert r.status_code == 409 and "run the repository first" in r.json()["detail"]


def test_api_serves_reports_but_not_arbitrary_files(client):
    c, _ = client
    assert c.get("/api/v1/dashboard/docs").json() == {"docs": ["MODEL_EVAL.md"]}
    assert c.get("/api/v1/dashboard/docs/MODEL_EVAL.md").text.startswith("# Report")
    assert c.get("/api/v1/dashboard/docs/secret.md").status_code == 404
    assert c.get("/api/v1/dashboard/docs/..%2Fsecret.md").status_code == 404
    ev = c.get("/api/v1/dashboard/evidence", params={"repo": "o/r"}).json()
    assert ev["reports"][0]["report"] == "MODEL_EVAL.md"


def test_the_one_ui_is_served_and_contains_no_embedded_data(client):
    c, _ = client
    html = c.get("/").text
    assert "GitHub Actions Performance Optimizer" in html and "/api/v1/dashboard" in html
    ui = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
    assert ui.is_file() and not (ui.parent / "app.js").exists()                       # the old second UI is gone
    assert not (ui.parent.parent / "demo_ui.html").exists()
