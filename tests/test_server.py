"""HTTP behaviour of the API: health/readiness, refusal on cold start, and the optimize round trip.

The success-path test feeds the explicitly SYNTHETIC fixture history through the real endpoint: it checks the
wiring and the honesty of the response (ESTIMATED labels, no runner relabelling by default), never a result.
The same endpoint was also exercised by hand against real ingested poetry history.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gha_optimizer.server import app as app_module
from gha_optimizer.server.db import models  # noqa: F401  (registers the tables)
from gha_optimizer.server.db import session as session_module
from gha_optimizer.server.db.session import Base, get_db
from tests.fixtures.synthetic_history import generate_synthetic_test_history

WORKFLOW = Path(__file__).resolve().parent / "fixtures" / "sample_workflow.yml"
ALLOWED_TOP_LEVEL = {"name", "run-name", "on", "permissions", "env", "defaults", "concurrency", "jobs"}


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app_module.app.dependency_overrides[get_db] = override
    monkeypatch.setattr(session_module, "SessionLocal", Session)   # health / ready use SessionLocal directly
    yield TestClient(app_module.app)
    app_module.app.dependency_overrides.clear()


def test_health_reports_a_working_database(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy" and body["db_connected"] is True


def test_health_and_ready_notice_a_broken_database(client, monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(session_module, "SessionLocal", broken)
    assert client.get("/health").json()["db_connected"] is False
    assert client.get("/ready").status_code == 503


def test_ready_when_the_database_answers(client):
    assert client.get("/ready").json() == {"status": "ready"}


def test_missing_workflow_file_is_404(client):
    r = client.post("/api/v1/optimize", json={"repo": "o/r", "workflow_path": "does/not/exist.yml"})
    assert r.status_code == 404


def test_cold_start_is_refused_with_a_hint_not_answered_with_a_guess(client):
    r = client.post("/api/v1/optimize", json={"repo": "never/ingested", "workflow_path": str(WORKFLOW)})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "insufficient_history" and "ingest" in detail["hint"]


def _with_history(monkeypatch):
    """SYNTHETIC records, tagged with the workflow path the endpoint derives (it filters history by that path)."""
    import gha_optimizer.ingestion.history as history_module
    from gha_optimizer.ingestion.workspace import workflow_repo_path

    wf = workflow_repo_path(WORKFLOW, WORKFLOW.parent.parent.parent)

    def fake_load(*args, **kwargs):
        recs = generate_synthetic_test_history(n_runs=50)
        for r in recs:
            r.workflow_path = wf
        return recs

    monkeypatch.setattr(history_module, "load_history", fake_load)


def test_optimize_round_trip_is_labelled_estimated_and_changes_no_runner_by_default(client, monkeypatch):
    _with_history(monkeypatch)
    r = client.post("/api/v1/optimize", json={"repo": "o/r", "workflow_path": str(WORKFLOW),
                                              "changed_files": ["src/api/routes.py"]})
    assert r.status_code == 200, r.text
    body = r.json()
    summary = body["plan_summary"]
    assert summary["basis"].startswith("ESTIMATED") and summary["runner_upsizing"] is False
    assert "ASSUMED" in summary["assumed_whatif_larger_runners"]["note"]
    assert "ESTIMATED" in body["explanation"]
    doc = yaml.safe_load(body["optimized_yaml"])
    assert {("on" if k is True else k) for k in doc} <= ALLOWED_TOP_LEVEL
    assert {j["runs-on"] for j in doc["jobs"].values()} == {"ubuntu-latest"}


def test_runner_upsizing_is_opt_in_through_the_api(client, monkeypatch):
    _with_history(monkeypatch)
    r = client.post("/api/v1/optimize", json={"repo": "o/r", "workflow_path": str(WORKFLOW),
                                              "allow_runner_upsizing": True, "speedup_exponent": 0.9})
    assert r.status_code == 200, r.text
    summary = r.json()["plan_summary"]
    assert summary["runner_upsizing"] is True and summary["assumed_whatif_larger_runners"]["speedup_exponent"] == 0.9


def test_skip_recommendations_are_opt_in_through_the_api(client, monkeypatch):
    _with_history(monkeypatch)
    off = client.post("/api/v1/optimize", json={"repo": "o/r", "workflow_path": str(WORKFLOW)})
    assert off.status_code == 200 and off.json()["plan_summary"]["skip_recommendations_applied"] is False
    on = client.post("/api/v1/optimize", json={"repo": "o/r", "workflow_path": str(WORKFLOW),
                                               "allow_skip_recommendations": True})
    assert on.status_code == 200 and on.json()["plan_summary"]["skip_recommendations_applied"] is True


# ---- webhook: HMAC validation and dispatch -------------------------------------------------------------

def _signed(secret: str, body: bytes) -> dict:
    import hashlib
    import hmac

    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": "workflow_job", "Content-Type": "application/json"}


class _FakeTask:
    id = "task-123"


def _capture_dispatch(monkeypatch):
    import gha_optimizer.worker.tasks as tasks_module

    sent = []
    monkeypatch.setattr(tasks_module.process_webhook, "delay", lambda event, payload: sent.append((event, payload)) or _FakeTask())
    return sent


def test_webhook_rejects_a_missing_or_wrong_signature_when_a_secret_is_configured(client, monkeypatch):
    monkeypatch.setattr(app_module.config.server, "webhook_secret", "s3cret")
    sent = _capture_dispatch(monkeypatch)
    body = b'{"action": "completed"}'
    assert client.post("/webhook/github", content=body, headers={"X-GitHub-Event": "workflow_job"}).status_code == 401
    assert client.post("/webhook/github", content=body, headers=_signed("wrong", body)).status_code == 401
    assert sent == []                                                 # nothing was dispatched


def test_webhook_with_a_valid_signature_is_dispatched_to_the_worker(client, monkeypatch):
    monkeypatch.setattr(app_module.config.server, "webhook_secret", "s3cret")
    sent = _capture_dispatch(monkeypatch)
    body = b'{"action": "completed", "repository": {"full_name": "o/r"}}'
    r = client.post("/webhook/github", content=body, headers=_signed("s3cret", body))
    assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["task_id"] == "task-123"
    assert sent == [("workflow_job", {"action": "completed", "repository": {"full_name": "o/r"}})]


def test_webhook_without_a_configured_secret_accepts_unsigned_requests(client, monkeypatch):
    monkeypatch.setattr(app_module.config.server, "webhook_secret", None)
    sent = _capture_dispatch(monkeypatch)
    r = client.post("/webhook/github", json={"action": "completed"}, headers={"X-GitHub-Event": "workflow_job"})
    assert r.status_code == 200 and len(sent) == 1


def test_webhook_rejects_invalid_json(client, monkeypatch):
    monkeypatch.setattr(app_module.config.server, "webhook_secret", None)
    assert client.post("/webhook/github", content=b"not json", headers={"X-GitHub-Event": "push"}).status_code == 400


def test_health_reports_whether_a_celery_worker_answers(client, monkeypatch):
    monkeypatch.setattr(app_module, "_celery_ok", lambda: True)
    assert client.get("/health").json()["celery_connected"] is True
    monkeypatch.setattr(app_module, "_celery_ok", lambda: False)
    assert client.get("/health").json()["celery_connected"] is False


def test_celery_ok_is_false_when_the_broker_is_unreachable(monkeypatch):
    import gha_optimizer.worker.celery_app as celery_module

    class Broken:
        def inspect(self, timeout=1.0):
            raise ConnectionError("broker down")

    monkeypatch.setattr(celery_module.celery_app, "control", Broken())
    assert app_module._celery_ok() is False


def test_signature_check_matches_githubs_published_test_vector():
    """Secret, payload and expected value are the example in GitHub's 'Validating webhook deliveries' documentation."""
    secret, payload = "It's a Secret to Everybody", b"Hello, World!"
    expected = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert app_module._verify_webhook_signature(payload, expected, secret) is True
    assert app_module._verify_webhook_signature(payload, expected.replace("7571", "7572"), secret) is False
    assert app_module._verify_webhook_signature(payload, "", secret) is False


def test_startup_creates_the_tables_and_survives_an_unreachable_database(monkeypatch):
    """A fresh database (compose: only the worker used to create the tables) must not turn /api/v1/optimize into a raw SQL error: the API creates missing
    tables at start-up, and an unreachable database does not stop it from starting (/ready reports it)."""
    import asyncio

    import gha_optimizer.server.db.session as session_mod
    from gha_optimizer.server.app import app

    calls = []
    monkeypatch.setattr(session_mod, "init_db", lambda: calls.append("init_db"))

    async def start_and_stop():
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(start_and_stop())
    assert calls == ["init_db"]

    def boom():
        raise RuntimeError("database not reachable")

    monkeypatch.setattr(session_mod, "init_db", boom)
    asyncio.run(start_and_stop())          # must not raise
