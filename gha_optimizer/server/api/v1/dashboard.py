"""
Dashboard REST API - the data behind ``gha_optimizer/dashboard_ui/index.html``.

  GET  /dashboard/repos               ingested repositories (+ whether a result is ready)
  POST /dashboard/run                 start (or reuse) the pipeline for a repository; idempotent
  GET  /dashboard/status              stage-by-stage progress of that run
  GET  /dashboard/result              the finished result (404 until ready)
  GET  /dashboard/files               search the repository's files / one-click what-if scenarios
  POST /dashboard/whatif              impacted jobs, predictions and schedule for other changed files
  GET  /dashboard/evidence            rows of the generated reports (docs/) for a repository
  GET  /dashboard/docs[/{name}]       list / read the generated reports
  GET  /dashboard/live                hash-chain verified summary of the live-scoring ledgers

The service object is shared; ``configure`` is called by the standalone dashboard app and by the main API app.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from gha_optimizer.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
_service: DashboardService | None = None


def configure(service: DashboardService) -> None:
    global _service
    _service = service


def service() -> DashboardService:
    global _service
    if _service is None:
        _service = DashboardService()
    return _service


class WhatIfRequest(BaseModel):
    repo: str
    changed_files: list[str] = Field(default_factory=list, max_length=500)


@router.get("/repos")
def repos():
    svc = service()
    return {"default": svc.default_repo, "repos": svc.repos()}


@router.post("/run")
def run(repo: str, force: bool = False, need_context: bool = False):
    return service().start(repo, force=force, need_context=need_context)


@router.get("/status")
def status(repo: str):
    return service().status(repo)


@router.get("/result")
def result(repo: str):
    res = service().result(repo)
    if res is None:
        raise HTTPException(status_code=404, detail="no result yet: POST /dashboard/run first")
    return res


@router.get("/files")
def files(repo: str, q: str = "", limit: int = Query(40, ge=1, le=200)):
    return service().files(repo, q, limit)


@router.post("/whatif")
def whatif(req: WhatIfRequest):
    try:
        return service().what_if(req.repo, req.changed_files)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/measured")
def measured(repo: str):
    return service().measured(repo)


@router.get("/evidence")
def evidence(repo: str):
    return service().evidence(repo)


@router.get("/docs")
def docs():
    from gha_optimizer import evidence as ev

    return {"docs": ev.list_docs(service().docs_dir)}


@router.get("/docs/{name}", response_class=PlainTextResponse)
def doc(name: str):
    text = service().doc(name)
    if text is None:
        raise HTTPException(status_code=404, detail="no such report")
    return text


@router.get("/live")
def live():
    return service().live()
