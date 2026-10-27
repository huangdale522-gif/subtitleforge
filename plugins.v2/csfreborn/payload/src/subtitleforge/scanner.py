"""Resumable media-library discovery and subtitle completion."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import (
    MediaType,
    ProcessRequest,
    ProcessResult,
    ScanItemResult,
    ScanRequest,
    ScanResult,
)
from .service import SubtitleService
from .subtitle_files import ensure_path_allowed

TMDB_PATTERN = re.compile(r"(?i)\{?\btmdbid\s*[=:]\s*(\d+)\}?")
YEAR_PATTERN = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
EPISODE_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})(?:[^0-9]|$)"
)
CHINESE_EPISODE_PATTERN = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
SEASON_DIR_PATTERN = re.compile(r"(?i)^(?:season|s)\s*(\d{1,2})$")
IGNORED_DIRECTORIES = {
    "@eadir",
    "#recycle",
    "$recycle.bin",
    ".recycle",
    ".trash",
    "bdmv",
    "lost+found",
}
TRAILING_RELEASE_TOKENS = re.compile(
    r"(?i)(?:[ ._-]+(?:2160p|1080p|720p|480p|bluray|blu-ray|webrip|web-dl|"
    r"hdtv|remux|x264|x265|h264|h265|hevc|avc|aac|dts|proper|repack).*)$"
)
MAX_SCAN_ITEMS = 500


def _now() -> str:
    """Return an ISO UTC timestamp."""

    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class DiscoveredVideo:
    """Filesystem metadata plus the best title information derived from a path."""

    path: Path
    title: str
    original_title: str | None
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None
    media_type: MediaType
    season: int | None
    episode: int | None


@dataclass(slots=True)
class QueueEntry:
    """One persisted missing-subtitle task waiting for download quota."""

    video_path: str
    payload: dict[str, Any]
    attempts: int


class ScanStore:
    """Persistent full-scan state, download queue, and daily quota ledger."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "scans.sqlite3"
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_checks (
                    video_path TEXT PRIMARY KEY,
                    last_checked_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS download_queue (
                    video_path TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    queued_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_downloads (
                    day TEXT PRIMARY KEY,
                    count INTEGER NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived local database connection."""

        return sqlite3.connect(self.path, timeout=10)

    def last_checked(self) -> dict[str, datetime]:
        """Return known files and when they were last processed."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT video_path, last_checked_at FROM scan_checks"
            ).fetchall()
        result: dict[str, datetime] = {}
        for path, raw_value in rows:
            try:
                result[str(path)] = datetime.fromisoformat(str(raw_value))
            except ValueError:
                continue
        return result

    def mark_checked(self, video_path: Path, status: str) -> None:
        """Persist completion so the next batch spends quota on other files."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_checks (video_path, last_checked_at, status)
                VALUES (?, ?, ?)
                ON CONFLICT(video_path) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    status = excluded.status
                """,
                (str(video_path), _now(), status),
            )
            connection.commit()

    def is_due(self, video_path: Path, retry_after_hours: int) -> bool:
        """Return whether a file is outside its retry cooldown."""

        checked = self.last_checked().get(str(video_path))
        if checked is None:
            return True
        return datetime.now(UTC) - checked >= timedelta(hours=retry_after_hours)

    def upsert_queue_item(
        self, video_path: Path, payload: dict[str, Any]
    ) -> None:
        """Add or refresh one missing-subtitle task without losing its retry state."""

        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO download_queue (
                    video_path, payload_json, status, attempts,
                    next_attempt_at, last_error, queued_at, updated_at
                )
                VALUES (?, ?, 'pending', 0, NULL, NULL, ?, ?)
                ON CONFLICT(video_path) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    status = 'pending',
                    updated_at = excluded.updated_at
                """,
                (
                    str(video_path),
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            connection.commit()

    def remove_queue_item(self, video_path: Path) -> None:
        """Remove a task that no longer needs subtitles."""

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM download_queue WHERE video_path = ?",
                (str(video_path),),
            )
            connection.commit()

    def remove_unsupported_media(self) -> None:
        """Drop queue entries for Blu-ray segments that are not standalone media."""

        with self._connect() as connection:
            connection.execute(
                "DELETE FROM download_queue WHERE lower(video_path) LIKE '%/bdmv/%'"
            )
            connection.commit()

    def next_queue_items(self, limit: int) -> list[QueueEntry]:
        """Return due queue tasks in oldest-first order."""

        if limit <= 0:
            return []
        now = _now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT video_path, payload_json, attempts
                FROM download_queue
                WHERE status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY
                    CASE WHEN next_attempt_at IS NULL THEN 0 ELSE 1 END,
                    next_attempt_at,
                    queued_at
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        entries: list[QueueEntry] = []
        for video_path, payload_json, attempts in rows:
            try:
                payload = json.loads(str(payload_json))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                QueueEntry(
                    video_path=str(video_path),
                    payload=payload,
                    attempts=int(attempts or 0),
                )
            )
        return entries

    def mark_queue_result(
        self,
        video_path: Path,
        status: str,
        retry_after_hours: int,
        errors: list[str],
    ) -> None:
        """Complete a task or defer it for a later quota window."""

        if status in {"completed", "skipped"}:
            self.remove_queue_item(video_path)
            return
        now = datetime.now(UTC)
        next_attempt = now + timedelta(hours=max(1, retry_after_hours))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE download_queue
                SET status = 'pending',
                    attempts = attempts + 1,
                    next_attempt_at = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE video_path = ?
                """,
                (
                    next_attempt.isoformat(),
                    " | ".join(errors)[:2000],
                    now.isoformat(),
                    str(video_path),
                ),
            )
            connection.commit()

    def queue_count(self) -> int:
        """Return the number of queued missing-subtitle tasks."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM download_queue WHERE status = 'pending'"
            ).fetchone()
        return int(row[0]) if row else 0

    def queue_status(self) -> dict[str, int]:
        """Return total and currently due queue counts."""

        now = _now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(
                        CASE
                            WHEN next_attempt_at IS NULL THEN 1
                            WHEN next_attempt_at <= ? THEN 1
                            ELSE 0
                        END
                    )
                FROM download_queue
                WHERE status = 'pending'
                """,
                (now,),
            ).fetchone()
        total = int(row[0] or 0) if row else 0
        due = int(row[1] or 0) if row else 0
        return {"total": total, "due": due, "deferred": total - due}

    def downloads_today(self) -> int:
        """Return successful subtitle downloads recorded for the local day."""

        day = datetime.now().date().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count FROM daily_downloads WHERE day = ?",
                (day,),
            ).fetchone()
        return int(row[0]) if row else 0

    def record_downloads(self, count: int) -> None:
        """Add successful subtitle downloads to today's persistent ledger."""

        if count <= 0:
            return
        day = datetime.now().date().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO daily_downloads (day, count)
                VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    count = count + excluded.count
                """,
                (day, count),
            )
            connection.commit()


def _strip_release_suffix(value: str) -> str:
    """Remove common release tags from a file or directory name."""

    cleaned = TMDB_PATTERN.sub("", value)
    cleaned = TRAILING_RELEASE_TOKENS.sub("", cleaned)
    cleaned = cleaned.strip(" .-_[](){}")
    return " ".join(cleaned.split())


def _titles(name: str) -> tuple[str, str | None]:
    """Extract display and alternate titles from a bilingual folder name."""

    cleaned = YEAR_PATTERN.sub("", _strip_release_suffix(name))
    parts = [
        " ".join(part.split()).strip(" .-_[](){}")
        for part in re.split(r"\s*[/|]\s*", cleaned)
        if part.strip(" .-_[](){}")
    ]
    if not parts:
        return name.strip() or "Unknown", None
    return parts[0], parts[1] if len(parts) > 1 else None


def _year_from(values: Iterable[str]) -> int | None:
    """Return the first plausible year found in the supplied labels."""

    for value in values:
        match = YEAR_PATTERN.search(value)
        if match:
            return int(match.group(1))
    return None


def _season_directory(path: Path, root: Path) -> Path:
    """Find the show directory above a season folder."""

    parent = path.parent
    if parent == root:
        return parent
    match = SEASON_DIR_PATTERN.match(parent.name)
    if match or "season" in parent.name.casefold():
        return parent.parent
    return parent


def _nfo_ids(path: Path) -> tuple[int | None, str | None]:
    """Read common Kodi-style NFO identifiers when they are available."""

    show_dir = (
        path.parent.parent
        if SEASON_DIR_PATTERN.match(path.parent.name)
        or "season" in path.parent.name.casefold()
        else path.parent
    )
    candidates = [
        path.with_suffix(".nfo"),
        path.parent / "movie.nfo",
        path.parent / "tvshow.nfo",
        show_dir / "tvshow.nfo",
        show_dir / "movie.nfo",
    ]
    tmdb_id: int | None = None
    imdb_id: str | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            content = candidate.read_text(encoding="utf-8", errors="ignore")[:262_144]
        except OSError:
            continue
        if tmdb_id is None:
            match = re.search(r"(?is)<tmdbid>\s*(\d+)\s*</tmdbid>", content)
            if match:
                tmdb_id = int(match.group(1))
        if imdb_id is None:
            match = re.search(
                r"(?is)<imdbid>\s*(tt\d{5,12})\s*</imdbid>", content
            )
            if match:
                imdb_id = match.group(1)
        if tmdb_id is not None and imdb_id is not None:
            break
    return tmdb_id, imdb_id


def parse_media_path(path: Path, root: Path) -> DiscoveredVideo:
    """Derive searchable media metadata from a conventional library path."""

    episode_match = EPISODE_PATTERN.search(path.stem)
    chinese_episode = CHINESE_EPISODE_PATTERN.search(path.stem)
    season: int | None = None
    episode: int | None = None
    if episode_match:
        season = int(episode_match.group(1))
        episode = int(episode_match.group(2))
    elif chinese_episode:
        episode = int(chinese_episode.group(1))
        season_match = SEASON_DIR_PATTERN.match(path.parent.name)
        season = int(season_match.group(1)) if season_match else 1

    media_type = MediaType.TV if episode is not None else MediaType.MOVIE
    title_source = (
        _season_directory(path, root)
        if media_type is MediaType.TV
        else path.parent
    )
    if title_source == root or not title_source.name:
        title_source = path
    title, original_title = _titles(title_source.name)
    if title == "Unknown" and media_type is MediaType.TV:
        title, original_title = _titles(path.stem.split(" S", 1)[0])
    year = _year_from(
        (title_source.name, path.stem, path.parent.name, path.parent.parent.name)
    )
    tmdb_match = TMDB_PATTERN.search(
        " ".join(
            part
            for part in (
                title_source.name,
                path.parent.name,
                path.parent.parent.name,
                path.stem,
            )
            if part
        )
    )
    nfo_tmdb_id, nfo_imdb_id = _nfo_ids(path)
    return DiscoveredVideo(
        path=path,
        title=title,
        original_title=original_title,
        year=year,
        tmdb_id=int(tmdb_match.group(1)) if tmdb_match else nfo_tmdb_id,
        imdb_id=nfo_imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
    )


def discover_videos(
    roots: list[Path],
    exclude_paths: list[str],
    extensions: set[str],
) -> list[tuple[Path, Path]]:
    """Walk configured roots without following symlinked directories."""

    root_pairs: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    excluded = [value.casefold() for value in exclude_paths if value.strip()]
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix.casefold() not in extensions:
                continue
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                root_pairs.append((resolved, resolved.parent))
            continue
        walk_root = root if root.is_dir() else root.parent
        for current, directories, files in os.walk(walk_root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in IGNORED_DIRECTORIES
                and not name.startswith(".")
                and not any(
                    excluded_path in str(current_path / name).casefold()
                    for excluded_path in excluded
                )
            ]
            for name in files:
                path = current_path / name
                if (
                    path.suffix.casefold() not in extensions
                    or path.name.startswith(".")
                ):
                    continue
                if re.search(
                    r"(?i)(?:^|[ ._-])sample(?:[ ._-]|$)", path.stem
                ):
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                if any(
                    excluded_path in str(resolved).casefold()
                    for excluded_path in excluded
                ):
                    continue
                seen.add(resolved)
                root_pairs.append((resolved, root.resolve()))
    return root_pairs


def interleave_by_media_directory(
    items: list[tuple[Path, Path]],
) -> list[tuple[Path, Path]]:
    """Rotate through media directories so one series cannot fill a batch."""

    groups: dict[Path, list[tuple[Path, Path]]] = {}
    for item in items:
        path = item[0]
        group = path.parent
        if SEASON_DIR_PATTERN.match(group.name) or "season" in group.name.casefold():
            group = group.parent
        groups.setdefault(group, []).append(item)
    interleaved: list[tuple[Path, Path]] = []
    while groups:
        for group in list(groups):
            interleaved.append(groups[group].pop(0))
            if not groups[group]:
                del groups[group]
    return interleaved


class LibraryScanner:
    """Inspect the full library, queue missing subtitles, then spend a bounded quota."""

    def __init__(self, service: SubtitleService, data_dir: Path) -> None:
        self._service = service
        self._store = ScanStore(data_dir)
        self._scan_lock = asyncio.Lock()

    def queue_status(self) -> dict[str, int]:
        """Return the persisted missing-subtitle queue summary."""

        return self._store.queue_status()

    async def scan(self, request: ScanRequest) -> ScanResult:
        """Serialize full scans so concurrent triggers cannot exceed daily quota."""

        async with self._scan_lock:
            return await self._scan_locked(request)

    async def _scan_locked(self, request: ScanRequest) -> ScanResult:
        """Inventory every video before downloading a quota-bounded queue batch."""

        if not request.dry_run:
            self._store.remove_unsupported_media()

        roots: list[Path] = []
        errors: list[str] = []
        for raw_root in request.paths:
            try:
                root = ensure_path_allowed(
                    Path(raw_root), self._service.settings.media_root_paths
                )
            except ValueError as exc:
                errors.append(f"{raw_root}: {exc}")
                continue
            if not root.exists():
                errors.append(f"{raw_root}: path does not exist")
                continue
            roots.append(root)

        discovered = discover_videos(
            roots,
            request.exclude_paths,
            set(request.video_extensions),
        )
        if not discovered:
            queue = self._store.queue_status()
            return ScanResult(
                status="not_found",
                discovered=0,
                queue_remaining=queue["total"],
                remaining=queue["total"],
                downloads_today=self._store.downloads_today(),
                daily_download_limit=request.daily_download_limit,
                dry_run=request.dry_run,
                errors=errors,
            )

        ordered = interleave_by_media_directory(
            sorted(discovered, key=lambda item: str(item[0]))
        )
        result = ScanResult(
            status="processing",
            discovered=len(ordered),
            downloads_today=self._store.downloads_today(),
            daily_download_limit=request.daily_download_limit,
            dry_run=request.dry_run,
            errors=errors,
        )
        items_by_path: dict[str, ScanItemResult] = {}

        # Phase 1: inspect every file locally. This never queries a provider.
        for path, root in ordered:
            discovered_media = parse_media_path(path, root)
            inspect_request = ProcessRequest(
                video_path=str(path),
                title=discovered_media.title,
                original_title=discovered_media.original_title,
                year=discovered_media.year,
                imdb_id=discovered_media.imdb_id,
                tmdb_id=discovered_media.tmdb_id,
                media_type=discovered_media.media_type,
                season=discovered_media.season,
                episode=discovered_media.episode,
                languages=request.languages,
                overwrite=request.overwrite,
                dry_run=True,
                provider_failover=request.provider_failover,
                provider_order=request.provider_order,
            )
            try:
                inspected = await self._service.process(inspect_request)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop a scan
                item_errors = [f"{type(exc).__name__}: {exc}"]
                inspected = ProcessResult(
                    status="error",
                    video_path=str(path),
                    errors=item_errors,
                )
            result.checked += 1
            item = ScanItemResult(
                video_path=str(path),
                title=discovered_media.title,
                year=discovered_media.year,
                media_type=discovered_media.media_type,
                season=discovered_media.season,
                episode=discovered_media.episode,
                status=inspected.status,
                existing_languages=inspected.existing_languages,
                missing_languages=inspected.missing_languages,
                errors=list(inspected.errors),
            )
            if inspected.errors:
                result.errors.extend(f"{path}: {error}" for error in inspected.errors)
            if not inspected.missing_languages:
                result.existing += 1
                if not request.dry_run:
                    self._store.remove_queue_item(path)
                continue

            result.queued += 1
            if request.dry_run:
                item.status = "dry_run"
            else:
                payload = ProcessRequest(
                    video_path=str(path),
                    title=discovered_media.title,
                    original_title=discovered_media.original_title,
                    year=discovered_media.year,
                    imdb_id=discovered_media.imdb_id,
                    tmdb_id=discovered_media.tmdb_id,
                    media_type=discovered_media.media_type,
                    season=discovered_media.season,
                    episode=discovered_media.episode,
                    languages=inspected.missing_languages,
                    overwrite=request.overwrite,
                    dry_run=False,
                    provider_failover=request.provider_failover,
                    provider_order=request.provider_order,
                ).model_dump(mode="json")
                self._store.upsert_queue_item(path, payload)
                item.status = "queued"
            if len(items_by_path) < MAX_SCAN_ITEMS:
                items_by_path[item.video_path] = item

        queue = self._store.queue_status()
        result.queue_remaining = queue["total"]
        result.remaining = queue["total"]
        if request.dry_run:
            result.items = list(items_by_path.values())
            result.status = "dry_run" if result.queued else "completed"
            return result

        # Phase 2: only queued missing files can consume provider quota.
        used_today = self._store.downloads_today()
        result.downloads_today = used_today
        daily_remaining = max(0, request.daily_download_limit - used_today)
        result.download_limit = min(request.max_files, daily_remaining)
        if result.download_limit <= 0:
            result.items = list(items_by_path.values())
            result.status = "queued" if queue["total"] else _scan_status(result)
            return result

        queued_entries = self._store.next_queue_items(result.download_limit)
        for entry in queued_entries:
            used_today = self._store.downloads_today()
            remaining_slots = request.daily_download_limit - used_today
            if remaining_slots <= 0:
                break
            process_request: ProcessRequest | None = None
            try:
                process_request = ProcessRequest.model_validate(entry.payload)
                process_request.languages = process_request.languages[:remaining_slots]
                process_request.overwrite = request.overwrite
                process_request.dry_run = False
                process_request.provider_failover = request.provider_failover
                process_request.provider_order = request.provider_order
                processed = await self._service.process(process_request)
            except Exception as exc:  # noqa: BLE001 - keep one task from blocking the queue
                processed = ProcessResult(
                    status="error",
                    video_path=entry.video_path,
                    errors=[f"{type(exc).__name__}: {exc}"],
                )

            downloaded_count = len(processed.downloaded)
            if downloaded_count:
                self._store.record_downloads(downloaded_count)
            self._store.mark_queue_result(
                Path(entry.video_path),
                processed.status,
                request.retry_after_hours,
                processed.errors,
            )
            result.downloaded += downloaded_count
            result.downloads_today = self._store.downloads_today()
            item = items_by_path.get(entry.video_path)
            if item is not None:
                item.status = processed.status
                item.existing_languages = processed.existing_languages
                item.missing_languages = processed.missing_languages
                item.downloaded = processed.downloaded
                item.errors = list(processed.errors)
            elif len(items_by_path) < MAX_SCAN_ITEMS:
                items_by_path[entry.video_path] = ScanItemResult(
                    video_path=entry.video_path,
                    title=(
                        process_request.title
                        if process_request is not None
                        else str(entry.payload.get("title") or entry.video_path)
                    ),
                    year=(
                        process_request.year
                        if process_request is not None
                        else None
                    ),
                    media_type=(
                        process_request.media_type
                        if process_request is not None
                        else MediaType.MOVIE
                    ),
                    season=(
                        process_request.season
                        if process_request is not None
                        else None
                    ),
                    episode=(
                        process_request.episode
                        if process_request is not None
                        else None
                    ),
                    status=processed.status,
                    existing_languages=processed.existing_languages,
                    missing_languages=processed.missing_languages,
                    downloaded=processed.downloaded,
                    errors=list(processed.errors),
                )
            if processed.errors:
                result.errors.extend(
                    f"{entry.video_path}: {error}" for error in processed.errors
                )

        queue = self._store.queue_status()
        result.queue_remaining = queue["total"]
        result.remaining = queue["total"]
        result.items = list(items_by_path.values())
        if result.errors:
            result.status = "partial"
        else:
            result.status = "completed" if queue["total"] == 0 else "queued"
        return result


def _scan_status(result: ScanResult) -> str:
    """Return a useful status when no queue work is currently due."""

    if result.errors:
        return "partial"
    return "completed"
