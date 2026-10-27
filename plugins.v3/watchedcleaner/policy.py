"""Safety and normalization helpers shared by the scanner and plugin APIs."""

from __future__ import annotations

import hashlib
import posixpath
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

CATEGORIES = ("movie", "tv", "anime")
TV_MODES = ("episode", "series")
DEFAULT_ANIME_KEYWORDS = (
    "动漫",
    "动画",
    "番剧",
    "国漫",
    "日漫",
    "anime",
    "animation",
)


def as_bool(value: Any, default: bool = False) -> bool:
    """Parse common form and API boolean values without treating 'false' as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Return an integer constrained to an explicit safe range."""
    try:
        return min(max(int(value), minimum), maximum)
    except (TypeError, ValueError):
        return default


def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Return a float constrained to an explicit safe range."""
    try:
        return min(max(float(value), minimum), maximum)
    except (TypeError, ValueError):
        return default


def split_values(value: Any) -> list[str]:
    """Split a textarea, comma-separated string, or iterable into unique values."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[\n,，;；]+", value)
    elif isinstance(value, Iterable):
        candidates = list(value)
    else:
        candidates = [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_categories(value: Any) -> list[str]:
    """Return a valid category subset, falling back to all categories."""
    selected = [item.lower() for item in split_values(value)]
    result = [item for item in CATEGORIES if item in selected]
    return result or list(CATEGORIES)


def normalize_tv_mode(value: Any) -> str:
    """Return a supported TV deletion mode."""
    mode = str(value or "episode").strip().lower()
    return mode if mode in TV_MODES else "episode"


def parse_timestamp(value: Any) -> datetime | None:
    """Parse ISO-8601, Emby fractional dates, or Unix timestamps into UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return parse_timestamp(float(text))
        # Emby commonly emits seven fractional digits, which fromisoformat does
        # not accept on every supported Python runtime.
        text = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)", r"\1", text)
        text = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_timestamp(value: Any) -> str:
    """Return a stable UTC ISO timestamp or an empty string."""
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed else ""


def newest_timestamp(values: Sequence[Any]) -> str:
    """Return the newest parsable timestamp in a sequence."""
    parsed = [item for item in (parse_timestamp(value) for value in values) if item]
    return max(parsed).isoformat() if parsed else ""


def days_since(value: Any, now: datetime | None = None) -> float | None:
    """Return elapsed days, clamping future timestamps to zero."""
    parsed = parse_timestamp(value)
    if not parsed:
        return None
    current = now or datetime.now(timezone.utc)
    return max((current - parsed).total_seconds() / 86400, 0.0)


def percentage(record: Mapping[str, Any]) -> float:
    """Return a bounded playback percentage from a normalized record."""
    try:
        value = float(record.get("percentage") or 0)
    except (TypeError, ValueError):
        value = 0.0
    return min(max(value, 0.0), 100.0)


def is_watched(record: Mapping[str, Any], threshold: float) -> bool:
    """Treat explicit played state or a reached progress threshold as watched."""
    return as_bool(record.get("played")) or percentage(record) >= threshold


def _is_windows_path(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:/", path) or path.startswith("//"))


def normalize_path(path: Any) -> str:
    """Normalize a server-reported path without touching the local filesystem."""
    text = str(path or "").strip().replace("\\", "/")
    if not text or "\x00" in text:
        return ""
    normalized = posixpath.normpath(text)
    if normalized == ".":
        return ""
    if _is_windows_path(normalized):
        return normalized.casefold()
    return normalized


def _is_too_broad_root(root: str) -> bool:
    """Reject roots that would effectively whitelist the whole filesystem."""
    stripped = root.rstrip("/")
    return stripped in {"", "/"} or bool(re.fullmatch(r"[a-z]:", stripped, re.IGNORECASE))


def valid_roots(roots: Sequence[Any]) -> list[str]:
    """Return normalized roots that scope deletion to a real subtree."""
    result: list[str] = []
    for root in roots:
        normalized = normalize_path(root)
        if normalized and not _is_too_broad_root(normalized):
            result.append(normalized)
    return result


def path_allowed(path: Any, roots: Sequence[str]) -> tuple[bool, str]:
    """Check a path against normalized, explicitly scoped roots."""
    candidate = normalize_path(path)
    if not candidate:
        return False, "媒体服务器未返回文件路径"
    scoped_roots = valid_roots(roots)
    if not scoped_roots:
        return False, "未配置有效的删除路径白名单"
    for root in scoped_roots:
        if candidate.startswith(root + "/"):
            return True, ""
    return False, "文件路径不在删除白名单内"


def paths_allowed(paths: Sequence[Any], roots: Sequence[str]) -> tuple[bool, str]:
    """Require every file represented by a candidate to be inside the whitelist."""
    unique_paths = [
        str(path).strip()
        for path in paths
        if path is not None and str(path).strip()
    ]
    if not unique_paths:
        return False, "媒体服务器未返回文件路径"
    for path in unique_paths:
        allowed, reason = path_allowed(path, roots)
        if not allowed:
            return False, reason
    return True, ""


def classify_category(
    base_category: str,
    library_name: str,
    record: Mapping[str, Any],
    anime_keywords: Sequence[str],
) -> str:
    """Classify anime by library, genre, title, or path keywords."""
    if base_category not in {"movie", "tv"}:
        base_category = "tv"
    genres = record.get("genres") or []
    text_parts = [
        library_name,
        str(record.get("title") or ""),
        str(record.get("series_title") or ""),
        " ".join(str(item) for item in genres),
        " ".join(str(item) for item in (record.get("paths") or [])),
    ]
    haystack = " ".join(text_parts).casefold()
    if any(keyword.casefold() in haystack for keyword in anime_keywords if keyword):
        return "anime"
    return base_category


def record_block_reason(record: Mapping[str, Any], options: Mapping[str, Any]) -> str:
    """Return why a normalized record must not be deleted, or an empty string."""
    threshold = bounded_float(options.get("watched_threshold"), 90.0, 1.0, 100.0)
    protect_favorites = as_bool(options.get("protect_favorites"), True)
    min_played_days = bounded_int(options.get("min_days_since_played"), 7, 0, 3650)
    min_added_days = bounded_int(options.get("min_days_since_added"), 7, 0, 3650)
    roots = split_values(options.get("allowed_roots"))
    now = datetime.now(timezone.utc)

    if protect_favorites and as_bool(record.get("favorite")):
        return "收藏条目受保护"

    episodes = list(record.get("episodes") or [])
    if record.get("kind") == "series":
        if not episodes:
            return "未找到已入库分集，无法确认整剧已看完"
        unfinished = sum(1 for episode in episodes if not is_watched(episode, threshold))
        if unfinished:
            return f"仍有 {unfinished} 集未达到观看阈值"
        if protect_favorites and any(as_bool(episode.get("favorite")) for episode in episodes):
            return "至少一集已收藏，整剧删除受保护"
        paths = [
            path
            for episode in episodes
            for path in (episode.get("paths") or [])
        ]
        played_dates = [episode.get("last_played") for episode in episodes]
        added_dates = [episode.get("added_at") for episode in episodes]
    else:
        if not is_watched(record, threshold):
            return "尚未达到观看阈值"
        paths = list(record.get("paths") or [])
        played_dates = [record.get("last_played")]
        added_dates = [record.get("added_at")]

    latest_played = newest_timestamp(played_dates)
    if min_played_days > 0 and latest_played:
        elapsed = days_since(latest_played, now)
        if elapsed is not None and elapsed < min_played_days:
            return f"最近 {min_played_days} 天内播放过"

    latest_added = newest_timestamp(added_dates)
    if min_added_days > 0 and latest_added:
        elapsed = days_since(latest_added, now)
        if elapsed is not None and elapsed < min_added_days:
            return f"最近 {min_added_days} 天内入库"

    if not paths:
        return "媒体服务器未返回文件路径"
    if as_bool(options.get("require_allowed_roots"), True):
        allowed, reason = paths_allowed(paths, roots)
        if not allowed:
            return reason
    return ""


def candidate_key(server: str, kind: str, item_id: str) -> str:
    """Build an opaque key that cannot contain path or shell syntax."""
    raw = f"{server}\0{kind}\0{item_id}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a scanner record and normalize fields used by safety checks."""
    normalized = dict(record)
    normalized["kind"] = str(normalized.get("kind") or "movie")
    normalized["title"] = str(normalized.get("title") or "").strip()
    normalized["series_title"] = str(normalized.get("series_title") or "").strip()
    normalized["series_id"] = str(normalized.get("series_id") or "").strip()
    normalized["item_id"] = str(normalized.get("item_id") or "").strip()
    normalized["paths"] = split_values(normalized.get("paths"))
    normalized["genres"] = split_values(normalized.get("genres"))
    normalized["played"] = as_bool(normalized.get("played"))
    normalized["favorite"] = as_bool(normalized.get("favorite"))
    normalized["percentage"] = percentage(normalized)
    normalized["last_played"] = iso_timestamp(normalized.get("last_played"))
    normalized["added_at"] = iso_timestamp(normalized.get("added_at"))
    normalized["episodes"] = [
        normalize_record(episode)
        for episode in (normalized.get("episodes") or [])
        if isinstance(episode, Mapping)
    ]
    return normalized
