"""Persistent background job storage for long-running subtitle searches."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import JobRecord, ProcessRequest, ProcessResult
from .service import SubtitleService


def _now() -> str:
    """Return an ISO UTC timestamp."""

    return datetime.now(UTC).isoformat()


class JobStore:
    """SQLite-backed job history."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "jobs.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """Create a short-lived SQLite connection."""

        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        """Create the jobs table and mark interrupted work."""

        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                )
                """
            )
            now = _now()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', updated_at = ?, error = 'Service restarted before job completion'
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            connection.commit()

    async def create(self, request: ProcessRequest) -> JobRecord:
        """Persist a new queued job."""

        return await asyncio.to_thread(self._create, request)

    def _create(self, request: ProcessRequest) -> JobRecord:
        """Synchronous job creation."""

        now = _now()
        record = JobRecord(
            job_id=uuid4().hex,
            status="queued",
            created_at=now,
            updated_at=now,
            request=request,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (job_id, status, created_at, updated_at, request_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.job_id,
                    record.status,
                    record.created_at,
                    record.updated_at,
                    request.model_dump_json(),
                ),
            )
            connection.commit()
        return record

    async def update(
        self,
        job_id: str,
        status: str,
        result: ProcessResult | None = None,
        error: str | None = None,
    ) -> None:
        """Update a job's terminal or running state."""

        await asyncio.to_thread(self._update, job_id, status, result, error)

    def _update(
        self,
        job_id: str,
        status: str,
        result: ProcessResult | None,
        error: str | None,
    ) -> None:
        """Synchronous job update."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, result_json = ?, error = ?
                WHERE job_id = ?
                """,
                (
                    status,
                    _now(),
                    result.model_dump_json() if result else None,
                    error,
                    job_id,
                ),
            )
            connection.commit()

    async def get(self, job_id: str) -> JobRecord | None:
        """Read one job."""

        return await asyncio.to_thread(self._get, job_id)

    def _get(self, job_id: str) -> JobRecord | None:
        """Synchronous job read."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    async def list(self, limit: int = 50) -> list[JobRecord]:
        """Read recent jobs."""

        return await asyncio.to_thread(self._list, limit)

    def _list(self, limit: int) -> list[JobRecord]:
        """Synchronous recent-job read."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        """Convert a database row to an API model."""

        return JobRecord(
            job_id=row["job_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            request=ProcessRequest.model_validate_json(row["request_json"]),
            result=(
                ProcessResult.model_validate_json(row["result_json"])
                if row["result_json"]
                else None
            ),
            error=row["error"],
        )


class JobManager:
    """Run jobs in the service event loop while keeping state in SQLite."""

    def __init__(self, store: JobStore, service: SubtitleService) -> None:
        self._store = store
        self._service = service
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, request: ProcessRequest) -> JobRecord:
        """Persist and schedule a processing job."""

        record = await self._store.create(request)
        task = asyncio.create_task(
            self._run(record.job_id, request), name=f"subtitle-job-{record.job_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def _run(self, job_id: str, request: ProcessRequest) -> None:
        """Execute a job and persist its terminal result."""

        await self._store.update(job_id, "running")
        try:
            result = await self._service.process(request)
            await self._store.update(job_id, result.status, result=result)
        except Exception as exc:  # noqa: BLE001 - job failure must be persisted
            await self._store.update(job_id, "failed", error=str(exc))

    async def close(self) -> None:
        """Cancel outstanding jobs during service shutdown."""

        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
