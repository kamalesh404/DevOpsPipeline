"""FastAPI application factory for the DevOpsPipeline dashboard.

Wires the in-memory :class:`Registry`, optional engine/plugin hooks, CORS,
Prometheus metrics and REST routes into a ready-to-serve ASGI app. The
factory pattern (``create_app``) supports ``uvicorn --factory`` and testing
via ``TestClient``.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.dashboard.models import utcnow
from src.dashboard.routes import api_router
from src.metrics.collector import MetricsCollector, get_collector

LOG = logging.getLogger("devopspipeline.dashboard")


class Registry:
    """Thread-safe in-memory store for pipelines and runs.

    Suitable for development and single-node deployments; production
    deployments would swap in a database-backed implementation with the same
    surface.
    """

    def __init__(self) -> None:
        self._pipelines: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # -- pipelines ---------------------------------------------------------
    def register_pipeline(self, data: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace a pipeline definition."""
        record = {**data, "stages": list(data.get("stages", [])), "triggers": list(data.get("triggers", []))}
        with self._lock:
            self._pipelines[record["name"]] = record
        return record

    def get_pipeline(self, name: str) -> Optional[dict[str, Any]]:
        """Fetch a pipeline by name."""
        with self._lock:
            return self._pipelines.get(name)

    def list_pipelines(self) -> list[dict[str, Any]]:
        """All pipelines ordered by name."""
        with self._lock:
            return sorted((dict(item) for item in self._pipelines.values()), key=lambda p: p["name"])

    def delete_pipeline(self, name: str) -> bool:
        """Remove a pipeline; False when unknown."""
        with self._lock:
            return self._pipelines.pop(name, None) is not None

    # -- runs ---------------------------------------------------------------
    def create_run(
        self,
        pipeline_name: str,
        *,
        trigger: str = "manual",
        environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a PENDING run skeleton for ``pipeline_name``."""
        run_id = uuid.uuid4().hex[:12]
        run = {
            "id": run_id,
            "pipeline": pipeline_name,
            "trigger": trigger,
            "status": "PENDING",
            "started_at": utcnow(),
            "finished_at": None,
            "duration": 0.0,
            "environment": environment or {},
            "results": {},
            "error": None,
        }
        with self._lock:
            self._runs[run_id] = run
        return run

    def update_run(self, run_id: str, **fields: Any) -> None:
        """Merge fields into an existing run."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.update(fields)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        results: dict[str, Any],
        duration: float = 0.0,
        error: str | None = None,
    ) -> None:
        """Move a run to a terminal state."""
        self.update_run(
            run_id,
            status=status,
            finished_at=utcnow(),
            duration=duration or 0.0,
            results=results or {},
            error=error,
        )

    def cancel_run(self, run_id: str) -> bool | None:
        """Cancel a non-terminal run; None when it does not exist."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            if run["status"] in {"SUCCESS", "FAILED", "CANCELLED", "SKIPPED"}:
                return False
            run.update(status="CANCELLED", finished_at=utcnow())
            return True

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Fetch a run by id."""
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(
        self,
        *,
        pipeline: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Most recent runs first, optionally filtered."""
        items = [
            dict(item)
            for item in self._runs.values()
            if (pipeline is None or item["pipeline"] == pipeline)
            and (status is None or item["status"] == status)
        ]
        return sorted(items, key=lambda r: r["started_at"], reverse=True)[:limit]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Record process start time for uptime reporting."""
    from time import time

    app.state.started_at = time()
    LOG.info("dashboard started")
    yield


def create_app(
    registry: Registry | None = None,
    *,
    plugins: Any = None,
    engine: Any = None,
    collector: MetricsCollector | None = None,
    title: str = "DevOpsPipeline Dashboard",
    version: str = "1.0.0",
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build a fully configured dashboard application."""
    app = FastAPI(title=title, version=version, lifespan=lifespan)
    app.state.registry = registry or Registry()
    app.state.plugins = plugins
    app.state.engine = engine
    app.state.collector = collector or get_collector()
    app.state.version = version

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        LOG.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"service": title, "version": version, "docs": "/docs", "health": "/api/v1/health"}

    app.include_router(api_router)
    return app


_default_app: Optional[FastAPI] = None


def create_default_app() -> FastAPI:
    """Singleton used by `python -m uvicorn src.dashboard.app:create_app`."""
    global _default_app
    if _default_app is None:
        _default_app = create_app()
    return _default_app
