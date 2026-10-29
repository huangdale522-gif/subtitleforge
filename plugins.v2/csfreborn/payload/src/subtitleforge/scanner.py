"""Resumable media-library discovery and subtitle completion."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from .models import (
    ManualCandidateView,
    ManualDownloadRequest,
    ManualSearchResult,
    MediaType,
    ProcessRequest,
    ProcessResult,
    QueueGroupView,
    QueueItemView,
    QueueView,
    ScanMediaMetadata,
    ScanItemResult,
    ScanRequest,
    ScanResult,
    SubtitleCandidate,
    SubtitleLanguage,
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
POSTER_BASENAMES = (
    "poster",
    "folder",
    "show",
    "cover",
)
POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
POSTER_MAX_BYTES = 256 * 1024
QUEUE_ENTRY_SCAN_LIMIT = 5000
QUEUE_MAX_ITEMS_PER_GROUP = 100
TRAILING_RELEASE_TOKENS = re.compile(
    r"(?i)(?:[ ._-]+(?:2160p|1080p|720p|480p|bluray|blu-ray|webrip|web-dl|"
    r"hdtv|remux|x264|x265|h264|h265|hevc|avc|aac|dts|proper|repack).*)$"
)
MAX_SCAN_ITEMS = 500
ANIME_GENRE_IDS = {"16"}
ANIME_GENRE_NAMES = {
    "animation",
    "anime",
    "动画",
    "动漫",
    "番剧",
}
ANIME_PATH_MARKERS = {
    "动漫",
    "动画",
    "番剧",
    "anime",
    "animation",
}
CHINESE_LANGUAGE_CODES = {"zh", "cn", "bo", "za"}
REGION_ALIASES = {
    "cn": {
        "cn",
        "chn",
        "china",
        "中国",
        "中国大陆",
        "中国内地",
        "中华人民共和国",
    },
    "tw": {
        "tw",
        "twn",
        "taiwan",
        "台湾",
        "中国台湾",
        "台灣",
    },
    "hk": {
        "hk",
        "hkg",
        "hongkong",
        "hong kong",
        "香港",
        "中国香港",
    },
    "mo": {
        "mo",
        "macau",
        "macao",
        "澳门",
        "澳門",
        "中国澳门",
    },
}


def _now() -> str:
    """Return an ISO UTC timestamp."""

    return datetime.now(UTC).isoformat()


def _optional_int(value: Any) -> int | None:
    """Convert an optional persisted value to an integer."""

    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an optional ISO timestamp into an aware UTC datetime."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
    genres: list[str] = field(default_factory=list)
    origin_countries: list[str] = field(default_factory=list)
    original_language: str | None = None
    poster_url: str | None = None
    poster_source: str | None = None


@dataclass(slots=True)
class QueueEntry:
    """One persisted missing-subtitle task waiting for download quota."""

    video_path: str
    payload: dict[str, Any]
    attempts: int
    queued_at: str | None = None
    next_attempt_at: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class NfoMetadata:
    """Useful media metadata read from a Kodi-style NFO file."""

    tmdb_id: int | None = None
    imdb_id: str | None = None
    genres: list[str] = field(default_factory=list)
    origin_countries: list[str] = field(default_factory=list)
    original_language: str | None = None
    poster_url: str | None = None


@dataclass(slots=True)
class ManualSearchSession:
    """Short-lived server-side candidate references for manual selection."""

    search_id: str
    request: ProcessRequest
    candidates: dict[str, SubtitleCandidate]
    expires_at: datetime


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

    def remove_excluded_paths(self, prefixes: list[str]) -> int:
        """Drop queued tasks under configured directory exclusions."""

        normalized = [
            str(prefix).replace("\\", "/").rstrip("/").casefold()
            for prefix in prefixes
            if str(prefix).strip()
        ]
        if not normalized:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT video_path FROM download_queue"
            ).fetchall()
            matches = [
                str(row[0])
                for row in rows
                if any(
                    _path_prefix_matches(Path(str(row[0])), prefix)
                    for prefix in normalized
                )
            ]
            if matches:
                connection.executemany(
                    "DELETE FROM download_queue WHERE video_path = ?",
                    ((video_path,) for video_path in matches),
                )
                connection.executemany(
                    "DELETE FROM scan_checks WHERE video_path = ?",
                    ((video_path,) for video_path in matches),
                )
            connection.commit()
        return len(matches)

    @staticmethod
    def _queue_entry(row: tuple[Any, ...]) -> QueueEntry | None:
        """Convert a queue row into a typed entry."""

        if len(row) < 3:
            return None
        video_path, payload_json, attempts = row[:3]
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return QueueEntry(
            video_path=str(video_path),
            payload=payload,
            attempts=int(attempts or 0),
            queued_at=str(row[3]) if len(row) > 3 and row[3] else None,
            next_attempt_at=str(row[4]) if len(row) > 4 and row[4] else None,
            last_error=str(row[5]) if len(row) > 5 and row[5] else None,
        )

    def get_queue_item(self, video_path: Path) -> QueueEntry | None:
        """Return one pending queue entry by its exact video path."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    video_path, payload_json, attempts,
                    queued_at, next_attempt_at, last_error
                FROM download_queue
                WHERE video_path = ? AND status = 'pending'
                """,
                (str(video_path),),
            ).fetchone()
        return self._queue_entry(tuple(row)) if row else None

    def queue_entries(self, limit: int = 500) -> list[QueueEntry]:
        """Return pending queue entries for the grouped plugin view."""

        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    video_path, payload_json, attempts,
                    queued_at, next_attempt_at, last_error
                FROM download_queue
                WHERE status = 'pending'
                ORDER BY queued_at, video_path
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        entries = [self._queue_entry(tuple(row)) for row in rows]
        return [entry for entry in entries if entry is not None]

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
        entries = [self._queue_entry(tuple(row)) for row in rows]
        entries = [entry for entry in entries if entry is not None]
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


def _nfo_values(content: str, *tags: str) -> list[str]:
    """Read simple text values from common Kodi-style NFO tags."""

    values: list[str] = []
    for tag in tags:
        pattern = re.compile(
            rf"(?is)<(?:[a-z0-9_]+:)?{re.escape(tag)}"
            rf"(?:\s[^>]*)?>\s*([^<]+?)\s*"
            rf"</(?:[a-z0-9_]+:)?{re.escape(tag)}>"
        )
        values.extend(
            unescape(match.group(1)).strip()
            for match in pattern.finditer(content)
            if match.group(1).strip()
        )
    return list(dict.fromkeys(values))


def _xml_local_name(tag: str) -> str:
    """Return an XML tag without a namespace or namespace prefix."""

    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].casefold()


def _nfo_poster_values(content: str) -> list[str]:
    """Read only root-level media artwork, never nested cast thumbnails."""

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        # A malformed NFO must not make us fall back to a broad regex that can
        # accidentally select an actor/director headshot as the poster.
        return []

    values: list[str] = []

    def add(value: str | None) -> None:
        text = unescape(str(value or "")).strip()
        if text:
            values.append(text)

    # Kodi/Jellyfin variants commonly use either <poster> directly or
    # <art><poster>. Both are unambiguous media artwork.
    for child in root:
        if _xml_local_name(child.tag) == "poster":
            add(child.text)
    for child in root:
        if _xml_local_name(child.tag) != "art":
            continue
        for nested in child:
            if _xml_local_name(nested.tag) == "poster":
                add(nested.text)

    # A root-level <thumb aspect="poster"> is also valid. Ignore landscape
    # thumbs because they are backdrops, not portrait cover art.
    for child in root:
        if _xml_local_name(child.tag) != "thumb":
            continue
        aspect = str(child.attrib.get("aspect") or "").casefold()
        if aspect not in {"", "poster", "cover"}:
            continue
        add(child.text)
    return list(dict.fromkeys(values))


def _poster_data_uri(path: Path) -> str | None:
    """Embed a reasonably sized local poster without exposing NAS paths."""

    try:
        if not path.is_file() or path.stat().st_size > POSTER_MAX_BYTES:
            return None
        content = path.read_bytes()
    except OSError:
        return None
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if not mime_type.startswith("image/"):
        return None
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _poster_from_value(video_path: Path, value: Any) -> str | None:
    """Resolve a stored poster URL or local artwork reference."""

    raw_poster = str(value or "").strip()
    if not raw_poster:
        return None
    if raw_poster.startswith(("http://", "https://", "data:image/")):
        return raw_poster
    if raw_poster.startswith("api/"):
        raw_poster = f"/{raw_poster}"
    if raw_poster.startswith("/api/"):
        # MoviePilot's image proxy is a browser-relative URL. Treating it as a
        # local path makes every proxied TMDB poster fail on the NAS.
        return raw_poster
    local = Path(raw_poster).expanduser()
    if not local.is_absolute():
        local = video_path.parent / local
    return _poster_data_uri(local)


def _poster_for_video(
    video_path: Path,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Find a browser-safe poster from queue metadata or nearby artwork."""

    payload = payload or {}
    payload_source = str(payload.get("poster_source") or "").casefold()
    if payload_source == "moviepilot":
        poster = _poster_from_value(video_path, payload.get("poster_url"))
        if poster:
            return poster

    # Prefer a fresh, root-level NFO poster. Legacy queue rows have no source
    # marker and may contain a nested actor thumbnail in poster_url, so their
    # stored artwork must not be trusted until the next scan refreshes it.
    nfo_poster = _read_nfo_metadata(video_path).poster_url
    poster = _poster_from_value(video_path, nfo_poster)
    if poster:
        return poster

    if payload_source in {"nfo", "local"}:
        poster = _poster_from_value(video_path, payload.get("poster_url"))
        if poster:
            return poster

    season = payload.get("season")
    try:
        season_number = int(season) if season is not None else None
    except (TypeError, ValueError):
        season_number = None
    names: list[str] = []
    if season_number is not None:
        names.extend(
            (
                f"season{season_number:02d}-poster",
                f"season{season_number}-poster",
                f"season {season_number:02d}",
                f"season {season_number}",
            )
        )
    names.extend(POSTER_BASENAMES)
    directories = [video_path.parent]
    if (
        SEASON_DIR_PATTERN.match(video_path.parent.name)
        or "season" in video_path.parent.name.casefold()
    ):
        directories.append(video_path.parent.parent)
    for directory in directories:
        for name in names:
            for extension in POSTER_EXTENSIONS:
                poster = _poster_data_uri(directory / f"{name}{extension}")
                if poster:
                    return poster
    return None


def _media_category(video_path: Path, media_type: MediaType) -> str:
    """Return the display category used by the queue page."""

    if media_type is not MediaType.TV:
        return "movie"
    if any(
        marker in part.casefold()
        for part in video_path.parts
        for marker in ANIME_PATH_MARKERS
    ):
        return "anime"
    return "tv"


def _read_nfo_metadata(path: Path) -> NfoMetadata:
    """Read IDs, genres and country metadata from nearby NFO files."""

    in_season_dir = bool(
        SEASON_DIR_PATTERN.match(path.parent.name)
        or "season" in path.parent.name.casefold()
    )
    is_tv = bool(
        in_season_dir
        or EPISODE_PATTERN.search(path.stem)
        or CHINESE_EPISODE_PATTERN.search(path.stem)
    )
    show_dir = path.parent.parent if in_season_dir else path.parent
    if is_tv:
        candidates = [
            show_dir / "tvshow.nfo",
            path.parent / "season.nfo",
            path.parent / "tvshow.nfo",
            path.with_suffix(".nfo"),
        ]
    else:
        candidates = [
            path.with_suffix(".nfo"),
            path.parent / "movie.nfo",
            show_dir / "movie.nfo",
        ]
    metadata = NfoMetadata()
    for candidate in dict.fromkeys(candidates):
        try:
            content = candidate.read_text(encoding="utf-8", errors="ignore")[:262_144]
        except OSError:
            continue
        if metadata.tmdb_id is None:
            match = re.search(r"(?is)<tmdbid>\s*(\d+)\s*</tmdbid>", content)
            if match:
                metadata.tmdb_id = int(match.group(1))
        if metadata.imdb_id is None:
            match = re.search(
                r"(?is)<imdbid>\s*(tt\d{5,12})\s*</imdbid>", content
            )
            if match:
                metadata.imdb_id = match.group(1)
        if not metadata.genres:
            metadata.genres = _nfo_values(content, "genre")
        if not metadata.origin_countries:
            metadata.origin_countries = _nfo_values(
                content,
                "origin_country",
                "country",
                "production_country",
            )
        if not metadata.original_language:
            languages = _nfo_values(content, "original_language")
            metadata.original_language = languages[0] if languages else None
        allow_episode_poster = not (
            is_tv and candidate == path.with_suffix(".nfo")
        )
        if not metadata.poster_url and allow_episode_poster:
            for value in _nfo_poster_values(content):
                if value.startswith(("http://", "https://")):
                    metadata.poster_url = value
                    break
                local_poster = Path(value).expanduser()
                if not local_poster.is_absolute():
                    local_poster = candidate.parent / local_poster
                if local_poster.is_file():
                    metadata.poster_url = str(local_poster)
                    break
        if (
            metadata.tmdb_id is not None
            and metadata.imdb_id is not None
            and metadata.genres
            and metadata.origin_countries
            and metadata.original_language
            and metadata.poster_url
        ):
            break
    return metadata


def _nfo_ids(path: Path) -> tuple[int | None, str | None]:
    """Read common Kodi-style NFO identifiers when they are available."""

    metadata = _read_nfo_metadata(path)
    return metadata.tmdb_id, metadata.imdb_id


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
    nfo = _read_nfo_metadata(path)
    return DiscoveredVideo(
        path=path,
        title=title,
        original_title=original_title,
        year=year,
        tmdb_id=int(tmdb_match.group(1)) if tmdb_match else nfo.tmdb_id,
        imdb_id=nfo.imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        genres=nfo.genres,
        origin_countries=nfo.origin_countries,
        original_language=nfo.original_language,
        poster_url=nfo.poster_url,
        poster_source="nfo" if nfo.poster_url else None,
    )


def _path_prefix_matches(path: Path, prefix: str) -> bool:
    """Return whether a media path is inside a configured metadata prefix."""

    path_text = str(path).replace("\\", "/").rstrip("/").casefold()
    prefix_text = str(prefix).replace("\\", "/").rstrip("/").casefold()
    return bool(prefix_text) and (
        path_text == prefix_text or path_text.startswith(f"{prefix_text}/")
    )


def _normalized_title(value: Any) -> str:
    """Normalize titles for a conservative same-title metadata fallback."""

    text = TMDB_PATTERN.sub("", str(value or ""))
    text = YEAR_PATTERN.sub("", text)
    return "".join(character for character in text.casefold() if character.isalnum())


def _metadata_match_score(
    media: DiscoveredVideo,
    metadata: ScanMediaMetadata,
) -> int:
    """Score MoviePilot metadata so the most specific path wins."""

    score = 0
    for prefix in metadata.path_prefixes:
        if _path_prefix_matches(media.path, prefix):
            score = max(score, 10_000 + len(prefix))
    if metadata.tmdb_id and metadata.tmdb_id == media.tmdb_id:
        if metadata.media_type in (None, media.media_type):
            score = max(score, 5_000)
        else:
            score = max(score, 4_000)
    media_titles = {
        title
        for title in (
            _normalized_title(media.title),
            _normalized_title(media.original_title),
        )
        if title
    }
    metadata_titles = {
        title
        for title in (
            _normalized_title(metadata.title),
            _normalized_title(metadata.original_title),
        )
        if title
    }
    if media_titles.intersection(metadata_titles):
        if metadata.year and metadata.year == media.year:
            score = max(score, 2_000)
        elif metadata.media_type in (None, media.media_type):
            score = max(score, 1_000)
    return score


def _apply_scan_metadata(
    media: DiscoveredVideo,
    values: list[ScanMediaMetadata],
) -> bool:
    """Overlay MoviePilot metadata without weakening existing NFO evidence."""

    matches = [
        (score, metadata)
        for metadata in values
        if (score := _metadata_match_score(media, metadata)) > 0
    ]
    if not matches:
        return False
    _, metadata = max(matches, key=lambda item: item[0])
    if metadata.genres:
        media.genres = list(dict.fromkeys([*metadata.genres, *media.genres]))
    if metadata.origin_countries:
        media.origin_countries = list(metadata.origin_countries)
    if metadata.original_language:
        media.original_language = metadata.original_language
    if metadata.poster_url:
        media.poster_url = metadata.poster_url
        media.poster_source = "moviepilot"
    return True


def _region_key(value: Any) -> str:
    """Normalize a country code or localized country name."""

    token = str(value or "").strip().casefold()
    if not token:
        return ""
    for key, aliases in REGION_ALIASES.items():
        if token == key or token in aliases:
            return key
    return token


def _is_animation_media(
    media: DiscoveredVideo,
    path: Path,
) -> bool:
    """Return whether NFO metadata or the library path marks this as animation."""

    genre_tokens: set[str] = set()
    for value in media.genres:
        genre_tokens.update(
            token.strip().casefold()
            for token in re.split(r"[,/|]", str(value))
            if token.strip()
        )
    if genre_tokens.intersection(ANIME_GENRE_IDS):
        return True
    if any(
        marker in token
        for token in genre_tokens
        for marker in ANIME_GENRE_NAMES
    ):
        return True
    return any(
        marker in part.casefold()
        for part in path.parts
        for marker in ANIME_PATH_MARKERS
    )


def _path_matches_keywords(
    path: Path,
    keywords: Iterable[str],
) -> bool:
    """Return whether a path contains an explicitly configured keyword."""

    parts = [part.casefold() for part in path.parts]
    for raw_keyword in keywords:
        keyword = str(raw_keyword).strip().casefold()
        if not keyword:
            continue
        if "/" in keyword:
            if keyword in str(path).casefold():
                return True
        elif any(keyword in part for part in parts):
            return True
    return False


def is_domestic_anime(
    path: Path,
    media: DiscoveredVideo,
    regions: Iterable[str],
    path_keywords: Iterable[str],
) -> bool:
    """Identify domestic animation without guessing from a Chinese title alone."""

    if _path_matches_keywords(path, path_keywords):
        return True
    if not _is_animation_media(media, path):
        return False

    region_keys = {
        _region_key(token)
        for value in regions
        if str(value).strip()
        for token in re.split(r"[,/|]", str(value))
        if token.strip()
    }
    country_keys = {
        _region_key(token)
        for value in media.origin_countries
        if str(value).strip()
        for token in re.split(r"[,/|]", str(value))
        if token.strip()
    }
    if country_keys:
        return bool(region_keys.intersection(country_keys))

    language = str(media.original_language or "").strip().casefold()
    return (
        "cn" in region_keys
        and language in CHINESE_LANGUAGE_CODES
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
        self._manual_sessions: dict[str, ManualSearchSession] = {}

    def queue_status(self) -> dict[str, int]:
        """Return the persisted missing-subtitle queue summary."""

        return self._store.queue_status()

    def status(
        self,
        daily_download_limit: int,
        queue_limit: int = 30,
    ) -> dict[str, Any]:
        """Return live queue and local daily-quota state for the plugin page."""

        limit = max(1, int(daily_download_limit))
        used_today = self._store.downloads_today()
        return {
            "day": datetime.now().date().isoformat(),
            "quota": {
                "used": used_today,
                "limit": limit,
                "remaining": max(0, limit - used_today),
            },
            "queue": self._store.queue_status(),
            "queue_groups": self.queue_view(queue_limit).model_dump(mode="json"),
        }

    def queue_view(self, limit: int = 30) -> QueueView:
        """Return pending items grouped by movie or TV/anime season."""

        limit = max(1, min(int(limit), 100))
        queue_status = self._store.queue_status()
        entries = self._store.queue_entries(limit=QUEUE_ENTRY_SCAN_LIMIT)
        groups: dict[str, dict[str, Any]] = {}
        now = datetime.now(UTC)
        for entry in entries:
            payload = entry.payload
            try:
                media_type = MediaType(str(payload.get("media_type") or "movie"))
            except ValueError:
                media_type = MediaType.MOVIE
            title = str(payload.get("title") or Path(entry.video_path).stem)
            season = _optional_int(payload.get("season"))
            episode = _optional_int(payload.get("episode"))
            year = _optional_int(payload.get("year"))
            tmdb_id = _optional_int(payload.get("tmdb_id"))
            video = Path(entry.video_path)
            if media_type is MediaType.TV:
                show_dir = video.parent
                if (
                    SEASON_DIR_PATTERN.match(video.parent.name)
                    or "season" in video.parent.name.casefold()
                ):
                    show_dir = video.parent.parent
                key_source = (
                    f"tv|{show_dir}|{title.casefold()}|{year or ''}|"
                    f"{season if season is not None else ''}|{tmdb_id or ''}"
                )
            else:
                key_source = f"movie|{video}"
            group_id = hashlib.sha1(
                key_source.encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            group = groups.get(group_id)
            if group is None:
                group = {
                    "group_id": group_id,
                    "title": title,
                    "year": year,
                    "media_type": media_type,
                    "category": _media_category(video, media_type),
                    "season": season,
                    "tmdb_id": tmdb_id,
                    "poster": _poster_for_video(video, payload),
                    "missing_languages": [],
                    "item_count": 0,
                    "items": [],
                }
                groups[group_id] = group
            elif not group["poster"]:
                group["poster"] = _poster_for_video(video, payload)
            for language in payload.get("languages") or []:
                try:
                    normalized = SubtitleLanguage(str(language))
                except ValueError:
                    continue
                if normalized not in group["missing_languages"]:
                    group["missing_languages"].append(normalized)
            next_attempt = _parse_datetime(entry.next_attempt_at)
            group["items"].append(
                QueueItemView(
                    video_path=entry.video_path,
                    filename=video.name,
                    title=title,
                    year=year,
                    media_type=media_type,
                    season=season,
                    episode=episode,
                    languages=[
                        SubtitleLanguage(str(language))
                        for language in (payload.get("languages") or [])
                        if str(language)
                        in {
                            SubtitleLanguage.SIMPLIFIED.value,
                            SubtitleLanguage.TRADITIONAL.value,
                        }
                    ],
                    attempts=entry.attempts,
                    queued_at=entry.queued_at,
                    next_attempt_at=entry.next_attempt_at,
                    last_error=entry.last_error,
                    deferred=bool(next_attempt and next_attempt > now),
                )
            )
            group["item_count"] += 1

        ordered_groups = sorted(
            groups.values(),
            key=lambda group: (
                str(group["title"]).casefold(),
                int(group["season"] or 0),
                str(group["group_id"]),
            ),
        )
        for group in ordered_groups:
            group["items"].sort(
                key=lambda item: (
                    int(item.season or 0),
                    int(item.episode or 0),
                    item.filename.casefold(),
                )
            )
        limited = ordered_groups[:limit]
        for group in limited:
            item_count = int(group["item_count"])
            group["items"] = group["items"][:QUEUE_MAX_ITEMS_PER_GROUP]
            group["items_truncated"] = item_count > len(group["items"])
        return QueueView(
            total=queue_status["total"],
            due=queue_status["due"],
            deferred=queue_status["deferred"],
            groups=[QueueGroupView.model_validate(group) for group in limited],
            truncated=len(ordered_groups) > len(limited)
            or len(entries) >= QUEUE_ENTRY_SCAN_LIMIT,
        )

    def _cleanup_manual_sessions(self) -> None:
        """Discard expired candidate references."""

        now = datetime.now(UTC)
        expired = [
            search_id
            for search_id, session in self._manual_sessions.items()
            if session.expires_at <= now
        ]
        for search_id in expired:
            self._manual_sessions.pop(search_id, None)

    async def manual_search(
        self,
        request: ManualSearchRequest,
    ) -> ManualSearchResult:
        """Search all configured providers for one queued video."""

        self._cleanup_manual_sessions()
        video = ensure_path_allowed(
            Path(request.video_path).expanduser(),
            self._service.settings.media_root_paths,
        )
        if not video.is_file():
            raise ValueError("Video file does not exist")
        entry = self._store.get_queue_item(video)
        if entry is None:
            raise ValueError("该视频不在待处理字幕队列中，请重新扫描")
        process_request = ProcessRequest.model_validate(entry.payload)
        provider_order = request.provider_order or process_request.provider_order
        candidates, errors = await self._service.search(
            # Keep ProcessRequest so fingerprint providers can still see
            # video_path while searching for a manual selection.
            process_request,
            provider_order=provider_order,
            provider_failover=False,
        )
        search_id = uuid.uuid4().hex
        selection_map: dict[str, SubtitleCandidate] = {}
        public_candidates: list[ManualCandidateView] = []
        for rank, candidate in enumerate(candidates[:100], start=1):
            selection_id = hashlib.sha256(
                (
                    f"{candidate.provider}\0{candidate.candidate_id}\0"
                    f"{candidate.release_name or ''}\0{rank}"
                ).encode("utf-8", errors="ignore")
            ).hexdigest()[:20]
            selection_map[selection_id] = candidate
            public_candidates.append(
                ManualCandidateView(
                    selection_id=selection_id,
                    provider=candidate.provider,
                    title=candidate.title,
                    release_name=candidate.release_name,
                    language=candidate.language,
                    format=candidate.format,
                    season=candidate.season,
                    episode=candidate.episode,
                    year=candidate.year,
                    rank=rank,
                    score=candidate.score,
                    download_count=candidate.download_count,
                    machine_translated=candidate.machine_translated,
                )
            )
        expires_at = datetime.now(UTC) + timedelta(minutes=30)
        self._manual_sessions[search_id] = ManualSearchSession(
            search_id=search_id,
            request=process_request,
            candidates=selection_map,
            expires_at=expires_at,
        )
        return ManualSearchResult(
            search_id=search_id,
            video_path=str(video),
            title=process_request.title,
            year=process_request.year,
            media_type=process_request.media_type,
            season=process_request.season,
            episode=process_request.episode,
            poster=_poster_for_video(video, entry.payload),
            languages=process_request.languages,
            candidates=public_candidates,
            errors=errors,
            expires_at=expires_at.isoformat(),
        )

    async def manual_download(
        self,
        request: ManualDownloadRequest,
    ) -> ProcessResult:
        """Install one explicitly selected subtitle candidate."""

        self._cleanup_manual_sessions()
        session = self._manual_sessions.get(request.search_id)
        if session is None:
            raise ValueError("手动搜索已过期，请重新搜索字幕")
        candidate = session.candidates.get(request.selection_id)
        if candidate is None:
            raise ValueError("所选字幕候选已失效，请重新搜索")
        language = request.language or candidate.language
        if language is None:
            raise ValueError("该字幕未标注语言，请明确选择简体或繁体中文")
        if request.daily_download_limit is not None:
            if self._store.downloads_today() >= request.daily_download_limit:
                raise ValueError("今日字幕下载额度已用完，请明天再试")
        result = await self._service.install_candidate(
            Path(session.request.video_path),
            candidate,
            language,
            overwrite=request.overwrite,
        )
        if result.status == "completed" and result.downloaded:
            self._store.record_downloads(len(result.downloaded))
            self._store.remove_queue_item(Path(session.request.video_path))
            self._store.mark_checked(Path(session.request.video_path), "completed")
            self._manual_sessions.pop(request.search_id, None)
        return result

    async def scan(self, request: ScanRequest) -> ScanResult:
        """Serialize full scans so concurrent triggers cannot exceed daily quota."""

        async with self._scan_lock:
            return await self._scan_locked(request)

    async def _scan_locked(self, request: ScanRequest) -> ScanResult:
        """Inventory every video before downloading a quota-bounded queue batch."""

        removed_excluded_paths = 0
        if not request.dry_run:
            self._store.remove_unsupported_media()
            removed_excluded_paths = self._store.remove_excluded_paths(
                request.exclude_paths
            )

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
                excluded_configured_paths=removed_excluded_paths,
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
            excluded_configured_paths=removed_excluded_paths,
            downloads_today=self._store.downloads_today(),
            daily_download_limit=request.daily_download_limit,
            dry_run=request.dry_run,
            errors=errors,
        )
        items_by_path: dict[str, ScanItemResult] = {}

        # Phase 1: inspect every file locally. This never queries a provider.
        for path, root in ordered:
            discovered_media = parse_media_path(path, root)
            if _apply_scan_metadata(discovered_media, request.media_metadata):
                result.metadata_applied += 1
            if _is_animation_media(discovered_media, path):
                result.metadata_animation += 1
            if (
                request.exclude_domestic_anime
                and is_domestic_anime(
                    path,
                    discovered_media,
                    request.domestic_anime_regions,
                    request.domestic_anime_path_keywords,
                )
            ):
                result.excluded_domestic_anime += 1
                if not request.dry_run:
                    self._store.remove_queue_item(path)
                continue
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
                    poster_url=discovered_media.poster_url,
                    poster_source=discovered_media.poster_source,
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
