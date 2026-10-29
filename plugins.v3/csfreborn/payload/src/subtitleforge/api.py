"""FastAPI application for SubtitleForge."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from .config import Settings
from .jobs import JobManager, JobStore
from .models import (
    JobRecord,
    ManualDownloadRequest,
    ManualSearchRequest,
    ManualSearchResult,
    ProcessRequest,
    ProcessResult,
    QueueView,
    ScanRequest,
    ScanResult,
    SearchRequest,
    SubtitleCandidate,
)
from .scanner import LibraryScanner
from .service import SubtitleService


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the SubtitleForge ASGI application."""

    runtime_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = SubtitleService(runtime_settings)
        store = JobStore(runtime_settings.data_dir)
        jobs = JobManager(store, service)
        app.state.settings = runtime_settings
        app.state.service = service
        app.state.store = store
        app.state.jobs = jobs
        app.state.scanner = LibraryScanner(service, runtime_settings.data_dir)
        try:
            yield
        finally:
            await jobs.close()
            await service.close()

    app = FastAPI(
        title="SubtitleForge",
        version="0.4.0",
        description="Provider-driven Chinese subtitle downloader for NAS and MoviePilot.",
        lifespan=lifespan,
    )

    def require_token(
        authorization: Annotated[str | None, Header()] = None,
        x_api_token: Annotated[str | None, Header()] = None,
    ) -> None:
        """Require the configured bearer token when one is set."""

        configured = runtime_settings.api_token
        if not configured:
            return
        supplied = ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        elif x_api_token:
            supplied = x_api_token.strip()
        if not supplied or not secrets.compare_digest(supplied, configured):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token"
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Return a public liveness response with provider configuration state."""

        service: SubtitleService = app.state.service
        return {
            "status": "ok",
            "version": "0.4.0",
            "providers": {
                name: provider.configured
                for name, provider in service.providers.items()
            },
        }

    @app.get("/v1/status")
    async def runtime_status(
        daily_download_limit: int = Query(default=10, ge=1, le=1000),
        queue_limit: int = Query(default=30, ge=1, le=100),
        _: None = Depends(require_token),
    ) -> dict[str, Any]:
        """Return live quota, queue, and provider health for the plugin page."""

        scanner: LibraryScanner = app.state.scanner
        service: SubtitleService = app.state.service
        return {
            "status": "ok",
            "version": "0.4.0",
            **scanner.status(daily_download_limit, queue_limit),
            "providers": service.status(),
        }

    @app.get("/v1/queue", response_model=QueueView)
    async def queue_view(
        limit: int = Query(default=30, ge=1, le=100),
        _: None = Depends(require_token),
    ) -> QueueView:
        """Return the pending subtitle queue grouped by season."""

        scanner: LibraryScanner = app.state.scanner
        return scanner.queue_view(limit)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        """Show a minimal landing page."""

        return """
        <!doctype html>
        <html lang="zh-CN">
        <head><meta charset="utf-8"><title>SubtitleForge</title></head>
        <body style="font-family:system-ui;max-width:760px;margin:48px auto;line-height:1.6">
          <h1>SubtitleForge</h1>
          <p>字幕服务正在运行。API 文档位于 <a href="/docs">/docs</a>。</p>
          <p>健康检查：<a href="/healthz">/healthz</a></p>
        </body>
        </html>
        """

    @app.post("/v1/search", response_model=list[SubtitleCandidate])
    async def search(
        request: SearchRequest,
        _: None = Depends(require_token),
    ) -> list[SubtitleCandidate]:
        """Return ranked subtitle candidates."""

        service: SubtitleService = app.state.service
        candidates, _errors = await service.search(request)
        return candidates

    @app.post("/v1/process", response_model=ProcessResult)
    async def process(
        request: ProcessRequest,
        _: None = Depends(require_token),
    ) -> ProcessResult:
        """Synchronously search and install subtitles."""

        service: SubtitleService = app.state.service
        try:
            return await service.process(request)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/v1/jobs", response_model=JobRecord, status_code=status.HTTP_202_ACCEPTED
    )
    async def create_job(
        request: ProcessRequest,
        _: None = Depends(require_token),
    ) -> JobRecord:
        """Queue a subtitle processing job."""

        try:
            # Validate the target before exposing a job id.
            from .subtitle_files import ensure_path_allowed

            video = ensure_path_allowed(
                request.video, runtime_settings.media_root_paths
            )
            if not video.is_file():
                raise ValueError("Video file does not exist")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        jobs: JobManager = app.state.jobs
        return await jobs.submit(request)

    @app.post("/v1/scan", response_model=ScanResult)
    async def scan_library(
        request: ScanRequest,
        _: None = Depends(require_token),
    ) -> ScanResult:
        """Scan a bounded batch for videos that still need Chinese subtitles."""

        scanner: LibraryScanner = app.state.scanner
        return await scanner.scan(request)

    @app.post("/v1/manual/search", response_model=ManualSearchResult)
    async def manual_search(
        request: ManualSearchRequest,
        _: None = Depends(require_token),
    ) -> ManualSearchResult:
        """Search every configured source for one queued video."""

        scanner: LibraryScanner = app.state.scanner
        try:
            return await scanner.manual_search(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/manual/download", response_model=ProcessResult)
    async def manual_download(
        request: ManualDownloadRequest,
        _: None = Depends(require_token),
    ) -> ProcessResult:
        """Download and install one explicitly selected subtitle."""

        scanner: LibraryScanner = app.state.scanner
        try:
            return await scanner.manual_download(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/jobs/{job_id}", response_model=JobRecord)
    async def get_job(
        job_id: str,
        _: None = Depends(require_token),
    ) -> JobRecord:
        """Return one job."""

        store: JobStore = app.state.store
        record = await store.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return record

    @app.get("/v1/jobs", response_model=list[JobRecord])
    async def list_jobs(
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(require_token),
    ) -> list[JobRecord]:
        """Return recent jobs."""

        store: JobStore = app.state.store
        return await store.list(limit)

    return app


app = create_app()
