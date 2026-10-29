"""MoviePilot V2/V3 adapter for SubtitleForge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from fastapi import Body
from app.schemas.types import EventType, MediaType

try:
    from app.schemas.types import MessageType
    from app.sdk.events import Event, eventmanager
    from app.sdk.logging import logger
    from app.sdk.plugin import _PluginBase
except ImportError:
    from app.core.event import Event, eventmanager
    from app.log import logger
    from app.plugins import _PluginBase
    from app.schemas.types import NotificationType as MessageType


TMDB_PATH_PATTERN = re.compile(r"(?i)\{?\btmdbid\s*[=:]\s*(\d+)\}?")
EPISODE_PATH_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})(?:[^0-9]|$)"
)
EPISODE_ONLY_PATH_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])e(?:p)?(\d{1,3})(?:[^0-9]|$)"
)
CHINESE_EPISODE_PATH_PATTERN = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
SEASON_PATH_PATTERN = re.compile(r"(?i)^(?:season|s)\s*(\d{1,2})$")
IGNORED_MEDIA_DIRECTORIES = {
    "@eadir",
    "#recycle",
    "$recycle.bin",
    ".recycle",
    ".trash",
    "bdmv",
    "lost+found",
}
MAX_TMDB_METADATA_ITEMS = 500
TMDB_IMAGE_HOSTS = {
    "image.tmdb.org",
    "media.themoviedb.org",
    "themoviedb.org",
    "www.themoviedb.org",
}
TMDB_INFO_FIELDS = (
    "title",
    "name",
    "original_title",
    "original_name",
    "release_date",
    "first_air_date",
    "year",
    "original_language",
    "original_languages",
    "language",
    "languages",
    "genre_ids",
    "genres",
    "genre",
    "origin_country",
    "production_countries",
    "country",
    "countries",
    "country_codes",
    "countryOfOrigin",
    "poster_url",
    "poster_path",
    "poster",
    "cover",
)


class CSFReborn(_PluginBase):
    """Complete subtitles after transfers and during scheduled library scans."""

    plugin_name = "SubtitleForge 字幕补全"
    plugin_desc = (
        "接入 SubtitleBest、ASSRT、迅雷等中文字幕源，为电影、剧集和动漫自动补全字幕；"
        "队列按季度展示封面，并支持手动挑选字幕。"
    )
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.10.7"
    plugin_author = "SubtitleForge contributors"
    author_url = "https://github.com/"
    plugin_config_prefix = "csfreborn_"
    plugin_label = "字幕"
    plugin_order = 30
    auth_level = 1

    def __init__(self) -> None:
        """Initialize empty runtime state."""

        super().__init__()
        self._enabled = False
        self._service_url = "http://127.0.0.1:8600"
        self._api_token = ""
        self._media_roots = "/plex,/media"
        self._languages = ["zh-Hans", "zh-Hant"]
        self._overwrite = False
        self._dry_run = False
        self._notify = True
        self._timeout = 180
        self._manage_service = True
        self._service_dir = Path("/config/subtitleforge")
        self._provider_order = (
            "xunlei,assrt,subtitle_best,opensubtitles,custom,shooter"
        )
        self._subtitle_best_api_key = ""
        self._subtitle_best_header_token = "5akwmGAbuFWqgaZf9QwT"
        self._opensubtitles_api_key = ""
        self._opensubtitles_username = ""
        self._opensubtitles_password = ""
        self._assrt_token = ""
        self._xunlei_enabled = True
        self._shooter_enabled = False
        self._custom_provider_url = ""
        self._custom_provider_token = ""
        self._scan_enabled = False
        self._scan_paths = (
            "/plex/电影/外语电影,/plex/电视剧/欧美剧,/plex/电视剧/日韩剧,"
            "/plex/电视剧/纪录片,/plex/动漫"
        )
        self._scan_exclude_paths = "/plex/电影/华语电影,/plex/电视剧/国产剧"
        self._exclude_domestic_anime = True
        self._domestic_anime_regions = "CN,TW,HK"
        self._domestic_anime_path_keywords = (
            "国漫,国产,国产动漫,国产动画,国产番剧,国创"
        )
        self._scan_video_extensions = (
            ".mkv,.mp4,.avi,.mov,.wmv,.m4v,.ts,.m2ts,.webm"
        )
        self._scan_interval_hours = 24
        self._scan_max_files = 10
        self._scan_daily_download_limit = 10
        self._scan_dry_run = True
        self._scan_provider_order = ""
        self._scan_languages = ["zh-Hans"]
        self._managed_process: subprocess.Popen | None = None
        self._scan_running = False
        self._history_key = "download_history"
        self._latest_scan_key = "latest_scan"
        self._history_max = 30
        self._tmdb_metadata_cache: dict[str, dict[str, Any]] = {}
        self._tmdb_api_client: Any = None
        self._tmdb_api_import_failed = False

    def init_plugin(self, config: dict | None = None) -> None:
        """Load configuration; safe to call repeatedly."""

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._service_url = str(
            config.get("service_url") or "http://127.0.0.1:8600"
        ).rstrip("/")
        self._api_token = str(config.get("api_token") or "")
        self._media_roots = str(config.get("media_roots") or "/plex,/media")
        self._languages = list(config.get("languages") or ["zh-Hans", "zh-Hant"])
        self._overwrite = bool(config.get("overwrite", False))
        self._dry_run = bool(config.get("dry_run", False))
        self._notify = bool(config.get("notify", True))
        self._manage_service = bool(config.get("manage_service", True))
        self._service_dir = Path(
            str(config.get("service_dir") or "/config/subtitleforge")
        ).expanduser()
        self._provider_order = str(
            config.get("provider_order")
            or "xunlei,assrt,subtitle_best,opensubtitles,custom,shooter"
        )
        self._subtitle_best_api_key = str(config.get("subtitle_best_api_key") or "")
        self._subtitle_best_header_token = str(
            config.get("subtitle_best_header_token") or "5akwmGAbuFWqgaZf9QwT"
        )
        self._opensubtitles_api_key = str(
            config.get("opensubtitles_api_key") or ""
        )
        self._opensubtitles_username = str(
            config.get("opensubtitles_username") or ""
        )
        self._opensubtitles_password = str(
            config.get("opensubtitles_password") or ""
        )
        self._assrt_token = str(config.get("assrt_token") or "")
        self._xunlei_enabled = bool(config.get("xunlei_enabled", True))
        self._shooter_enabled = bool(config.get("shooter_enabled", False))
        self._custom_provider_url = str(config.get("custom_provider_url") or "")
        self._custom_provider_token = str(
            config.get("custom_provider_token") or ""
        )
        self._scan_enabled = bool(config.get("scan_enabled", False))
        self._scan_paths = str(config.get("scan_paths") or self._scan_paths)
        self._scan_exclude_paths = str(
            config.get("scan_exclude_paths") or self._scan_exclude_paths
        )
        self._exclude_domestic_anime = bool(
            config.get("exclude_domestic_anime", True)
        )
        self._domestic_anime_regions = str(
            config.get("domestic_anime_regions")
            or self._domestic_anime_regions
        )
        self._domestic_anime_path_keywords = str(
            config.get("domestic_anime_path_keywords")
            or self._domestic_anime_path_keywords
        )
        self._scan_video_extensions = str(
            config.get("scan_video_extensions") or self._scan_video_extensions
        )
        self._scan_dry_run = bool(config.get("scan_dry_run", True))
        self._scan_languages = list(
            config.get("scan_languages") or ["zh-Hans"]
        )
        self._scan_provider_order = str(
            config.get("scan_provider_order") or ""
        )
        try:
            self._scan_interval_hours = max(
                1, int(config.get("scan_interval_hours") or 24)
            )
        except (TypeError, ValueError):
            self._scan_interval_hours = 24
        try:
            self._scan_max_files = min(
                100, max(1, int(config.get("scan_max_files") or 10))
            )
        except (TypeError, ValueError):
            self._scan_max_files = 10
        try:
            self._scan_daily_download_limit = min(
                1000,
                max(
                    1,
                    int(config.get("scan_daily_download_limit") or 10),
                ),
            )
        except (TypeError, ValueError):
            self._scan_daily_download_limit = 10
        if not self._api_token:
            self._api_token = self._read_service_token()
        try:
            self._timeout = max(10, int(config.get("timeout") or 180))
        except (TypeError, ValueError):
            self._timeout = 180
        source_changed = self._sync_bundled_service()
        env_changed = self._write_provider_env()
        if self._enabled and self._manage_service:
            if source_changed or env_changed:
                self._stop_managed_service()
            self._ensure_managed_service()
        else:
            self._stop_managed_service()

    def get_state(self) -> bool:
        """Return whether the plugin is enabled."""

        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """Register a remote command for an on-demand library scan."""

        return [
            {
                "cmd": "/字幕扫描",
                "event": EventType.PluginAction,
                "desc": "立即扫描配置的媒体库并补全缺失字幕",
                "category": "字幕",
                "data": {"action": "subtitleforge_scan"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """Expose health, queue, and interactive subtitle endpoints."""

        return [
            {
                "path": "/health",
                "endpoint": self.health,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "检查 SubtitleForge 服务",
            },
            {
                "path": "/scan",
                "endpoint": self.run_manual_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即扫描媒体库",
            },
            {
                "path": "/queue",
                "endpoint": self.queue,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "读取按季度分组的字幕队列",
            },
            {
                "path": "/manual/search",
                "endpoint": self.manual_search,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "搜索一个队列视频的字幕候选",
            },
            {
                "path": "/manual/download",
                "endpoint": self.manual_download,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "下载用户明确选择的字幕候选",
            },
        ]

    def get_service(self) -> list[dict[str, Any]]:
        """Register the resumable library scan with MoviePilot."""

        if not self._enabled or not self._scan_enabled or not self._scan_paths:
            return []
        return [
            {
                "id": "SubtitleForgeLibraryScan",
                "name": "SubtitleForge 媒体库字幕扫描",
                "trigger": "interval",
                "func": self.scan_library,
                "kwargs": {"hours": self._scan_interval_hours},
            }
        ]

    async def run_manual_scan(self) -> dict[str, Any]:
        """Run one scan using the saved preview/download settings."""

        return await self.scan_library(manual=True)

    async def queue(self) -> dict[str, Any]:
        """Return the grouped queue for the plugin page."""

        try:
            result = await asyncio.to_thread(
                self._request_json,
                "GET",
                "/v1/queue?limit=30",
                None,
                20,
            )
            if not isinstance(result, dict):
                return {"status": "error"}
            return await asyncio.to_thread(self._enrich_queue_view, result)
        except Exception as exc:  # noqa: BLE001 - page request must degrade
            return {
                "status": "error",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }

    async def manual_search(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        """Search all providers for one queued video."""

        if not self._enabled:
            return {"status": "disabled", "message": "插件尚未启用"}
        data = payload or {}
        video_path = str(data.get("video_path") or "").strip()
        if not video_path:
            return {"status": "error", "message": "缺少视频路径"}
        try:
            result = await asyncio.to_thread(
                self._request_json,
                "POST",
                "/v1/manual/search",
                {
                    "video_path": video_path,
                    "provider_order": (
                        self._split_list(self._scan_provider_order)
                        if self._scan_provider_order
                        else None
                    ),
                },
                600,
            )
            return result if isinstance(result, dict) else {"status": "error"}
        except Exception as exc:  # noqa: BLE001 - keep API response structured
            logger.warning(f"SubtitleForge 手动搜索失败：{type(exc).__name__}: {exc}")
            return {
                "status": "error",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }

    async def manual_download(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        """Install one subtitle candidate explicitly chosen by the user."""

        if not self._enabled:
            return {"status": "disabled", "message": "插件尚未启用"}
        data = payload or {}
        search_id = str(data.get("search_id") or "").strip()
        selection_id = str(data.get("selection_id") or "").strip()
        if not search_id or not selection_id:
            return {"status": "error", "message": "缺少字幕候选信息"}
        request_payload: dict[str, Any] = {
            "search_id": search_id,
            "selection_id": selection_id,
            "overwrite": bool(data.get("overwrite", self._overwrite)),
            "daily_download_limit": self._scan_daily_download_limit,
        }
        language = data.get("language")
        if language in {"zh-Hans", "zh-Hant"}:
            request_payload["language"] = language
        try:
            result = await asyncio.to_thread(
                self._request_json,
                "POST",
                "/v1/manual/download",
                request_payload,
                600,
            )
            return result if isinstance(result, dict) else {"status": "error"}
        except Exception as exc:  # noqa: BLE001 - keep API response structured
            logger.warning(f"SubtitleForge 手动下载失败：{type(exc).__name__}: {exc}")
            return {
                "status": "error",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }

    @eventmanager.register(EventType.PluginAction)
    async def on_plugin_action(self, event: Event) -> None:
        """Run a scan when the MoviePilot remote command is invoked."""

        data = event.event_data if isinstance(event.event_data, dict) else {}
        if data.get("action") != "subtitleforge_scan":
            return
        result = await self.scan_library(manual=True, notify_completion=False)
        self.post_message(
            channel=data.get("channel"),
            mtype=MessageType.Plugin,
            title="SubtitleForge 字幕扫描",
            text=self._scan_notification_text(result),
            userid=data.get("user"),
        )

    async def scan_library(
        self,
        manual: bool = False,
        hours: int | None = None,
        notify_completion: bool = True,
    ) -> dict[str, Any]:
        """Scan the full library, then consume a quota-bounded download queue."""

        _ = hours
        if not self._enabled:
            return {"status": "disabled", "message": "插件尚未启用"}
        if self._scan_running:
            return {"status": "busy", "message": "已有媒体库扫描正在运行"}
        self._scan_running = True
        media_metadata = await asyncio.to_thread(self._build_scan_media_metadata)
        payload = {
            "paths": self._split_list(self._scan_paths),
            "exclude_paths": self._split_list(self._scan_exclude_paths),
            "exclude_domestic_anime": self._exclude_domestic_anime,
            "domestic_anime_regions": self._split_list(
                self._domestic_anime_regions
            ),
            "domestic_anime_path_keywords": self._split_list(
                self._domestic_anime_path_keywords
            ),
            "media_metadata": media_metadata,
            "video_extensions": self._split_list(self._scan_video_extensions),
            "languages": self._scan_languages or self._languages,
            "max_files": self._scan_max_files,
            "daily_download_limit": self._scan_daily_download_limit,
            "overwrite": self._overwrite,
            "dry_run": self._scan_dry_run,
            "provider_failover": True,
            "provider_order": (
                self._split_list(self._scan_provider_order)
                if self._scan_provider_order
                else None
            ),
        }
        try:
            result = await asyncio.to_thread(
                self._request_json,
                "POST",
                "/v1/scan",
                payload,
                3600,
            )
            result["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result["trigger"] = "manual" if manual else "schedule"
            result["metadata_enriched"] = len(media_metadata)
            self.save_data(
                self._latest_scan_key,
                json.dumps(result, ensure_ascii=False),
            )
            logger.info(
                "SubtitleForge 媒体库扫描完成："
                f"发现 {result.get('discovered', 0)}，"
                f"检查 {result.get('checked', 0)}，"
                f"下载 {result.get('downloaded', 0)}，"
                f"元数据命中 {result.get('metadata_applied', 0)}，"
                f"配置目录清理 {result.get('excluded_configured_paths', 0)}，"
                f"排除国产动漫 {result.get('excluded_domestic_anime', 0)}"
            )
            if (
                notify_completion
                and self._notify
                and int(result.get("downloaded") or 0) > 0
            ):
                self.post_message(
                    mtype=MessageType.Plugin,
                    title="SubtitleForge 已下载字幕",
                    text=self._scan_notification_text(result),
                )
            return result
        except Exception as exc:  # noqa: BLE001 - scheduler must survive failures
            message = f"{type(exc).__name__}: {exc}"
            logger.error(f"SubtitleForge 媒体库扫描失败：{message}")
            return {"status": "error", "errors": [message]}
        finally:
            self._scan_running = False

    def _build_scan_media_metadata(self) -> list[dict[str, Any]]:
        """Build browser-safe MoviePilot/TMDB metadata for scheduled scans."""

        targets = self._discover_scan_metadata_targets()
        metadata_items: list[dict[str, Any]] = []
        missing = 0
        for target in targets[:MAX_TMDB_METADATA_ITEMS]:
            metadata = self._tmdb_metadata(
                int(target["tmdb_id"]),
                str(target["media_type"]),
            )
            if not metadata:
                missing += 1
                continue
            item = dict(metadata)
            item["path_prefixes"] = sorted(target["path_prefixes"])
            metadata_items.append(item)
        country_count = sum(
            1 for item in metadata_items if item.get("origin_countries")
        )
        genre_count = sum(1 for item in metadata_items if item.get("genres"))
        language_count = sum(
            1 for item in metadata_items if item.get("original_language")
        )
        poster_count = sum(
            1 for item in metadata_items if item.get("poster_url")
        )
        if targets:
            logger.info(
                "SubtitleForge: 已为 "
                f"{len(metadata_items)}/{min(len(targets), MAX_TMDB_METADATA_ITEMS)} "
                "个媒体读取 MoviePilot/TMDB 元数据；"
                f"国家字段 {country_count} 条、题材字段 {genre_count} 条、"
                f"语言字段 {language_count} 条、海报 {poster_count} 条"
            )
        domestic_candidates = [
            item
            for item in metadata_items
            if self._is_domestic_anime(item, [])
        ]
        if domestic_candidates:
            samples = "; ".join(
                (
                    f"tmdb={item.get('tmdb_id')} "
                    f"title={item.get('title') or '-'} "
                    f"countries={item.get('origin_countries') or []}"
                )
                for item in domestic_candidates[:5]
            )
            logger.info(
                "SubtitleForge: TMDB 自动识别国产动漫 "
                f"{len(domestic_candidates)} 部；示例：{samples}"
            )
        incomplete = [
            item
            for item in metadata_items
            if not item.get("origin_countries") or not item.get("genres")
        ]
        if incomplete:
            samples = "; ".join(
                (
                    f"tmdb={item.get('tmdb_id')} "
                    f"title={item.get('title') or '-'} "
                    f"countries={item.get('origin_countries') or []} "
                    f"genres={item.get('genres') or []} "
                    f"language={item.get('original_language') or '-'}"
                )
                for item in incomplete[:5]
            )
            logger.warning(
                "SubtitleForge: TMDB 元数据字段不完整 "
                f"{len(incomplete)}/{len(metadata_items)} 条；示例：{samples}"
            )
        if missing:
            logger.warning(
                f"SubtitleForge: {missing} 个媒体未取得 TMDB 元数据，"
                "将继续使用 NFO 和路径规则"
            )
        return metadata_items

    def _discover_scan_metadata_targets(self) -> list[dict[str, Any]]:
        """Group scanned video files by TMDB identity before calling the service."""

        extensions = {
            value.casefold() if value.startswith(".") else f".{value.casefold()}"
            for value in self._split_list(self._scan_video_extensions)
        }
        excluded_paths = self._split_list(self._scan_exclude_paths)
        targets: dict[str, dict[str, Any]] = {}
        for raw_root in self._split_list(self._scan_paths):
            root = Path(raw_root).expanduser()
            if not root.exists():
                continue
            if root.is_file():
                candidates = [(root.parent, [], [root.name])]
            else:
                candidates = os.walk(root, followlinks=False)
            for current, directories, filenames in candidates:
                current_path = Path(current)
                if self._is_excluded_scan_path(current_path, excluded_paths):
                    directories[:] = []
                    continue
                directories[:] = [
                    name
                    for name in directories
                    if name.casefold() not in IGNORED_MEDIA_DIRECTORIES
                    and not self._is_excluded_scan_path(
                        current_path / name,
                        excluded_paths,
                    )
                ]
                for filename in filenames:
                    video = current_path / filename
                    if video.suffix.casefold() not in extensions:
                        continue
                    media_type = self._infer_scan_media_type(video)
                    tmdb_id = self._extract_scan_tmdb_id(
                        video,
                        root if root.is_dir() else root.parent,
                        media_type,
                    )
                    if not tmdb_id:
                        continue
                    key = f"{media_type}:{tmdb_id}"
                    target = targets.setdefault(
                        key,
                        {
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                            "path_prefixes": set(),
                        },
                    )
                    target["path_prefixes"].add(
                        str(self._scan_media_directory(video, media_type))
                    )
        return list(targets.values())

    @staticmethod
    def _is_excluded_scan_path(path: Path, excluded_paths: list[str]) -> bool:
        """Return whether a path is under one of the configured exclusions."""

        path_text = str(path).replace("\\", "/").rstrip("/").casefold()
        for raw_path in excluded_paths:
            excluded = str(raw_path).replace("\\", "/").rstrip("/").casefold()
            if excluded and (
                path_text == excluded or path_text.startswith(f"{excluded}/")
            ):
                return True
        return False

    @staticmethod
    def _infer_scan_media_type(video: Path) -> str:
        """Infer TV versus movie from conventional season and episode names."""

        if SEASON_PATH_PATTERN.match(video.parent.name):
            return "tv"
        if EPISODE_PATH_PATTERN.search(video.stem):
            return "tv"
        if EPISODE_ONLY_PATH_PATTERN.search(video.stem):
            return "tv"
        if CHINESE_EPISODE_PATH_PATTERN.search(video.stem):
            return "tv"
        return "movie"

    @staticmethod
    def _scan_media_directory(video: Path, media_type: str) -> Path:
        """Return the stable show or movie directory for metadata matching."""

        if media_type == "tv" and SEASON_PATH_PATTERN.match(video.parent.name):
            return video.parent.parent
        return video.parent

    def _extract_scan_tmdb_id(
        self,
        video: Path,
        root: Path,
        media_type: str,
    ) -> int | None:
        """Read a TMDB ID from the renamed path, falling back to nearby NFO."""

        try:
            relative_parts = video.relative_to(root).parts
        except ValueError:
            relative_parts = video.parts
        for value in (*reversed(relative_parts), video.stem):
            match = TMDB_PATH_PATTERN.search(str(value))
            if match:
                return int(match.group(1))

        media_dir = self._scan_media_directory(video, media_type)
        candidates = (
            [media_dir / "tvshow.nfo", video.with_suffix(".nfo")]
            if media_type == "tv"
            else [video.with_suffix(".nfo"), media_dir / "movie.nfo"]
        )
        for candidate in dict.fromkeys(candidates):
            try:
                content = candidate.read_text(
                    encoding="utf-8",
                    errors="ignore",
                )[:262_144]
            except OSError:
                continue
            for pattern in (
                r"(?is)<tmdbid>\s*(\d+)\s*</tmdbid>",
                (
                    r"(?is)<uniqueid[^>]*type=[\"']tmdb[\"'][^>]*>"
                    r"\s*(\d+)\s*</uniqueid>"
                ),
            ):
                match = re.search(pattern, content)
                if match:
                    return int(match.group(1))
        return None

    @staticmethod
    def _tmdb_metadata_key(tmdb_id: int, media_type: str) -> str:
        """Build a cache key that keeps movie and TV namespaces separate."""

        return f"{media_type}:{tmdb_id}"

    def _tmdb_metadata(
        self,
        tmdb_id: int,
        media_type: str,
    ) -> dict[str, Any] | None:
        """Return sanitized TMDB metadata suitable for the local service."""

        key = self._tmdb_metadata_key(tmdb_id, media_type)
        if key in self._tmdb_metadata_cache:
            return dict(self._tmdb_metadata_cache[key])
        info = self._fetch_tmdb_info(tmdb_id, media_type)
        if not info:
            return None
        metadata = self._sanitize_tmdb_metadata(info, tmdb_id, media_type)
        if not metadata:
            return None
        self._tmdb_metadata_cache[key] = metadata
        return dict(metadata)

    def _fetch_tmdb_info(self, tmdb_id: int, media_type: str) -> Any:
        """Use MoviePilot's chain first, then the TMDB API compatibility path."""

        mtype = self._tmdb_media_type(media_type)
        chain = getattr(self, "chain", None)
        tmdb_info = getattr(chain, "tmdb_info", None)
        primary = None
        if callable(tmdb_info):
            try:
                primary = tmdb_info(tmdbid=tmdb_id, mtype=mtype) or None
            except Exception as exc:  # noqa: BLE001 - fall back to TmdbApi
                logger.debug(
                    "SubtitleForge: MoviePilot chain TMDB 查询失败 "
                    f"{tmdb_id}: {type(exc).__name__}: {exc}"
                )
        if primary is not None and not self._tmdb_info_needs_fallback(primary):
            return primary
        client = self._get_tmdb_api_client()
        if client is None:
            return primary
        fallback = None
        try:
            fallback = client.get_info(mtype=mtype, tmdbid=tmdb_id) or None
        except Exception as exc:  # noqa: BLE001 - one lookup must not stop a scan
            logger.debug(
                f"SubtitleForge: TMDB {tmdb_id} 查询失败："
                f"{type(exc).__name__}: {exc}"
            )
        return self._merge_tmdb_info(primary, fallback)

    @classmethod
    def _tmdb_info_needs_fallback(cls, info: Any) -> bool:
        """Return whether a TMDB response lacks fields needed by the UI."""

        if info is None:
            return True
        return not all(
            (
                cls._metadata_values(
                    info,
                    ("title", "name", "original_title", "original_name"),
                ),
                cls._metadata_values(
                    info,
                    ("genre_ids", "genres", "genre"),
                ),
                cls._metadata_values(
                    info,
                    (
                        "origin_country",
                        "production_countries",
                        "country",
                        "countries",
                        "country_codes",
                        "countryOfOrigin",
                    ),
                ),
                cls._media_poster_url(info),
            )
        )

    @classmethod
    def _merge_tmdb_info(cls, primary: Any, fallback: Any) -> Any:
        """Merge a chain response with the native TMDB compatibility result."""

        if primary is None:
            return fallback
        if fallback is None:
            return primary
        merged: dict[str, Any] = {}
        for name in TMDB_INFO_FIELDS:
            primary_value = cls._field(primary, name)
            if cls._metadata_has_value(primary_value):
                merged[name] = primary_value
                continue
            fallback_value = cls._field(fallback, name)
            if cls._metadata_has_value(fallback_value):
                merged[name] = fallback_value
        return merged or fallback

    @staticmethod
    def _metadata_has_value(value: Any) -> bool:
        """Return whether a metadata field contains usable content."""

        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Mapping):
            return bool(value)
        if isinstance(value, (list, tuple, set)):
            return bool(value)
        return bool(value)

    def _get_tmdb_api_client(self) -> Any:
        """Lazily create a MoviePilot TmdbApi client for older hosts."""

        if self._tmdb_api_client is not None:
            return self._tmdb_api_client
        if self._tmdb_api_import_failed:
            return None
        try:
            try:
                from app.modules.themoviedb.tmdbapi import TmdbApi
            except ImportError:
                from app.modules.themoviedb import TmdbApi
            self._tmdb_api_client = TmdbApi()
        except Exception as exc:  # noqa: BLE001 - optional compatibility path
            self._tmdb_api_import_failed = True
            logger.warning(
                "SubtitleForge: 当前 MoviePilot 无法加载 TMDB 元数据接口，"
                f"将仅使用 NFO/路径规则：{type(exc).__name__}: {exc}"
            )
            return None
        return self._tmdb_api_client

    @staticmethod
    def _tmdb_media_type(media_type: str) -> Any:
        """Return MoviePilot's MediaType enum for a canonical media type."""

        return MediaType.MOVIE if media_type == "movie" else MediaType.TV

    @classmethod
    def _sanitize_tmdb_metadata(
        cls,
        info: Any,
        tmdb_id: int,
        media_type: str,
    ) -> dict[str, Any]:
        """Keep only safe, useful fields from a MoviePilot/TMDB response."""

        title_values = cls._metadata_values(
            info,
            (
                "title",
                "name",
                "original_title",
                "original_name",
            ),
        )
        date_values = cls._metadata_values(
            info,
            ("release_date", "first_air_date", "year"),
        )
        year = None
        for value in date_values:
            match = re.search(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)", value)
            if match:
                year = int(match.group(1))
                break
        language_values = cls._metadata_values(
            info,
            (
                "original_language",
                "original_languages",
                "language",
                "languages",
            ),
        )
        original_title_values = cls._metadata_values(
            info,
            ("original_title", "original_name"),
        )
        return {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": title_values[0] if title_values else None,
            "original_title": (
                original_title_values[0] if original_title_values else None
            ),
            "year": year,
            "genres": cls._metadata_values(
                info,
                ("genre_ids", "genres", "genre"),
            ),
            "origin_countries": cls._metadata_values(
                info,
                (
                    "origin_country",
                    "production_countries",
                    "country",
                    "countries",
                    "country_codes",
                    "countryOfOrigin",
                ),
            ),
            "original_language": (
                language_values[0] if language_values else None
            ),
            "poster_url": cls._media_poster_url(info),
        }

    @staticmethod
    def _moviepilot_settings() -> Any:
        """Load MoviePilot settings across V2, V3 and compatibility layouts."""

        for module_name in (
            "app.runtime.config",
            "app.core.config",
            "app.sdk.config",
        ):
            try:
                module = __import__(module_name, fromlist=["settings"])
                settings = getattr(module, "settings", None)
                if settings is not None:
                    return settings
            except (ImportError, AttributeError):
                continue
        return None

    @classmethod
    def _tmdb_image_url(cls, path: str) -> str:
        """Build a TMDB image URL, honoring MoviePilot's configured proxy."""

        value = str(path or "").strip()
        settings = cls._moviepilot_settings()
        if value.startswith(("http://", "https://")):
            source_url = value
        else:
            source_url = ""
            builder = getattr(settings, "TMDB_IMAGE_URL", None)
            if callable(builder):
                try:
                    built = builder(value, "w500")
                    if isinstance(built, str) and built.strip():
                        source_url = built.strip()
                except Exception:  # noqa: BLE001 - use the public fallback
                    pass
            if not source_url:
                domain = str(
                    getattr(settings, "TMDB_IMAGE_DOMAIN", "") or ""
                ).strip()
                if domain:
                    if not domain.startswith(("http://", "https://")):
                        domain = f"https://{domain}"
                    source_url = (
                        f"{domain.rstrip('/')}/t/p/w500/"
                        f"{value.lstrip('/')}"
                    )
                else:
                    source_url = (
                        "https://image.tmdb.org/t/p/w500/"
                        f"{value.lstrip('/')}"
                    )
        host = (urlsplit(source_url).hostname or "").casefold()
        if host not in TMDB_IMAGE_HOSTS:
            return source_url
        api_prefix = str(
            getattr(settings, "API_V1_STR", "/api/v1") or "/api/v1"
        ).rstrip("/")
        return (
            f"{api_prefix}/system/img/1?"
            f"imgurl={quote(source_url, safe='')}"
        )

    def _enrich_queue_view(self, result: dict[str, Any]) -> dict[str, Any]:
        """Fill missing queue posters and hide known domestic anime groups."""

        queue_groups = result.get("queue_groups")
        if not isinstance(queue_groups, dict):
            return result
        groups = queue_groups.get("groups")
        if not isinstance(groups, list):
            return result

        visible_groups: list[dict[str, Any]] = []
        removed_total = removed_due = removed_deferred = 0
        for group in groups:
            if not isinstance(group, dict):
                continue
            items = [
                item
                for item in (group.get("items") or [])
                if isinstance(item, dict)
            ]
            media_type = str(group.get("media_type") or "movie")
            tmdb_id = self._optional_int(group.get("tmdb_id"))
            if tmdb_id is not None and tmdb_id < 1:
                tmdb_id = None
            if tmdb_id is None:
                for item in items:
                    match = TMDB_PATH_PATTERN.search(
                        str(item.get("video_path") or "")
                    )
                    if match:
                        tmdb_id = int(match.group(1))
                        break
            current_poster = self._media_poster_url(
                {"poster_url": group.get("poster")}
            )
            if current_poster:
                group["poster"] = current_poster
            metadata = (
                self._tmdb_metadata(tmdb_id, media_type)
                if tmdb_id is not None
                else None
            )
            if metadata:
                if metadata.get("poster_url"):
                    group["poster"] = metadata["poster_url"]
                paths = [
                    str(item.get("video_path") or "")
                    for item in items
                    if item.get("video_path")
                ]
                if (
                    self._exclude_domestic_anime
                    and self._is_domestic_anime(metadata, paths)
                ):
                    item_count = int(group.get("item_count") or len(items))
                    visible_due = sum(
                        1 for item in items if not item.get("deferred")
                    )
                    visible_deferred = sum(
                        1 for item in items if item.get("deferred")
                    )
                    removed_total += item_count
                    removed_due += visible_due + max(
                        0, item_count - len(items)
                    )
                    removed_deferred += visible_deferred
                    continue
            visible_groups.append(group)

        if removed_total:
            queue_groups["groups"] = visible_groups
            for container in (result.get("queue"), queue_groups):
                if not isinstance(container, dict):
                    continue
                for name, removed in (
                    ("total", removed_total),
                    ("due", removed_due),
                    ("deferred", removed_deferred),
                ):
                    if name in container:
                        container[name] = max(
                            0,
                            int(container.get(name) or 0) - removed,
                        )
            result["excluded_in_queue"] = removed_total
        return result

    @staticmethod
    def _scan_notification_text(result: dict[str, Any]) -> str:
        """Format downloaded subtitle files for Telegram or another message channel."""

        lines = [
            (
                f"全库发现 {result.get('discovered', 0)} 个视频，"
                f"检查 {result.get('checked', 0)} 个，"
                f"已有 {result.get('existing', 0)} 个，"
                f"队列剩余 {result.get('queue_remaining', 0)} 个。"
            ),
            (
                f"本轮下载 {result.get('downloaded', 0)} 条，"
                f"今日额度 {result.get('downloads_today', 0)}/"
                f"{result.get('daily_download_limit', 0)}。"
            ),
        ]
        excluded = int(result.get("excluded_domestic_anime") or 0)
        if excluded:
            lines.append(f"已按规则排除国产动漫 {excluded} 个。")
        configured_excluded = int(
            result.get("excluded_configured_paths") or 0
        )
        if configured_excluded:
            lines.append(
                f"已从队列清理配置排除目录中的视频 {configured_excluded} 个。"
            )
        downloaded: list[str] = []
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            for subtitle in item.get("downloaded") or []:
                if not isinstance(subtitle, dict):
                    continue
                filename = Path(str(subtitle.get("path") or "")).name
                episode = ""
                if item.get("season") is not None and item.get("episode") is not None:
                    episode = (
                        f" S{int(item['season']):02d}E{int(item['episode']):02d}"
                    )
                downloaded.append(
                    f"· {item.get('title', '未知')}{episode} "
                    f"[{subtitle.get('language', '')}/"
                    f"{subtitle.get('provider', '')}] {filename}"
                )
        if downloaded:
            lines.append("已下载文件：")
            lines.extend(downloaded[:20])
            if len(downloaded) > 20:
                lines.append(f"……另有 {len(downloaded) - 20} 条未展开")
        return "\n".join(lines)

    async def health(self) -> dict[str, Any]:
        """Check the configured SubtitleForge service."""

        try:
            return await asyncio.to_thread(self._request_json, "GET", "/healthz", None)
        except Exception as exc:  # noqa: BLE001 - API should return a useful error
            return {"status": "error", "error": str(exc)}

    def _ensure_managed_service(self) -> None:
        """Start the local SubtitleForge process once when requested."""

        if self._service_healthy():
            return
        source_dir = self._service_dir / "src"
        app_file = source_dir / "subtitleforge" / "api.py"
        if not app_file.is_file():
            logger.error(
                f"SubtitleForge 服务目录不完整，未找到 {app_file}；"
                "请检查 service_dir 配置"
            )
            return
        log_dir = self._service_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source_dir)
        environment["PYTHONUNBUFFERED"] = "1"
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "subtitleforge.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8600",
        ]
        with (log_dir / "subtitleforge.log").open("ab", buffering=0) as log_file:
            self._managed_process = subprocess.Popen(
                command,
                cwd=self._service_dir,
                env=environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        logger.info(
            f"SubtitleForge 服务已启动，PID={self._managed_process.pid}, "
            f"目录={self._service_dir}"
        )
        try:
            (self._service_dir / "subtitleforge.pid").write_text(
                str(self._managed_process.pid), encoding="utf-8"
            )
        except OSError:
            logger.warning("SubtitleForge: 无法写入 PID 文件")

    def _sync_bundled_service(self) -> bool:
        """Copy the service bundled with the plugin without overwriting data."""

        payload_dir = Path(__file__).resolve().parent / "payload"
        if not payload_dir.is_dir():
            return False
        changed = False
        for relative in ("src",):
            source_root = payload_dir / relative
            target_root = self._service_dir / relative
            if not source_root.is_dir():
                continue
            for source in source_root.rglob("*"):
                if not source.is_file() or "__pycache__" in source.parts:
                    continue
                target = target_root / source.relative_to(source_root)
                try:
                    if target.is_file() and target.read_bytes() == source.read_bytes():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    changed = True
                except OSError as exc:
                    logger.error(f"SubtitleForge: 同步服务文件失败 {target}: {exc}")
        for name in ("pyproject.toml", ".env.example"):
            source = payload_dir / name
            target = self._service_dir / name
            if not source.is_file():
                continue
            try:
                content = source.read_bytes()
                if target.is_file() and target.read_bytes() == content:
                    continue
                self._service_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                changed = True
            except OSError as exc:
                logger.error(f"SubtitleForge: 同步服务文件失败 {target}: {exc}")
        if changed:
            logger.info("SubtitleForge: 已同步插件内置服务代码")
        return changed

    def _write_provider_env(self) -> bool:
        """Persist provider settings to the private service env file."""

        values = {
            "SF_MEDIA_ROOTS": self._media_roots,
            "SF_PROVIDER_ORDER": self._provider_order,
            "SF_SUBTITLE_BEST_API_KEY": self._subtitle_best_api_key,
            "SF_SUBTITLE_BEST_HEADER_TOKEN": self._subtitle_best_header_token,
            "SF_OPENSUBTITLES_API_KEY": self._opensubtitles_api_key,
            "SF_OPENSUBTITLES_USERNAME": self._opensubtitles_username,
            "SF_OPENSUBTITLES_PASSWORD": self._opensubtitles_password,
            "SF_ASSRT_TOKEN": self._assrt_token,
            "SF_XUNLEI_ENABLED": str(self._xunlei_enabled).lower(),
            "SF_SHOOTER_ENABLED": str(self._shooter_enabled).lower(),
            "SF_CUSTOM_PROVIDER_URL": self._custom_provider_url,
            "SF_CUSTOM_PROVIDER_TOKEN": self._custom_provider_token,
        }
        # Blank secret fields mean "keep the existing value" so a routine form
        # save cannot accidentally erase credentials.
        updates = {
            key: value.strip()
            for key, value in values.items()
            if value and "\n" not in value and "\r" not in value
        }
        if not updates:
            return False
        env_file = self._service_dir / ".env"
        try:
            self._service_dir.mkdir(parents=True, exist_ok=True)
            lines = (
                env_file.read_text(encoding="utf-8").splitlines()
                if env_file.is_file()
                else []
            )
            remaining = dict(updates)
            output: list[str] = []
            for line in lines:
                key = line.split("=", 1)[0].strip() if "=" in line else ""
                if key in remaining:
                    output.append(f"{key}={json.dumps(remaining.pop(key), ensure_ascii=False)}")
                else:
                    output.append(line)
            output.extend(
                f"{key}={json.dumps(value, ensure_ascii=False)}"
                for key, value in remaining.items()
            )
            env_file.write_text("\n".join(output) + "\n", encoding="utf-8")
            env_file.chmod(0o600)
            return True
        except OSError as exc:
            logger.error(f"SubtitleForge: 无法保存字幕源配置: {exc}")
            return False

    def _read_service_token(self) -> str:
        """Read the service token from its private env file when not overridden."""

        env_file = self._service_dir / ".env"
        try:
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line.startswith("SF_API_TOKEN="):
                    continue
                return line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            return ""
        return ""

    def _service_healthy(self) -> bool:
        """Return whether the configured service answers its health check."""

        try:
            self._request_json("GET", "/healthz", None, timeout=2)
            return True
        except Exception:  # noqa: BLE001 - a health probe must not escape
            return False

    def _stop_managed_service(self) -> None:
        """Stop only the child process started by this plugin instance."""

        process = self._managed_process
        self._managed_process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        pid_file = self._service_dir / "subtitleforge.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        if self._is_managed_pid(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _is_managed_pid(pid: int) -> bool:
        """Verify a PID belongs to the bundled service before signalling it."""

        if pid < 2:
            return False
        try:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return b"subtitleforge.api:app" in command_line and b"uvicorn" in command_line

    @eventmanager.register(EventType.TransferComplete)
    async def on_transfer_complete(self, event: Event) -> None:
        """Process each successfully transferred video."""

        if not self._enabled:
            return
        data = event.event_data if isinstance(event.event_data, dict) else {}
        transferinfo = data.get("transferinfo")
        mediainfo = data.get("mediainfo")
        meta = data.get("meta")
        paths = self._extract_video_paths(transferinfo)
        if not paths:
            logger.debug("SubtitleForge: TransferComplete has no video paths")
            return
        if mediainfo is None:
            logger.debug("SubtitleForge: TransferComplete has no mediainfo")
            return
        media_type = self._media_type(getattr(mediainfo, "type", None))
        if (
            self._exclude_domestic_anime
            and self._is_domestic_anime(mediainfo, paths)
        ):
            logger.info(
                "SubtitleForge: 已跳过国产动漫 "
                f"{getattr(mediainfo, 'title', '') or '未知标题'}"
            )
            return
        common_payload = {
            "title": str(getattr(mediainfo, "title", "") or ""),
            "original_title": getattr(mediainfo, "original_title", None),
            "english_title": getattr(mediainfo, "en_title", None),
            "year": self._optional_int(getattr(mediainfo, "year", None)),
            "imdb_id": getattr(mediainfo, "imdb_id", None),
            "tmdb_id": self._optional_int(getattr(mediainfo, "tmdb_id", None)),
            "media_type": media_type,
            "season": self._optional_int(getattr(meta, "begin_season", None)),
            "episode": self._optional_int(getattr(meta, "begin_episode", None)),
            "poster_url": self._media_poster_url(mediainfo),
            "poster_source": "moviepilot",
            "languages": self._languages,
            "overwrite": self._overwrite,
            "dry_run": self._dry_run,
        }
        if not common_payload["title"]:
            logger.warning("SubtitleForge: media title is empty; skip subtitle search")
            return
        aggregate: dict[str, Any] = {
            "status": "ok",
            "downloaded": [],
            "errors": [],
        }
        for path in paths:
            payload = {**common_payload, "video_path": str(path)}
            try:
                result = await asyncio.to_thread(
                    self._request_json, "POST", "/v1/process", payload
                )
            except Exception as exc:  # noqa: BLE001 - handlers must not crash the host
                logger.error(f"SubtitleForge 处理失败：{type(exc).__name__}: {exc}")
                result = {"status": "error", "errors": [str(exc)]}
            logger.info(
                f"SubtitleForge: {payload['title']} -> {result.get('status')}, "
                f"downloaded={len(result.get('downloaded') or [])}"
            )
            self._add_history(payload, result)
            aggregate["downloaded"].extend(result.get("downloaded") or [])
            aggregate["errors"].extend(result.get("errors") or [])
            if result.get("status") not in (None, "ok", "downloaded"):
                aggregate["status"] = result.get("status")
        if self._notify and aggregate["downloaded"]:
            names = "\n".join(
                f"· {Path(str(item.get('path') or '')).name} "
                f"[{item.get('language', '')}/{item.get('provider', '')}]"
                for item in aggregate["downloaded"]
                if isinstance(item, dict)
            )
            self.post_message(
                mtype=MessageType.Plugin,
                title=f"字幕已补全 - {common_payload['title']}",
                text=f"已下载字幕文件：\n{names}",
            )

    @staticmethod
    def _extract_video_paths(transferinfo: Any) -> list[str]:
        """Read file paths from both MoviePilot V2 and V3 transfer payloads."""

        if transferinfo is None:
            return []
        target_dir = getattr(transferinfo, "target_diritem", None)
        target_dir_path = getattr(target_dir, "path", None)
        candidates: list[str] = []
        for item in getattr(transferinfo, "file_list_new", None) or []:
            item_path = item if isinstance(item, str) else getattr(item, "path", None)
            if not item_path:
                continue
            if target_dir_path and not str(item_path).startswith("/"):
                item_path = str(Path(target_dir_path) / str(item_path))
            candidates.append(str(item_path))
        if not candidates:
            target_item = getattr(transferinfo, "target_item", None)
            target_path = getattr(target_item, "path", None)
            if target_path:
                candidates.append(str(target_path))
        return list(dict.fromkeys(path for path in candidates if path))

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        timeout: float | None = None,
    ) -> Any:
        """Perform a small JSON request without adding another dependency."""

        url = f"{self._service_url}{path}"
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout or self._timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"无法连接 SubtitleForge: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SubtitleForge 返回了非 JSON 内容") from exc

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """Return the Vuetify JSON configuration form."""

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用字幕补全",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "overwrite",
                                            "label": "覆盖已有字幕",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "下载成功通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "每次运行先全库检查外挂和内嵌中文字幕，再把缺失项写入持久队列；只有队列任务会访问字幕源，并受每日下载额度限制。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scan_enabled",
                                            "label": "启用定时媒体库扫描",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scan_dry_run",
                                            "label": "仅预览，不下载",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "scan_interval_hours",
                                            "label": "扫描间隔（小时）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "scan_paths",
                            "label": "扫描目录（每行或逗号分隔）",
                            "rows": 3,
                            "placeholder": "/plex/电影/外语电影\n/plex/电视剧/欧美剧",
                        },
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "scan_exclude_paths",
                            "label": "排除目录",
                            "rows": 2,
                            "placeholder": "/plex/电影/华语电影\n/plex/电视剧/国产剧",
                        },
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "exclude_domestic_anime",
                            "label": "排除国产动漫（动画 + 中国地区）",
                            "hint": "同时作用于定时扫描和整理完成后的实时补全",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "domestic_anime_regions",
                            "label": "国产动漫地区",
                            "placeholder": "CN,TW,HK",
                            "hint": "留空则不按地区排除；默认包含中国大陆、台湾和香港",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "domestic_anime_path_keywords",
                            "label": "国产动漫路径关键词（兜底）",
                            "placeholder": "国漫,国产,国产动漫,国产动画,国产番剧,国创",
                            "hint": "当 NFO 缺少国家信息时，可用目录名如 /动漫/国漫 排除",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "scan_max_files",
                                            "label": "每次最多从队列下载",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "scan_daily_download_limit",
                                            "label": "每日下载上限",
                                            "type": "number",
                                            "hint": "所有字幕源合计，按成功下载数量计数",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "scan_video_extensions",
                                            "label": "视频扩展名",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "scan_provider_order",
                            "label": "扫描专用字幕源顺序（可选）",
                            "placeholder": "留空则使用上方字幕源顺序，按顺序故障切换",
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "scan_languages",
                            "label": "扫描需要补齐的语言",
                            "multiple": True,
                            "chips": True,
                            "items": [
                                {"title": "简体中文", "value": "zh-Hans"},
                                {"title": "繁體中文", "value": "zh-Hant"},
                            ],
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "配额保护：全库扫描和排队不访问字幕源；下载数量同时受“本次批次”和“每日上限”限制，跨重启保留。命中限流会冷却字幕源，TG 推送只列出实际下载成功的字幕文件。",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "provider_order",
                            "label": "字幕源顺序",
                            "placeholder": (
                                "xunlei,assrt,subtitle_best,"
                                "opensubtitles,custom,shooter"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "xunlei_enabled",
                                            "label": "启用迅雷文件指纹字幕源",
                                            "hint": "免费、按视频文件指纹精确匹配；部分冷门内容可能无结果",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "shooter_enabled",
                                            "label": "启用 Shooter 指纹源",
                                            "hint": "上游项目仍保留，但当前网络环境经常超时，默认关闭",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "ASSRT 使用 MoviePilot 的标题、年份和 IMDb 信息直接搜索，不再要求视频目录存在 NFO。字幕源密码留空表示保留已有值。",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "subtitle_best_api_key",
                            "label": "SubtitleBest API Key",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "subtitle_best_header_token",
                            "label": "SubtitleBest 客户端 Token（通常无需修改）",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "assrt_token",
                            "label": "ASSRT Token",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "opensubtitles_api_key",
                            "label": "OpenSubtitles API Key",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "opensubtitles_username",
                            "label": "OpenSubtitles 用户名",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "opensubtitles_password",
                            "label": "OpenSubtitles 密码",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "custom_provider_url",
                            "label": "自定义字幕源 JSON 地址",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "custom_provider_token",
                            "label": "自定义字幕源 Token",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "service_url",
                            "label": "SubtitleForge 服务地址",
                            "placeholder": "http://127.0.0.1:8600",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "media_roots",
                            "label": "允许扫描的媒体根目录",
                            "hint": "多个目录用英文逗号分隔；必须与 MoviePilot 容器内路径一致",
                            "placeholder": "/plex,/media",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "api_token",
                            "label": "服务 API Token",
                            "type": "password",
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "languages",
                            "label": "需要补齐的语言",
                            "multiple": True,
                            "chips": True,
                            "items": [
                                {"title": "简体中文", "value": "zh-Hans"},
                                {"title": "繁體中文", "value": "zh-Hant"},
                            ],
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "timeout",
                            "label": "请求超时（秒）",
                            "type": "number",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "manage_service",
                                            "label": "随插件启动本机服务",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "service_dir",
                                            "label": "SubtitleForge 安装目录",
                                            "placeholder": "/config/subtitleforge",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "MoviePilot 与 SubtitleForge 必须能互相访问；两者对媒体目录的挂载路径也必须一致。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "service_url": "http://127.0.0.1:8600",
            "api_token": "",
            "media_roots": "/plex,/media",
            "languages": ["zh-Hans", "zh-Hant"],
            "overwrite": False,
            "notify": True,
            "timeout": 180,
            "dry_run": False,
            "manage_service": True,
            "service_dir": "/config/subtitleforge",
            "provider_order": (
                "xunlei,assrt,subtitle_best,opensubtitles,custom,shooter"
            ),
            "subtitle_best_api_key": "",
            "subtitle_best_header_token": "5akwmGAbuFWqgaZf9QwT",
            "opensubtitles_api_key": "",
            "opensubtitles_username": "",
            "opensubtitles_password": "",
            "assrt_token": "",
            "xunlei_enabled": True,
            "shooter_enabled": False,
            "custom_provider_url": "",
            "custom_provider_token": "",
            "scan_enabled": False,
            "scan_paths": (
                "/plex/电影/外语电影,/plex/电视剧/欧美剧,/plex/电视剧/日韩剧,"
                "/plex/电视剧/纪录片,/plex/动漫"
            ),
            "scan_exclude_paths": "/plex/电影/华语电影,/plex/电视剧/国产剧",
            "exclude_domestic_anime": True,
            "domestic_anime_regions": "CN,TW,HK",
            "domestic_anime_path_keywords": (
                "国漫,国产,国产动漫,国产动画,国产番剧,国创"
            ),
            "scan_video_extensions": (
                ".mkv,.mp4,.avi,.mov,.wmv,.m4v,.ts,.m2ts,.webm"
            ),
            "scan_interval_hours": 24,
            "scan_max_files": 10,
            "scan_daily_download_limit": 10,
            "scan_dry_run": True,
            "scan_provider_order": "",
            "scan_languages": ["zh-Hans"],
        }

    def get_page(self) -> list[dict]:
        """Render live quota, provider health, scan results, and history."""

        history = self._load_history()
        latest_scan = self._load_latest_scan()
        runtime, status_error = self._load_runtime_status()
        quota = runtime.get("quota") if isinstance(runtime.get("quota"), dict) else {}
        queue = runtime.get("queue") if isinstance(runtime.get("queue"), dict) else {}
        queue_groups = (
            runtime.get("queue_groups")
            if isinstance(runtime.get("queue_groups"), dict)
            else {}
        )
        provider_data = runtime.get("providers")
        provider_data = provider_data if isinstance(provider_data, dict) else {}
        temporary_cooldown = int(
            provider_data.get("temporary_cooldown_minutes", 15) or 0
        )
        used_today = int(
            quota.get(
                "used",
                latest_scan.get("downloads_today", 0),
            )
            or 0
        )
        daily_limit = int(
            quota.get("limit", self._scan_daily_download_limit)
            or self._scan_daily_download_limit
        )
        queue_total = int(
            queue.get("total", latest_scan.get("queue_remaining", 0)) or 0
        )
        queue_due = int(queue.get("due", 0) or 0)
        queue_deferred = int(queue.get("deferred", 0) or 0)
        service_online = bool(runtime)
        quota_percent = min(100.0, used_today / max(1, daily_limit) * 100)
        mode_text = "仅预览" if self._scan_dry_run else "自动补全"
        schedule_text = (
            f"每 {self._scan_interval_hours} 小时扫描"
            if self._scan_enabled
            else "定时扫描关闭"
        )

        page: list[dict] = [
            {
                "component": "VCard",
                "props": {
                    "class": "mb-3",
                    "variant": "tonal",
                    "color": "success" if self._enabled else "default",
                },
                "content": [
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "div",
                                "props": {
                                    "class": "d-flex flex-wrap align-center ga-2"
                                },
                                "content": [
                                    {
                                        "component": "VIcon",
                                        "props": {
                                            "icon": "mdi-subtitles",
                                            "size": 32,
                                        },
                                    },
                                    {
                                        "component": "div",
                                        "props": {"class": "flex-grow-1"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-h6 font-weight-bold"
                                                },
                                                "text": "SubtitleForge 字幕补全",
                                            },
                                            {
                                                "component": "div",
                                                "props": {
                                                    "class": "text-caption"
                                                },
                                                "text": (
                                                    f"扫描每日额度 {used_today}/"
                                                    f"{daily_limit} · "
                                                    f"队列 {queue_total} · "
                                                    f"{schedule_text}"
                                                ),
                                            },
                                        ],
                                    },
                                    self._chip(
                                        "运行中" if self._enabled else "未启用",
                                        "success" if self._enabled else "default",
                                    ),
                                    self._chip(mode_text, "info"),
                                    self._chip(
                                        "服务在线" if service_online else "服务离线",
                                        "success" if service_online else "error",
                                    ),
                                ],
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-caption mt-2"},
                                "text": (
                                    f"服务地址 {self._service_url} · "
                                    "MoviePilot 通知 "
                                    f"{'已开启' if self._notify else '已关闭'}"
                                ),
                            },
                        ],
                    }
                ],
            },
            {
                "component": "div",
                "props": {
                    "class": "d-flex flex-wrap align-center ga-2 my-3"
                },
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "flat",
                            "prepend-icon": "mdi-magnify-scan",
                            "disabled": not self._enabled
                            or self._scan_running,
                            "onclick": self._scan_onclick(),
                        },
                        "text": (
                            "扫描中..."
                            if self._scan_running
                            else (
                                "立即扫描（仅预览）"
                                if self._scan_dry_run
                                else "立即扫描并补全"
                            )
                        ),
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "default",
                            "variant": "tonal",
                            "prepend-icon": "mdi-refresh",
                            "onclick": "location.reload()",
                        },
                        "text": "刷新状态",
                    },
                    self._chip(
                        f"今日扫描下载 {used_today}/{daily_limit}",
                        (
                            "warning"
                            if used_today >= daily_limit
                            else "primary"
                        ),
                    ),
                    self._chip(f"待处理 {queue_total}", "default"),
                    self._chip(
                        f"到期 {queue_due} / 延后 {queue_deferred}",
                        "default",
                    ),
                    self._chip(
                        (
                            f"迅雷 {'开启' if self._xunlei_enabled else '关闭'}"
                        ),
                        "success" if self._xunlei_enabled else "default",
                    ),
                    self._chip(
                        f"Shooter {'开启' if self._shooter_enabled else '关闭'}",
                        "success" if self._shooter_enabled else "default",
                    ),
                    self._chip(
                        (
                            "国产动漫排除"
                            if self._exclude_domestic_anime
                            else "国产动漫保留"
                        ),
                        "success" if self._exclude_domestic_anime else "default",
                    ),
                ],
            },
        ]
        if status_error:
            page.append(
                self._alert(
                    "warning",
                    "实时状态服务暂不可用，以下额度与队列可能来自最近一次扫描。"
                    "请先在插件设置中启用插件，并确认内置服务已启动。",
                )
            )
        if not self._enabled:
            page.append(
                self._alert(
                    "info",
                    "插件当前未启用。启用后才能开始扫描，字幕源不会产生任何请求。",
                )
            )

        page.extend(
            [
                {
                    "component": "VRow",
                    "props": {"class": "mb-1"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6, "lg": 3},
                            "content": [
                                self._metric_card(
                                    "今日扫描下载",
                                    f"{used_today} / {daily_limit}",
                                    f"剩余 {max(0, daily_limit - used_today)} 条",
                                    "mdi-download-circle-outline",
                                    "primary",
                                    quota_percent,
                                )
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6, "lg": 3},
                            "content": [
                                self._metric_card(
                                    "待处理队列",
                                    str(queue_total),
                                    f"到期 {queue_due} · 延后 {queue_deferred}",
                                    "mdi-tray-full",
                                    "info",
                                )
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6, "lg": 3},
                            "content": [
                                self._metric_card(
                                    "最近扫描",
                                    str(latest_scan.get("checked", 0)),
                                    (
                                        f"发现 {latest_scan.get('discovered', 0)} · "
                                        f"已有 {latest_scan.get('existing', 0)} · "
                                        f"缺失 {latest_scan.get('queued', 0)} · "
                                        f"排除国产动漫 "
                                        f"{latest_scan.get('excluded_domestic_anime', 0)} · "
                                        f"配置目录清理 "
                                        f"{latest_scan.get('excluded_configured_paths', 0)}"
                                    ),
                                    "mdi-library-shelves",
                                    "secondary",
                                )
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "sm": 6, "lg": 3},
                            "content": [
                                self._metric_card(
                                    "最近扫描时间",
                                    str(latest_scan.get("time") or "尚未扫描"),
                                    (
                                        f"本轮下载 {latest_scan.get('downloaded', 0)} 条"
                                        if latest_scan
                                        else "等待首次媒体库扫描"
                                    ),
                                    "mdi-clock-outline",
                                    "default",
                                )
                            ],
                        },
                    ],
                },
                {
                    "component": "VCard",
                    "props": {"class": "mb-3", "variant": "outlined"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {"class": "text-subtitle-1"},
                            "text": "工作逻辑",
                        },
                        {
                            "component": "VCardText",
                            "content": [
                                self._logic_line(
                                    "mdi-file-search-outline",
                                    "1. 先全库检查",
                                    (
                                        "逐个检查外挂和内嵌中文字幕。只读本地文件，"
                                        "这一步不访问任何字幕源。开启国产动漫排除时，"
                                        "先按动画类型和国家/地区过滤，日漫不会仅因"
                                        "中文标题被排除；缺少元数据时再用路径关键词兜底。"
                                    ),
                                ),
                                self._logic_line(
                                    "mdi-tray-arrow-down",
                                    "2. 缺失项排队",
                                    (
                                        "缺字幕的视频写入持久队列，按入队时间处理；"
                                        "失败任务约 24 小时后再试。"
                                    ),
                                ),
                                self._logic_line(
                                    "mdi-source-branch",
                                    "3. 按顺序故障切换",
                                    (
                                        "按下方字幕源顺序搜索。前一个源返回可用候选后，"
                                        "后面的源不再搜索；候选还要通过标题、年份、"
                                        "季集、语言和格式匹配。"
                                    ),
                                ),
                                self._logic_line(
                                    "mdi-counter",
                                    "4. 成功下载才计额度",
                                    (
                                        f"媒体库扫描队列每天最多记录 {daily_limit} 个"
                                        "成功字幕文件，是所有字幕源合计，不是每家各"
                                        " 10 条。同一视频补齐简体和繁体各成功一次，"
                                        "会分别计 2 条。"
                                    ),
                                ),
                                self._logic_line(
                                    "mdi-shield-alert-outline",
                                    "5. 限流后自动冷却",
                                    (
                                        "某个源返回 429 或额度错误时，该源冷却 24 小时；"
                                        "HTTP 5xx、Cloudflare 522 或连接失败时短暂"
                                        f"冷却 {temporary_cooldown} 分钟；其他源继续。"
                                        "外部账号的每日额度仍由各站点控制，本插件"
                                        "无法提前知道其剩余数量。"
                                    ),
                                ),
                            ],
                        },
                    ],
                },
                self._alert(
                    "warning",
                    (
                        "额度说明：每日上限只约束媒体库扫描队列，按“成功写入的"
                        "字幕文件”计数，跨服务重启保留。MoviePilot 整理完成后调用"
                        "的实时补全接口不经过扫描队列，因此不计入、也不受该上限约束。"
                        "迅雷免账号；ASSRT、SubtitleBest、OpenSubtitles 等还会受"
                        "各自账号、积分或频率限制，具体额度以服务方为准。命中限流"
                        "会自动冷却，不会无限重试。"
                    ),
                ),
            ]
        )

        if queue_total:
            groups = queue_groups.get("groups")
            groups = groups if isinstance(groups, list) else []
            if groups:
                page.append(self._queue_card(queue_groups))
            else:
                page.append(
                    self._alert(
                        "info",
                        "队列中有待处理视频，但当前服务版本还没有返回季度分组数据。"
                        "请更新插件内置服务后再刷新页面。",
                    )
                )

        page.append(
            {
                "component": "VCard",
                "props": {"class": "my-3", "variant": "outlined"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "字幕源状态与额度边界",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "pt-0"},
                        "content": [
                            self._provider_table(
                                provider_data
                            )
                        ],
                    },
                ],
            }
        )

        if latest_scan:
            page.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-3", "variant": "outlined"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {
                                "class": (
                                    "text-subtitle-1 d-flex flex-wrap "
                                    "align-center ga-2"
                                )
                            },
                            "content": [
                                {
                                    "component": "span",
                                    "text": (
                                        f"最近扫描 · "
                                        f"{latest_scan.get('time') or '-'}"
                                    ),
                                },
                                self._chip(
                                    "仅预览"
                                    if latest_scan.get("dry_run")
                                    else "自动补全",
                                    "info",
                                ),
                                self._chip(
                                    self._scan_status_label(
                                        str(latest_scan.get("status") or "")
                                    ),
                                    "primary",
                                ),
                            ],
                        },
                        {
                            "component": "VCardText",
                            "props": {"class": "pt-0"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "text-caption mb-2"
                                    },
                                    "text": (
                                        f"发现 {latest_scan.get('discovered', 0)} · "
                                        f"检查 {latest_scan.get('checked', 0)} · "
                                        f"已有 {latest_scan.get('existing', 0)} · "
                                        f"配置目录清理 "
                                        f"{latest_scan.get('excluded_configured_paths', 0)} · "
                                        f"排除国产动漫 "
                                        f"{latest_scan.get('excluded_domestic_anime', 0)} · "
                                        f"缺失入队 {latest_scan.get('queued', 0)} · "
                                        f"本轮下载 {latest_scan.get('downloaded', 0)} · "
                                        f"队列剩余 "
                                        f"{latest_scan.get('queue_remaining', 0)}"
                                    ),
                                },
                                (
                                    self._scan_items_table(
                                        [
                                            item
                                            for item in (
                                                latest_scan.get("items") or []
                                            )
                                            if isinstance(item, dict)
                                        ][:20]
                                    )
                                    if latest_scan.get("items")
                                    else {
                                        "component": "div",
                                        "props": {"class": "text-caption"},
                                        "text": "本轮没有可展示的条目明细。",
                                    }
                                ),
                                *(
                                    [
                                        self._alert(
                                            "error",
                                            "扫描错误："
                                            + "；".join(
                                                str(error)
                                                for error in (
                                                    latest_scan.get("errors") or []
                                                )[:10]
                                            ),
                                        )
                                    ]
                                    if latest_scan.get("errors")
                                    else []
                                ),
                            ],
                        },
                    ],
                }
            )
        else:
            page.append(
                self._alert(
                    "info",
                    "尚未执行媒体库扫描。可先在设置中保持“仅预览”"
                    "完成一次全库检查，确认识别结果后再关闭预览。",
                )
            )

        if history:
            page.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-3", "variant": "outlined"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {"class": "text-subtitle-1"},
                            "text": "整理完成后的最近处理",
                        },
                        {
                            "component": "VCardText",
                            "props": {"class": "pt-0"},
                            "content": [self._history_table(history[:10])],
                        },
                    ],
                }
            )
        return page

    def _queue_card(self, queue_view: dict[str, Any]) -> dict[str, Any]:
        """Build the grouped queue with posters and manual subtitle actions."""

        raw_groups = queue_view.get("groups")
        groups = [
            group
            for group in (raw_groups if isinstance(raw_groups, list) else [])
            if isinstance(group, dict)
        ]
        content: list[dict[str, Any]] = [
            {
                "component": "div",
                "props": {"class": "text-caption mb-3"},
                "text": (
                    "电影单独显示；剧集和动漫按“名称 + 季度”合并，"
                    "默认收起分集。展开本季后可逐集手动搜索并指定字幕。"
                ),
            }
        ]
        content.extend(self._queue_group_card(group) for group in groups[:30])
        if queue_view.get("truncated"):
            content.append(
                self._alert(
                    "info",
                    "队列较大，页面只显示前 30 个分组；完整队列仍会继续自动处理。",
                )
            )
        return {
            "component": "VCard",
            "props": {"class": "mb-3", "variant": "outlined"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {
                        "class": (
                            "text-subtitle-1 d-flex flex-wrap align-center ga-2"
                        )
                    },
                    "content": [
                        {"component": "span", "text": "待处理字幕队列"},
                        self._chip(
                            f"{queue_view.get('total', 0)} 个视频",
                            "primary",
                        ),
                        self._chip(
                            f"{len(groups)} 个分组",
                            "info",
                        ),
                    ],
                },
                {
                    "component": "VCardText",
                    "props": {"class": "pt-0"},
                    "content": content,
                },
            ],
        }

    def _queue_group_card(self, group: dict[str, Any]) -> dict[str, Any]:
        """Build one movie or one TV/anime season card."""

        title = str(group.get("title") or "未知标题")
        year = group.get("year")
        season = group.get("season")
        category = str(group.get("category") or group.get("media_type") or "movie")
        category_label = {
            "movie": "电影",
            "tv": "剧集",
            "anime": "动漫",
        }.get(category, "媒体")
        season_label = (
            f"第 {int(season)} 季"
            if season is not None
            else "未识别季度"
        )
        meta_parts = [category_label]
        if year is not None:
            meta_parts.append(f"{year} 年")
        if category in {"tv", "anime"}:
            meta_parts.append(season_label)
        missing = "、".join(
            self._language_label(language)
            for language in (group.get("missing_languages") or [])
        ) or "未标注"
        poster = str(group.get("poster") or "").strip()
        poster_frame_props = {
            "class": (
                "me-3 flex-shrink-0 rounded overflow-hidden "
                "d-flex align-center justify-center"
            ),
            "style": (
                "width:92px;height:138px;min-width:92px;"
                "background:rgba(128,128,128,.12);"
            ),
        }
        poster_node: dict[str, Any] = {
            "component": "div",
            "props": poster_frame_props,
            "content": [
                (
                    {
                        "component": "VImg",
                        "props": {
                            "src": poster,
                            "alt": f"{title} 封面",
                            "width": 92,
                            "height": 138,
                            "cover": True,
                            "class": "w-100 h-100",
                            "style": (
                                "width:100%;height:100%;"
                                "object-fit:cover;"
                            ),
                        },
                    }
                    if poster
                    else {
                        "component": "VIcon",
                        "props": {
                            "icon": "mdi-image-off-outline",
                            "size": 42,
                        },
                    }
                )
            ],
        }
        items = [
            item
            for item in (group.get("items") or [])
            if isinstance(item, dict)
        ]
        is_series = category in {"tv", "anime"}
        episode_numbers: list[int] = []
        for item in items:
            try:
                episode_number = int(item.get("episode"))
            except (TypeError, ValueError):
                continue
            if episode_number not in episode_numbers:
                episode_numbers.append(episode_number)
        episode_numbers.sort()
        episode_preview = "、".join(
            f"E{episode_number:02d}"
            for episode_number in episode_numbers[:12]
        )
        if group.get("items_truncated") or len(episode_numbers) > 12:
            episode_preview = (
                f"{episode_preview} 等 {group.get('item_count', 0)} 集"
                if episode_preview
                else f"共 {group.get('item_count', 0)} 集"
            )
        item_nodes = [self._queue_item_row(item) for item in items]
        if group.get("items_truncated"):
            item_nodes.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis py-2"},
                    "text": (
                        f"该分组共 {group.get('item_count', 0)} 个视频，"
                        "页面只展开前 100 个。"
                    ),
                }
            )
        if not item_nodes:
            item_nodes.append(
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis py-2"},
                    "text": "当前没有可展开的视频条目。",
                }
            )
        if is_series:
            detail_nodes: list[dict[str, Any]] = [
                {
                    "component": "VExpansionPanels",
                    "props": {"variant": "accordion", "flat": True},
                    "content": [
                        {
                            "component": "VExpansionPanel",
                            "props": {
                                "title": (
                                    "查看分集与手动选字幕"
                                    f"（{group.get('item_count', len(items))} 集）"
                                )
                            },
                            "content": [
                                {
                                    "component": "VExpansionPanelText",
                                    "content": item_nodes,
                                }
                            ],
                        }
                    ],
                }
            ]
        else:
            detail_nodes = item_nodes
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "mb-3"},
            "content": [
                {
                    "component": "VCardText",
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex align-start flex-wrap"
                            },
                            "content": [
                                poster_node,
                                {
                                    "component": "div",
                                    "props": {"class": "flex-grow-1"},
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "text-h6 font-weight-bold"
                                            },
                                            "text": title,
                                        },
                                        {
                                            "component": "div",
                                            "props": {"class": "text-caption mt-1"},
                                            "text": " · ".join(meta_parts),
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": (
                                                    "d-flex flex-wrap ga-2 mt-2"
                                                )
                                            },
                                            "content": [
                                                self._chip(
                                                    (
                                                        "待处理 "
                                                        f"{group.get('item_count', len(items))} 集"
                                                        if is_series
                                                        else "共 "
                                                        f"{group.get('item_count', len(items))} 个视频"
                                                    ),
                                                    "info",
                                                ),
                                                self._chip(
                                                    f"缺失 {missing}",
                                                    "warning",
                                                ),
                                            ],
                                        },
                                        *(
                                            [
                                                {
                                                    "component": "div",
                                                    "props": {
                                                        "class": (
                                                            "text-caption "
                                                            "mt-2"
                                                        )
                                                    },
                                                    "text": (
                                                        "待处理分集："
                                                        f"{episode_preview}"
                                                        if episode_preview
                                                        else "待处理分集：未识别"
                                                    ),
                                                }
                                            ]
                                            if is_series
                                            else []
                                        ),
                                    ],
                                },
                            ],
                        },
                        {
                            "component": "div",
                            "props": {"class": "mt-2"},
                            "content": detail_nodes,
                        },
                    ],
                }
            ],
        }

    def _queue_item_row(self, item: dict[str, Any]) -> dict[str, Any]:
        """Build one queue item with a manual subtitle button."""

        season = item.get("season")
        episode = item.get("episode")
        episode_label = (
            f"S{int(season):02d}E{int(episode):02d}"
            if season is not None and episode is not None
            else "单文件"
        )
        filename = str(item.get("filename") or item.get("title") or "未知视频")
        languages = "、".join(
            self._language_label(language)
            for language in (item.get("languages") or [])
        ) or "未标注"
        state = (
            "等待重试"
            if item.get("deferred")
            else f"尝试 {item.get('attempts', 0)} 次"
        )
        video_path = str(item.get("video_path") or "")
        return {
            "component": "div",
            "props": {
                "class": (
                    "d-flex flex-wrap align-center ga-2 "
                    "border-b py-2"
                )
            },
            "content": [
                {
                    "component": "div",
                    "props": {"class": "flex-grow-1"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "font-weight-medium"},
                            "text": f"{episode_label} · {filename}",
                        },
                        {
                            "component": "div",
                            "props": {"class": "text-caption"},
                            "text": f"缺失 {languages} · {state}",
                        },
                    ],
                },
                {
                    "component": "VBtn",
                    "props": {
                        "size": "small",
                        "color": "primary",
                        "variant": "tonal",
                        "prepend-icon": "mdi-magnify",
                        "disabled": not self._enabled or not video_path,
                        "onclick": self._manual_onclick(video_path),
                    },
                    "text": "手动选字幕",
                },
            ],
        }

    def _load_runtime_status(self) -> tuple[dict[str, Any], str]:
        """Read the live service status and degrade gracefully when offline."""

        path = (
            "/v1/status?daily_download_limit="
            f"{max(1, int(self._scan_daily_download_limit))}"
            "&queue_limit=30"
        )
        try:
            value = self._request_json("GET", path, None, timeout=2)
        except Exception as exc:  # noqa: BLE001 - page rendering must survive
            logger.debug(f"SubtitleForge 状态读取失败：{type(exc).__name__}: {exc}")
            return {}, str(exc)
        if not isinstance(value, dict):
            return {}, "status response is not an object"
        return value, ""

    @staticmethod
    def _chip(text: str, color: str = "default") -> dict[str, Any]:
        """Build a compact status chip."""

        return {
            "component": "VChip",
            "props": {
                "variant": "tonal",
                "color": color,
                "size": "small",
            },
            "text": text,
        }

    @staticmethod
    def _alert(alert_type: str, text: str) -> dict[str, Any]:
        """Build a tonal alert used by the plugin status page."""

        return {
            "component": "VAlert",
            "props": {
                "type": alert_type,
                "variant": "tonal",
                "class": "mb-3",
                "text": text,
            },
        }

    @staticmethod
    def _logic_line(icon: str, title: str, text: str) -> dict[str, Any]:
        """Build one numbered workflow line."""

        return {
            "component": "div",
            "props": {
                "class": "d-flex align-start ga-3 py-2 border-b"
            },
            "content": [
                {
                    "component": "VIcon",
                    "props": {"icon": icon, "color": "primary", "size": 22},
                },
                {
                    "component": "div",
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "font-weight-medium"},
                            "text": title,
                        },
                        {
                            "component": "div",
                            "props": {"class": "text-caption"},
                            "text": text,
                        },
                    ],
                },
            ],
        }

    @staticmethod
    def _metric_card(
        title: str,
        value: str,
        subtitle: str,
        icon: str,
        color: str,
        progress: float | None = None,
    ) -> dict[str, Any]:
        """Build a compact metric card."""

        content: list[dict[str, Any]] = [
            {
                "component": "div",
                "props": {
                    "class": (
                        "d-flex align-center justify-space-between "
                        "text-caption text-medium-emphasis"
                    )
                },
                "content": [
                    {"component": "span", "text": title},
                    {
                        "component": "VIcon",
                        "props": {"icon": icon, "color": color, "size": 20},
                    },
                ],
            },
            {
                "component": "div",
                "props": {"class": "text-h6 font-weight-bold mt-2"},
                "text": value,
            },
            {
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis"},
                "text": subtitle,
            },
        ]
        if progress is not None:
            content.append(
                {
                    "component": "VProgressLinear",
                    "props": {
                        "model-value": round(progress, 1),
                        "color": color,
                        "height": 5,
                        "rounded": True,
                        "class": "mt-3",
                    },
                }
            )
        return {
            "component": "VCard",
            "props": {"variant": "tonal", "class": "h-100"},
            "content": [
                {
                    "component": "VCardText",
                    "props": {"class": "py-3"},
                    "content": content,
                }
            ],
        }

    def _provider_table(self, provider_data: dict[str, Any]) -> dict[str, Any]:
        """Build the provider order, state, and quota-boundary table."""

        runtime_items = provider_data.get("items")
        runtime_by_name = {
            str(item.get("name")): item
            for item in (runtime_items if isinstance(runtime_items, list) else [])
            if isinstance(item, dict) and item.get("name")
        }
        order = provider_data.get("order")
        names = (
            [str(name) for name in order]
            if isinstance(order, list) and order
            else self._split_list(self._provider_order)
        )
        if not names:
            names = self._split_list(
                "xunlei,assrt,subtitle_best,opensubtitles,custom,shooter"
            )
        meta = {
            "xunlei": (
                "迅雷",
                "免账号，按视频文件指纹精确匹配",
                "插件不设单源额度；服务方策略可能变化",
            ),
            "assrt": (
                "ASSRT",
                "中文电影/剧集字幕库",
                "账号积分与频率由 ASSRT 控制",
            ),
            "subtitle_best": (
                "SubtitleBest",
                "share-sub 中文字幕索引",
                "账号每日配额由 SubtitleBest 控制",
            ),
            "opensubtitles": (
                "OpenSubtitles",
                "官方 API，需完整账号才能下载",
                "会员下载额度由 OpenSubtitles 控制",
            ),
            "custom": (
                "自定义源",
                "用户自建 JSON 字幕接口",
                "额度取决于你自己的接口",
            ),
            "shooter": (
                "Shooter",
                "旧版文件指纹源，经常超时",
                "默认关闭；公共接口可能限流",
            ),
        }
        rows: list[dict[str, Any]] = []
        for index, name in enumerate(names, start=1):
            label, purpose, quota_note = meta.get(
                name,
                (name, "自定义字幕源", "额度取决于服务方"),
            )
            runtime_item = runtime_by_name.get(name, {})
            configured = bool(
                runtime_item.get(
                    "configured",
                    self._configured_provider_fallback(name),
                )
            )
            cooling_down = bool(runtime_item.get("cooling_down"))
            if cooling_down:
                status_chip = self._chip("冷却中", "warning")
            elif configured:
                status_chip = self._chip("可用", "success")
            else:
                status_chip = self._chip("未启用/未配置", "default")
            cooldown = (
                self._cooldown_label(runtime_item.get("cooldown_until"))
                if cooling_down
                else "-"
            )
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": str(index)},
                        {
                            "component": "td",
                            "text": label,
                            "props": {"class": "font-weight-medium"},
                        },
                        {"component": "td", "content": [status_chip]},
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "div",
                                    "text": purpose,
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption"},
                                    "text": quota_note,
                                },
                            ],
                        },
                        {
                            "component": "td",
                            "text": cooldown,
                            "props": {"class": "text-caption"},
                        },
                    ],
                }
            )
        return self._table(
            ["顺序", "字幕源", "状态", "用途 / 额度边界", "当前冷却"],
            rows,
        )

    def _configured_provider_fallback(self, name: str) -> bool:
        """Approximate provider readiness when the status API is unavailable."""

        return {
            "xunlei": self._xunlei_enabled,
            "assrt": bool(self._assrt_token),
            "subtitle_best": bool(
                self._subtitle_best_api_key
                and self._subtitle_best_header_token
            ),
            "opensubtitles": bool(
                self._opensubtitles_api_key
                and self._opensubtitles_username
                and self._opensubtitles_password
            ),
            "custom": bool(self._custom_provider_url),
            "shooter": self._shooter_enabled,
        }.get(name, False)

    @staticmethod
    def _cooldown_label(value: Any) -> str:
        """Format a provider cooldown deadline in the NAS local timezone."""

        if not value:
            return "-"
        try:
            deadline = datetime.fromisoformat(str(value))
            return deadline.astimezone().strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            return str(value)

    def _scan_items_table(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the latest scan item table."""

        rows: list[dict[str, Any]] = []
        for item in items:
            media_type = (
                "剧集"
                if str(item.get("media_type") or "").lower() == "tv"
                else "电影"
            )
            season = item.get("season")
            episode = item.get("episode")
            episode_label = (
                f"S{int(season):02d}E{int(episode):02d}"
                if season is not None and episode is not None
                else "-"
            )
            missing = "、".join(
                self._language_label(language)
                for language in (item.get("missing_languages") or [])
            ) or "-"
            downloaded = "；".join(
                (
                    f"{Path(str(file.get('path') or '')).name} "
                    f"[{self._language_label(file.get('language'))}/"
                    f"{file.get('provider') or '-'}]"
                )
                for file in (item.get("downloaded") or [])
                if isinstance(file, dict)
            ) or "-"
            errors = "；".join(
                str(error) for error in (item.get("errors") or [])
            )
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": media_type},
                        {
                            "component": "td",
                            "text": str(item.get("title") or "-"),
                            "props": {"class": "font-weight-medium"},
                        },
                        {"component": "td", "text": episode_label},
                        {
                            "component": "td",
                            "text": self._scan_status_label(
                                str(item.get("status") or "")
                            ),
                        },
                        {"component": "td", "text": missing},
                        {
                            "component": "td",
                            "text": downloaded,
                            "props": {"class": "text-caption"},
                        },
                        {
                            "component": "td",
                            "text": (errors[:240] + "…")
                            if len(errors) > 240
                            else (errors or "-"),
                            "props": {"class": "text-caption text-error"},
                        },
                    ],
                }
            )
        return self._table(
            ["类型", "标题", "季集", "状态", "缺失", "已下载", "错误"],
            rows,
        )

    def _history_table(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a compact transfer-complete processing history table."""

        rows: list[dict[str, Any]] = []
        for item in history:
            result = item.get("result")
            result = result if isinstance(result, dict) else {}
            downloaded = "；".join(
                (
                    f"{Path(str(file.get('path') or '')).name} "
                    f"[{self._language_label(file.get('language'))}/"
                    f"{file.get('provider') or '-'}]"
                )
                for file in (result.get("downloaded") or [])
                if isinstance(file, dict)
            ) or "-"
            errors = result.get("errors") or []
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {
                            "component": "td",
                            "text": str(item.get("time") or "-"),
                            "props": {"class": "text-caption"},
                        },
                        {
                            "component": "td",
                            "text": str(item.get("title") or "-"),
                        },
                        {
                            "component": "td",
                            "text": self._scan_status_label(
                                str(result.get("status") or "")
                            ),
                        },
                        {
                            "component": "td",
                            "text": downloaded,
                            "props": {"class": "text-caption"},
                        },
                        {
                            "component": "td",
                            "text": str(len(errors)),
                        },
                    ],
                }
            )
        return self._table(
            ["时间", "标题", "状态", "已下载", "错误"],
            rows,
        )

    @staticmethod
    def _table(headers: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a compact table from header labels and row definitions."""

        return {
            "component": "VTable",
            "props": {
                "density": "compact",
                "hover": True,
                "class": "text-caption",
            },
            "content": [
                {
                    "component": "thead",
                    "content": [
                        {
                            "component": "tr",
                            "content": [
                                {"component": "th", "text": header}
                                for header in headers
                            ],
                        }
                    ],
                },
                {"component": "tbody", "content": rows},
            ],
        }

    @staticmethod
    def _scan_status_label(status: str) -> str:
        """Translate service status values for the page."""

        return {
            "completed": "已完成",
            "skipped": "已有字幕",
            "dry_run": "待下载（预览）",
            "queued": "已入队",
            "not_found": "未找到",
            "partial": "部分完成",
            "error": "错误",
            "processing": "处理中",
        }.get(status, status or "-")

    @staticmethod
    def _language_label(language: Any) -> str:
        """Translate subtitle language codes for compact table cells."""

        return {
            "zh-Hans": "简中",
            "zh-Hant": "繁中",
            None: "-",
        }.get(str(language) if language is not None else None, str(language))

    def _scan_onclick(self) -> str:
        """Build the manual scan button handler."""

        api = json.dumps(f"plugin/{self.__class__.__name__}/scan")
        return (
            "(function(){"
            "if(!confirm('立即扫描媒体库并按配置补全缺失字幕？')){return;}"
            f"window.MoviePilotAPI.post({api},{{}})"
            ".then(function(r){"
            "if(!r||r.status==='error'||r.status==='disabled'||r.status==='busy'){"
            "alert((r&&(r.message||r.errors&&r.errors[0]))||'扫描失败');return;}"
            "alert('扫描完成：检查 '+(r.checked||0)+'，下载 '+(r.downloaded||0)+"
            "'，今日 '+(r.downloads_today||0)+'/'+(r.daily_download_limit||0));"
            "location.reload();"
            "}).catch(function(err){console.error(err);alert('扫描失败');});"
            "})()"
        )

    def _manual_onclick(self, video_path: str) -> str:
        """Build the interactive search-and-download handler for one video."""

        search_api = json.dumps(
            f"plugin/{self.__class__.__name__}/manual/search"
        )
        download_api = json.dumps(
            f"plugin/{self.__class__.__name__}/manual/download"
        )
        video_literal = json.dumps(video_path, ensure_ascii=True)
        return (
            "(function(){"
            f"var videoPath={video_literal};"
            "if(!confirm('搜索这个视频的全部字幕候选？')){return;}"
            f"window.MoviePilotAPI.post({search_api},{{video_path:videoPath}})"
            ".then(function(r){"
            "if(!r||r.status==='error'||r.status==='disabled'){"
            "alert((r&&(r.message||r.errors&&r.errors[0]))||'搜索失败');return null;}"
            "var list=r.candidates||[];"
            "if(!list.length){"
            "var extra=(r.errors&&r.errors.length)?('\\n'+r.errors.join('\\n')):'';"
            "alert('没有找到可用字幕候选。'+extra);return null;}"
            "var lines=list.map(function(c,i){"
            "return (i+1)+'. '+(c.language||'未知语言')+' / '"
            "+(c.provider||'-')+' / '+(c.format||'-')+'\\n   '"
            "+(c.release_name||c.title||'');"
            "});"
            "var answer=prompt('选择要下载的字幕编号：\\n\\n'+"
            "lines.join('\\n\\n'),'1');"
            "if(answer===null){return null;}"
            "var index=parseInt(answer,10)-1;"
            "if(isNaN(index)||index<0||index>=list.length){"
            "alert('字幕编号无效');return null;}"
            "var candidate=list[index];"
            "var language=candidate.language||null;"
            "if(!language){"
            "var lang=prompt('该字幕未标注语言，请输入 1=简体 2=繁体','1');"
            "if(lang==='1'){language='zh-Hans';}"
            "else if(lang==='2'){language='zh-Hant';}"
            "else if(lang!==null){alert('语言无效');return null;}"
            "}"
            "if(language===null){return null;}"
            f"return window.MoviePilotAPI.post({download_api},{{"
            "search_id:r.search_id,selection_id:candidate.selection_id,"
            "language:language,overwrite:"
            + json.dumps(bool(self._overwrite))
            + "});"
            "}).then(function(r){"
            "if(!r){return;}"
            "if(r.status==='error'||r.status==='disabled'){"
            "alert((r.message||(r.errors&&r.errors[0]))||'字幕下载失败');return;}"
            "var count=(r.downloaded||[]).length;"
            "if(count){alert('字幕已下载并写入视频目录。');location.reload();}"
            "else {alert('字幕未下载：'+((r.errors||[]).join('\\n')||"
            "r.status||'未知错误'));}"
            "}).catch(function(err){console.error(err);alert('手动选字幕失败');});"
            "})()"
        )

    def stop_service(self) -> None:
        """Stop the optional local service process owned by this plugin."""

        self._stop_managed_service()

    def _load_history(self) -> list[dict]:
        """Read recent history from plugin data."""

        value = self.get_data(self._history_key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        return value if isinstance(value, list) else []

    def _load_latest_scan(self) -> dict[str, Any]:
        """Read the most recent library scan summary."""

        value = self.get_data(self._latest_scan_key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}

    def _add_history(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        """Append a bounded history entry."""

        history = self._load_history()
        history.insert(
            0,
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": request.get("title", ""),
                "result": result,
            },
        )
        self.save_data(
            self._history_key,
            json.dumps(history[: self._history_max], ensure_ascii=False),
        )

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        """Read a field from either a MoviePilot model or a mapping."""

        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    @classmethod
    def _as_values(cls, value: Any) -> list[str]:
        """Flatten scalar, list, or metadata-dict values into text."""

        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, Mapping):
            values: list[str] = []
            for key in ("value", "iso_3166_1", "code", "name", "title", "id"):
                item = value.get(key)
                if item not in (None, ""):
                    values.extend(cls._as_values(item))
            return list(dict.fromkeys(values))
        if isinstance(value, (list, tuple, set)):
            values = []
            for item in value:
                values.extend(cls._as_values(item))
            return list(dict.fromkeys(values))
        values = []
        for key in ("value", "iso_3166_1", "code", "name", "title", "id"):
            item = getattr(value, key, None)
            if item not in (None, ""):
                values.extend(cls._as_values(item))
        if values:
            return list(dict.fromkeys(values))
        return [str(value).strip()] if str(value).strip() else []

    @classmethod
    def _metadata_values(
        cls,
        mediainfo: Any,
        names: tuple[str, ...],
    ) -> list[str]:
        """Read top-level and TMDB metadata fields without assuming a model type."""

        values: list[str] = []
        for name in names:
            values.extend(cls._as_values(cls._field(mediainfo, name)))
        tmdb_info = cls._field(mediainfo, "tmdb_info")
        if tmdb_info is not None:
            for name in names:
                values.extend(cls._as_values(cls._field(tmdb_info, name)))
        return list(dict.fromkeys(values))

    @staticmethod
    def _region_key(value: Any) -> str:
        """Normalize common country codes and localized names."""

        aliases = {
            "cn": {
                "cn",
                "chn",
                "china",
                "中国",
                "中国大陆",
                "中国内地",
                "中华人民共和国",
            },
            "tw": {"tw", "twn", "taiwan", "台湾", "中国台湾", "台灣"},
            "hk": {
                "hk",
                "hkg",
                "hongkong",
                "hong kong",
                "香港",
                "中国香港",
            },
            "mo": {"mo", "macau", "macao", "澳门", "澳門", "中国澳门"},
        }
        token = str(value or "").strip().casefold()
        for key, names in aliases.items():
            if token == key or token in names:
                return key
        return token

    def _is_domestic_anime(self, mediainfo: Any, paths: list[str]) -> bool:
        """Return whether MoviePilot metadata identifies domestic animation."""

        path_text = " ".join(str(path) for path in paths).casefold()
        keywords = self._split_list(self._domestic_anime_path_keywords)
        if any(keyword.casefold() in path_text for keyword in keywords):
            return True

        genres = self._metadata_values(mediainfo, ("genre_ids", "genres"))
        genre_tokens = {
            token.strip().casefold()
            for value in genres
            for token in value.split(",")
            if token.strip()
        }
        anime_genre_names = {"animation", "anime", "动画", "动漫", "番剧"}
        is_animation = "16" in genre_tokens or any(
            marker in token
            for token in genre_tokens
            for marker in anime_genre_names
        )
        if not is_animation:
            is_animation = any(
                marker in path_text
                for marker in ("动漫", "动画", "番剧", "anime", "animation")
            )
        if not is_animation:
            return False

        regions = {
            self._region_key(token)
            for value in self._split_list(self._domestic_anime_regions)
            for token in value.split(",")
        }
        countries = {
            self._region_key(token)
            for value in self._metadata_values(
                mediainfo,
                ("origin_country", "production_countries", "country"),
            )
            for token in value.split(",")
        }
        if countries:
            return bool(regions.intersection(countries))

        languages = self._metadata_values(
            mediainfo,
            ("original_language", "original_languages"),
        )
        return (
            "cn" in regions
            and any(
                token.strip().casefold() in {"zh", "cn", "bo", "za"}
                for value in languages
                for token in value.split(",")
            )
        )

    @classmethod
    def _media_poster_url(cls, mediainfo: Any) -> str | None:
        """Return a browser-safe MoviePilot/TMDB poster URL when available."""

        candidates: list[Any] = []
        for name in ("poster_url", "poster_path", "poster", "cover"):
            candidates.append(cls._field(mediainfo, name))
        tmdb_info = cls._field(mediainfo, "tmdb_info")
        if isinstance(tmdb_info, Mapping):
            for name in ("poster_path", "poster", "poster_url"):
                candidates.append(tmdb_info.get(name))
        for value in candidates:
            text = str(value or "").strip()
            if not text:
                continue
            if text.startswith("//"):
                text = f"https:{text}"
            if text.startswith(("http://", "https://")):
                host = (urlsplit(text).hostname or "").casefold()
                if host in TMDB_IMAGE_HOSTS:
                    return cls._tmdb_image_url(text)
                return text
            if text.startswith("data:image/"):
                return text
            api_prefix = str(
                getattr(cls._moviepilot_settings(), "API_V1_STR", "/api/v1")
                or "/api/v1"
            ).rstrip("/")
            if text.startswith(("/api/", "api/")):
                return f"/{text.lstrip('/')}"
            if text.startswith(("/system/", "system/")):
                return f"{api_prefix}/{text.lstrip('/')}"
            if text.startswith("/t/p/"):
                return cls._tmdb_image_url(text)
            if "imgurl=" in text and not Path(text).is_absolute():
                query = text.split("imgurl=", 1)[1]
                return f"{api_prefix}/system/img/1?imgurl={query}"
            path_value = urlsplit(text).path
            local_path = Path(path_value)
            if (
                local_path.is_absolute()
                and len(local_path.parts) > 2
                and local_path.suffix.casefold()
                in {".avif", ".jpeg", ".jpg", ".png", ".webp"}
            ):
                return text
            if (
                "/" not in path_value.lstrip("/")
                and local_path.suffix.casefold()
                in {".avif", ".jpeg", ".jpg", ".png", ".webp"}
            ):
                return cls._tmdb_image_url(f"/{text.lstrip('/')}")
        return None

    @staticmethod
    def _media_type(value: Any) -> str:
        """Map MoviePilot's localized media type to SubtitleForge's enum."""

        text = str(getattr(value, "value", value) or "").lower()
        return "tv" if text in {"tv", "电视剧", "剧集"} else "movie"

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        """Convert a MoviePilot field to an optional integer."""

        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _split_list(value: str) -> list[str]:
        """Split comma- or newline-separated configuration values."""

        return [
            item.strip()
            for item in str(value or "").replace("\n", ",").split(",")
            if item.strip()
        ]


__all__ = ["CSFReborn"]
