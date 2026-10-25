"""
The standalone dashboard: ``gha-optimizer dashboard`` (and ``python demo_server.py``).

A small FastAPI app that serves ``dashboard/index.html`` and the dashboard API on the repositories already ingested into
the local database.  No Postgres, Redis or Celery is needed.  The same UI and API are mounted in the full API server
(``gha-optimizer serve`` -> ``/dashboard``), so there is exactly one dashboard.
"""
from __future__ import annotations

from typing import Any

import logging
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from gha_optimizer.dashboard_service import DashboardService
from gha_optimizer.server.api.v1 import dashboard as dashboard_api

UI_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


def ui_html() -> str:
    return UI_PATH.read_text(encoding="utf-8")


def mount_dashboard(app: FastAPI, service: DashboardService | None = None) -> None:
    """Add the dashboard API (``/api/v1/dashboard/*``) and the page itself (``/dashboard``) to ``app``."""
    if service is not None:
        dashboard_api.configure(service)
    app.include_router(dashboard_api.router, prefix="/api/v1")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_page():
        return HTMLResponse(content=ui_html())


def create_dashboard_app(service: DashboardService) -> FastAPI:
    app = FastAPI(title="GHA Optimizer dashboard")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"], allow_methods=["*"],
                       allow_headers=["*"])
    mount_dashboard(app, service)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def root():
        return HTMLResponse(content=ui_html())

    return app


def run_dashboard(repo: str | None, host: str = "127.0.0.1", port: int = 8501, warm: bool = False,
                  snapshot_root: str | None = None, cache_dir: str | None = None) -> None:
    import uvicorn

    kwargs: dict[str, Any] = {"cache_dir": cache_dir} if cache_dir else {}
    service = DashboardService(default_repo=repo, snapshot_root=snapshot_root, **kwargs)
    available = [r["repo"] for r in service.repos()]
    if repo is None and available:
        service.default_repo = repo = available[0]
    if not available:
        raise SystemExit("no ingested repository: run `gha-optimizer ingest --repo owner/repo` first")
    todo = ([repo] if repo else []) + ([r for r in available if r != repo] if warm else [])
    threading.Thread(target=service.warm, args=(todo,), daemon=True).start()
    logging.getLogger("gha-optimizer").info("dashboard on http://%s:%d  (default repository: %s%s)", host, port, repo,
                                            ", warming all ingested repositories" if warm else "")
    uvicorn.run(create_dashboard_app(service), host=host, port=port, log_level="warning")
