"""Resumable media-library discovery and subtitle completion."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

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
    "lost+found",
}
TRAILING_RELEASE_TOKENS = re.compile(
    r"(?i)(?:[ ._-]+(?:2160p|1080p|720p|480p|bluray|blu-ray|webrip|web-dl|"
    r"hdtv|remux|x264|x265|h264|h265|hevc|avc|aac|dts|proper|repack).*)$"
)


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


class ScanStore:
    """Small SQLite cursor so bounded scans eventually cover the whole library."""

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
    """Apply subtitle processing to a bounded, least-recently-checked batch."""

    def __init__(self, service: SubtitleService, data_dir: Path) -> None:
        self._service = service
        self._store = ScanStore(data_dir)

    async def scan(self, request: ScanRequest) -> ScanResult:
        """Scan configured roots and process up to ``max_files`` videos."""

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
            return ScanResult(
                status="not_found",
                discovered=0,
                dry_run=request.dry_run,
                errors=errors,
            )

        last_checked = self._store.last_checked()
        never_checked = interleave_by_media_directory(
            sorted(
                (item for item in discovered if str(item[0]) not in last_checked),
                key=lambda item: str(item[0]),
            )
        )
        previously_checked = interleave_by_media_directory(
            sorted(
                (item for item in discovered if str(item[0]) in last_checked),
                key=lambda item: last_checked[str(item[0])],
            )
        )
        selected = (never_checked + previously_checked)[: request.max_files]
        result = ScanResult(
            status="processing",
            discovered=len(discovered),
            remaining=max(0, len(discovered) - len(selected)),
            dry_run=request.dry_run,
            errors=errors,
        )
        for path, root in selected:
            discovered_media = parse_media_path(path, root)
            process_request = ProcessRequest(
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
                dry_run=request.dry_run,
                provider_order=request.provider_order,
            )
            try:
                processed = await self._service.process(process_request)
                status = processed.status
                item_errors = list(processed.errors)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop a scan
                status = "error"
                item_errors = [f"{type(exc).__name__}: {exc}"]
                processed = ProcessResult(
                    status="error",
                    video_path=str(path),
                    errors=item_errors,
                )
            result.checked += 1
            result.downloaded += len(processed.downloaded)
            result.items.append(
                ScanItemResult(
                    video_path=str(path),
                    title=discovered_media.title,
                    year=discovered_media.year,
                    media_type=discovered_media.media_type,
                    season=discovered_media.season,
                    episode=discovered_media.episode,
                    status=status,
                    existing_languages=processed.existing_languages,
                    missing_languages=processed.missing_languages,
                    downloaded=processed.downloaded,
                    errors=item_errors,
                )
            )
            if not request.dry_run:
                self._store.mark_checked(path, status)

        result.remaining = max(0, len(discovered) - result.checked)
        if any(item.status == "error" for item in result.items):
            result.status = "partial"
        elif result.downloaded:
            result.status = "completed"
        else:
            result.status = result.items[-1].status if result.items else "not_found"
        return result
