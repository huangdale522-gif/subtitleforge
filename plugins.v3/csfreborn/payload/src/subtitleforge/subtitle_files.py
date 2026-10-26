"""Subtitle discovery, archive handling and safe installation."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from .models import DownloadedAsset, SubtitleLanguage

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
SIMPLIFIED_MARKERS = ("zh-cn", "zh-hans", "chs", "chi.zh-cn", "简", "simplified")
TRADITIONAL_MARKERS = ("zh-tw", "zh-hant", "cht", "chi.zh-tw", "繁", "traditional")
GENERIC_CHINESE_MARKERS = (
    ".zh.",
    ".zh-",
    "_zh.",
    "_zh-",
    ".chi.",
    "_chi.",
    ".zho.",
    "_zho.",
    "中字",
    "中文",
    "简中",
    "繁中",
)
SIMPLIFIED_CHARS = set(
    "这为说时会过个们来学开关头门体国对发现后经么样点从当还没给让问听实际报长东动里种类华机产万话风电业处际识达罗马鱼鸟龙书"
)
TRADITIONAL_CHARS = set(
    "這為說時會過個們來學開關頭門體國對發現後經麼樣點從當還沒給讓問聽實際報長東動裡種類華機產萬話風電業處際識達羅馬魚鳥龍書"
)


def language_from_name(name: str) -> SubtitleLanguage | None:
    """Infer a language from a file name."""

    lowered = name.casefold()
    if any(marker in lowered for marker in TRADITIONAL_MARKERS):
        return SubtitleLanguage.TRADITIONAL
    if any(marker in lowered for marker in SIMPLIFIED_MARKERS):
        return SubtitleLanguage.SIMPLIFIED
    return None


def has_generic_chinese_marker(name: str) -> bool:
    """Return whether a subtitle name identifies Chinese without its script."""

    lowered = f".{name.casefold()}."
    return any(marker in lowered for marker in GENERIC_CHINESE_MARKERS)


def language_from_content(content: bytes) -> SubtitleLanguage | None:
    """Use a lightweight character-frequency heuristic on subtitle text."""

    sample = content[:512_000]
    text = sample.decode("utf-8", errors="ignore")
    if not text:
        for encoding in ("utf-16", "gb18030", "big5"):
            try:
                text = sample.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    simplified = sum(text.count(char) for char in SIMPLIFIED_CHARS)
    traditional = sum(text.count(char) for char in TRADITIONAL_CHARS)
    if traditional > simplified * 1.25 and traditional >= 2:
        return SubtitleLanguage.TRADITIONAL
    if simplified > traditional * 1.25 and simplified >= 2:
        return SubtitleLanguage.SIMPLIFIED
    return None


def ensure_path_allowed(path: Path, roots: list[Path]) -> Path:
    """Resolve a path and ensure it stays inside configured media roots."""

    resolved = path.expanduser().resolve()
    if not roots:
        raise ValueError("No media roots are configured")
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ValueError("Path is outside configured media roots")
    return resolved


def _subtitle_suffix(path: Path) -> str | None:
    """Return the subtitle extension, including compound temporary suffixes."""

    for suffix in path.suffixes:
        lowered = suffix.lower()
        if lowered in SUBTITLE_EXTENSIONS:
            return lowered
    return None


def scan_external_languages(video_path: Path) -> set[SubtitleLanguage]:
    """Find external subtitle languages belonging to a video."""

    found: set[SubtitleLanguage] = set()
    if not video_path.parent.exists():
        return found
    video_stem = video_path.stem.casefold()
    for candidate in video_path.parent.iterdir():
        if not candidate.is_file() or _subtitle_suffix(candidate) is None:
            continue
        if not candidate.name.casefold().startswith(video_stem):
            continue
        language = language_from_name(candidate.name)
        if language is not None:
            found.add(language)
            continue
        try:
            if candidate.stat().st_size <= 2_000_000:
                language = language_from_content(candidate.read_bytes())
        except OSError:
            language = None
        if language is not None:
            found.add(language)
        elif has_generic_chinese_marker(candidate.name):
            # A generic ``zh``/``chi`` tag cannot tell Simplified from
            # Traditional. Treat it as Chinese so the scanner does not spend
            # quota downloading a duplicate.
            found.update(
                {SubtitleLanguage.SIMPLIFIED, SubtitleLanguage.TRADITIONAL}
            )
    return found


async def probe_embedded_languages(
    video_path: Path, ffprobe_path: str = "ffprobe"
) -> set[SubtitleLanguage]:
    """Ask ffprobe for embedded subtitle stream languages."""

    try:
        process = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream_tags=language:stream_disposition=forced",
            "-of",
            "json",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    except (TimeoutError, FileNotFoundError, OSError):
        return set()
    try:
        payload = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
    except json.JSONDecodeError:
        return set()
    found: set[SubtitleLanguage] = set()
    for stream in payload.get("streams", []) if isinstance(payload, dict) else []:
        tags = stream.get("tags") if isinstance(stream, dict) else None
        code = str((tags or {}).get("language") or "").lower()
        if code in {"zh-cn", "zh-hans", "zho-hans", "chs", "chi-sim"}:
            found.add(SubtitleLanguage.SIMPLIFIED)
        elif code in {"zh-tw", "zh-hant", "zho-hant", "cht", "chi-tra", "zh-hk"}:
            found.add(SubtitleLanguage.TRADITIONAL)
        elif code in {"zh", "chi", "zho", "chinese"}:
            found.update(
                {SubtitleLanguage.SIMPLIFIED, SubtitleLanguage.TRADITIONAL}
            )
    return found


def select_subtitle(
    asset: DownloadedAsset,
    target_language: SubtitleLanguage,
    max_bytes: int,
) -> tuple[bytes, str]:
    """Extract a suitable subtitle from a raw file or ZIP archive."""

    if len(asset.content) > max_bytes:
        raise ValueError("Downloaded subtitle exceeds the configured size limit")
    if zipfile.is_zipfile(io.BytesIO(asset.content)):
        with zipfile.ZipFile(io.BytesIO(asset.content)) as archive:
            allowed: list[zipfile.ZipInfo] = []
            total_size = 0
            for info in archive.infolist():
                if (
                    info.is_dir()
                    or Path(info.filename).suffix.lower() not in SUBTITLE_EXTENSIONS
                ):
                    continue
                total_size += info.file_size
                if total_size > max_bytes:
                    raise ValueError(
                        "Subtitle archive exceeds the configured size limit"
                    )
                allowed.append(info)
            if not allowed:
                raise ValueError(
                    "Subtitle archive contains no supported subtitle files"
                )
            preferred = [
                info
                for info in allowed
                if language_from_name(info.filename) is target_language
            ]
            selected = max(preferred or allowed, key=lambda info: info.file_size)
            content = archive.read(selected)
            return content, Path(selected.filename).name
    suffix = Path(asset.file_name).suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        asset_name = Path(asset.file_name).name
        if not asset_name or asset_name in {".", ".."}:
            asset_name = "subtitle.srt"
        if not Path(asset_name).suffix:
            asset_name += ".srt"
        return asset.content, asset_name
    return asset.content, Path(asset.file_name).name


def install_subtitle(
    video_path: Path,
    content: bytes,
    source_name: str,
    language: SubtitleLanguage,
    overwrite: bool,
) -> tuple[Path, str]:
    """Atomically write a subtitle beside the video."""

    suffix = Path(source_name).suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        suffix = ".srt"
    language_suffix = (
        ".chi.zh-cn" if language is SubtitleLanguage.SIMPLIFIED else ".zh-tw"
    )
    destination = video_path.with_name(f"{video_path.stem}{language_suffix}{suffix}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Subtitle already exists: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=destination.suffix,
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination, suffix.lstrip(".")


def safe_name(value: str) -> str:
    """Return a conservative display/file name."""

    cleaned = re.sub(r"[^\w\u3400-\u9fff.\- ()]+", "_", value).strip()
    return cleaned[:180] or "subtitle"
