"""REST API routes for pipelines, runs, logs and webhook ingestion."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, Response, status

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

from src.dashboard.models import (
    CreatePipelineRequest,
    CreateRunRequest,
    HealthResponse,
    LogLine,
    PipelineModel,
    RunModel,
    WebhookAck,
    utcnow,
)

LOG = logging.getLogger("devopspipeline.dashboard.routes")

api_router = APIRouter(prefix="/api/v1")


def _registry(request: Request) -> Any:
    """Fetch the in-memory registry attached to the application."""
    return request.app.state.registry


def _require_pipeline(request: Request, name: str) -> dict[str, Any]:
    """Return the stored pipeline or raise 404."""
    pipeline = _registry(request).get_pipeline(name)
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"pipeline '{name}' not found")
    return pipeline


# --------------------------------------------------------------------------- #
# health & metrics
# --------------------------------------------------------------------------- #
@api_router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Liveness/readiness probe with basic inventory counters."""
    state = request.app.state
    return HealthResponse(
        status="ok",
        version=getattr(state, "version", "1.0.0"),
        uptime_seconds=max(0.0, utcnow().timestamp() - getattr(state, "started_at", utcnow().timestamp())),
        pipelines=len(state.registry.list_pipelines()),
        runs=len(state.registry.list_runs(limit=10_000)),
    )


@api_router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    """Prometheus exposition endpoint."""
    body, content_type = request.app.state.collector.export()
    return Response(content=body, media_type=content_type)


# --------------------------------------------------------------------------- #
# pipelines
# --------------------------------------------------------------------------- #
@api_router.get("/pipelines", response_model=list[PipelineModel])
def list_pipelines(request: Request) -> list[PipelineModel]:
    """All registered pipelines."""
    return [PipelineModel(**item) for item in _registry(request).list_pipelines()]


@api_router.post(
    "/pipelines",
    response_model=PipelineModel,
    status_code=status.HTTP_201_CREATED,
)
def create_pipeline(request: Request, body: CreatePipelineRequest) -> PipelineModel:
    """Register a new (initially stage-less) pipeline."""
    created = _registry(request).register_pipeline(
        {"name": body.name, "description": body.description, "environment": body.environment}
    )
    return PipelineModel(**created)


@api_router.get("/pipelines/{name}", response_model=PipelineModel)
def get_pipeline(name: str, request: Request) -> PipelineModel:
    """Fetch one pipeline definition."""
    return PipelineModel(**_require_pipeline(request, name))


@api_router.delete("/pipelines/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pipeline(name: str, request: Request) -> Response:
    """Remove a pipeline registration."""
    if not _registry(request).delete_pipeline(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"pipeline '{name}' not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
@api_router.post(
    "/pipelines/{name}/run",
    response_model=RunModel,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_pipeline(
    name: str,
    request: Request,
    background: BackgroundTasks,
    body: CreateRunRequest | None = None,
) -> RunModel:
    """Queue a run; execution proceeds in the background."""
    payload = body or CreateRunRequest()
    _require_pipeline(request, name)
    run = _registry(request).create_run(name, trigger=payload.trigger, environment=payload.environment)
    engine = request.app.state.engine
    background.add_task(_execute_run, request.app, run["id"], name, payload.model_dump())
    if engine is None:
        LOG.debug("no engine attached; run %s will be simulated", run["id"])
    return RunModel(**run)


def _execute_run(app: FastAPI, run_id: str, pipeline_name: str, options: dict[str, Any]) -> None:
    """Background task flipping the run through its lifecycle."""
    registry = app.state.registry
    engine = app.state.engine
    collector = app.state.collector
    registry.update_run(run_id, status="RUNNING")
    try:
        if engine is not None:
            from src.orchestrator.pipeline import Pipeline

            pipeline = Pipeline.from_dict(registry.get_pipeline(pipeline_name))
            finished = engine.run(
                pipeline,
                event={"kind": options.get("trigger", "manual"), **options.get("variables", {})},
                environment=options.get("environment") or None,
            )
            data = finished.to_dict()
            registry.finish_run(
                run_id,
                status=data["status"],
                results=data["results"],
                duration=data["duration"],
            )
        else:
            registry.finish_run(run_id, status="SUCCESS", results={}, duration=0.01)
        collector.record_pipeline_run(
            pipeline_name, registry.get_run(run_id).get("status", "FAILED")
        )
    except Exception as exc:  # never leak into the worker thread
        LOG.exception("background execution of run %s failed", run_id)
        registry.finish_run(run_id, status="FAILED", error=str(exc))
        collector.record_pipeline_run(pipeline_name, "FAILED")


@api_router.get("/runs", response_model=list[RunModel])
def list_runs(
    request: Request,
    pipeline: str | None = Query(default=None),
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[RunModel]:
    """Recent runs, optionally filtered by pipeline and/or status."""
    items = _registry(request).list_runs(pipeline=pipeline, status=run_status, limit=limit)
    return [RunModel(**item) for item in items]


@api_router.get("/runs/{run_id}", response_model=RunModel)
def get_run(run_id: str, request: Request) -> RunModel:
    """One run's full detail."""
    run = _registry(request).get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run '{run_id}' not found")
    return RunModel(**run)


@api_router.get("/runs/{run_id}/logs", response_model=list[LogLine])
def get_run_logs(run_id: str, request: Request) -> list[LogLine]:
    """Flatten captured stage output into a log stream."""
    run = _registry(request).get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run '{run_id}' not found")
    lines: list[LogLine] = []
    base = run.get("started_at")
    for result in (run.get("results") or {}).values():
        for message in str(result.get("output", "")).splitlines() or [""]:
            lines.append(LogLine(timestamp=base or utcnow(), stream="stdout", stage=result.get("name", ""), message=message))
    return lines


@api_router.post("/runs/{run_id}/cancel", response_model=RunModel)
def cancel_run(run_id: str, request: Request) -> RunModel:
    """Best-effort cancellation for queued/running runs."""
    cancelled = _registry(request).cancel_run(run_id)
    if cancelled is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run '{run_id}' not found")
    if not cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "run already reached a terminal state")
    return RunModel(**_registry(request).get_run(run_id))


# --------------------------------------------------------------------------- #
# webhooks
# --------------------------------------------------------------------------- #
@api_router.post("/webhooks/{source}", response_model=WebhookAck)
async def ingest_webhook(source: str, request: Request) -> WebhookAck:
    """Forward an SCM webhook to the matching plugin."""
    raw_body = await request.body()
    headers = {key: value for key, value in request.headers.items()}
    plugins = request.app.state.plugins
    responses: list[dict[str, Any]] = []
    if plugins is not None:
        import json

        try:
            payload: dict[str, Any] = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        payload["__raw__"] = raw_body
        event_type = headers.get("x-github-event") or headers.get("x-gitlab-event") or source
        responses = plugins.dispatch_webhook(source, event_type, payload, headers)
    return WebhookAck(source=source, handled=bool(responses), responses=responses)


def iter_log_lines(text: str) -> Iterator[str]:  # pragma: no cover - helper export
    """Split a blob of output into individual lines."""
    yield from text.splitlines()
