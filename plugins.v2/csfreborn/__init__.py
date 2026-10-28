"""MoviePilot V2/V3 adapter for SubtitleForge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import Body

from app import schemas
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


def _load_native_engine() -> Any:
    """Load the in-process subtitle engine in package, module or file layout."""

    try:
        from .native import NativeSubtitleEngine

        return NativeSubtitleEngine
    except ImportError:
        pass
    try:
        from native import NativeSubtitleEngine  # type: ignore[no-redef]

        return NativeSubtitleEngine
    except ImportError:
        pass
    import importlib.util

    path = Path(__file__).resolve().parent / "native.py"
    spec = importlib.util.spec_from_file_location("csfreborn_native", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载内置字幕引擎：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NativeSubtitleEngine


try:
    NativeSubtitleEngine: Any = _load_native_engine()
except Exception as _native_import_error:  # noqa: BLE001 - plugin must still load
    NativeSubtitleEngine = None
    logger.warning(
        "SubtitleForge 内置字幕引擎加载失败："
        f"{type(_native_import_error).__name__}: {_native_import_error}"
    )


class CSFReborn(_PluginBase):
    """Complete subtitles after transfers and during scheduled library scans."""

    plugin_name = "SubtitleForge 字幕补全"
    plugin_desc = (
        "监听整理完成事件并定时扫描媒体库，为缺少中文字幕的视频自动补全；"
        "内置 SubtitleBest / ASSRT / 迅雷 / Shooter 字幕源，无需额外服务也能工作。"
    )
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.6.5"
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
        self._service_url = "http://subtitleforge:8600"
        self._api_token = ""
        self._languages = ["zh-Hans", "zh-Hant"]
        self._overwrite = False
        self._dry_run = False
        self._notify = True
        self._timeout = 180
        self._manage_service = False
        self._service_dir = Path("/config/subtitleforge")
        self._provider_order = "subtitle_best,opensubtitles,assrt,custom"
        self._subtitle_best_api_key = ""
        self._subtitle_best_header_token = ""
        self._opensubtitles_api_key = ""
        self._opensubtitles_username = ""
        self._opensubtitles_password = ""
        self._assrt_token = ""
        self._custom_provider_url = ""
        self._custom_provider_token = ""
        self._scan_enabled = False
        self._scan_paths = (
            "/plex/电影/外语电影,/plex/电视剧/欧美剧,/plex/电视剧/日韩剧,"
            "/plex/电视剧/纪录片,/plex/动漫"
        )
        self._scan_exclude_paths = "/plex/电影/华语电影,/plex/电视剧/国产剧"
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
        # 待补全字幕队列（页面可勾选手动下载）
        self._queue_key = "subtitle_queue"
        self._queue_max = 200
        self._queue_running = False
        self._queue_task: Any = None
        # 内置字幕源（不依赖 SubtitleForge 服务）
        self._native_mode = "auto"
        self._native_xunlei = True
        self._native_shooter = False
        self._native_tag_style = "iso"
        self._native_auto_sync = False
        self._native_encoding = True
        self._native_max_subs = 2
        self._native_request_delay = 1.0
        self._native_engine: Any = None

    def init_plugin(self, config: dict | None = None) -> None:
        """Load configuration; safe to call repeatedly."""

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._service_url = str(
            config.get("service_url") or "http://subtitleforge:8600"
        ).rstrip("/")
        self._api_token = str(config.get("api_token") or "")
        self._languages = list(config.get("languages") or ["zh-Hans", "zh-Hant"])
        self._overwrite = bool(config.get("overwrite", False))
        self._dry_run = bool(config.get("dry_run", False))
        self._notify = bool(config.get("notify", True))
        self._manage_service = bool(config.get("manage_service", False))
        self._service_dir = Path(
            str(config.get("service_dir") or "/config/subtitleforge")
        ).expanduser()
        self._provider_order = str(
            config.get("provider_order")
            or "subtitle_best,opensubtitles,assrt,custom"
        )
        self._subtitle_best_api_key = str(config.get("subtitle_best_api_key") or "")
        self._subtitle_best_header_token = str(
            config.get("subtitle_best_header_token") or ""
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
        self._custom_provider_url = str(config.get("custom_provider_url") or "")
        self._custom_provider_token = str(
            config.get("custom_provider_token") or ""
        )
        self._scan_enabled = bool(config.get("scan_enabled", False))
        self._scan_paths = str(config.get("scan_paths") or self._scan_paths)
        self._scan_exclude_paths = str(
            config.get("scan_exclude_paths") or self._scan_exclude_paths
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
        # 服务 .env 里已配置的字幕源凭据直接复用，避免重复填写
        service_env = self._read_service_env()
        if not self._subtitle_best_api_key:
            self._subtitle_best_api_key = service_env.get(
                "SF_SUBTITLE_BEST_API_KEY", ""
            )
        if not self._subtitle_best_header_token:
            self._subtitle_best_header_token = service_env.get(
                "SF_SUBTITLE_BEST_HEADER_TOKEN", ""
            )
        if not self._assrt_token:
            self._assrt_token = service_env.get("SF_ASSRT_TOKEN", "")
        # 内置字幕源：默认读取插件已有的 SubtitleBest / ASSRT 凭据，无需重复配置
        self._native_mode = str(config.get("native_mode") or "auto")
        if self._native_mode not in {"auto", "native_only", "service_first", "off"}:
            self._native_mode = "auto"
        self._native_xunlei = bool(config.get("native_xunlei", True))
        self._native_shooter = bool(config.get("native_shooter", False))
        self._native_tag_style = str(config.get("native_tag_style") or "iso")
        self._native_auto_sync = bool(config.get("native_auto_sync", False))
        self._native_encoding = bool(config.get("native_encoding", True))
        try:
            self._native_max_subs = min(
                5, max(1, int(config.get("native_max_subs") or 2))
            )
        except (TypeError, ValueError):
            self._native_max_subs = 2
        try:
            self._native_request_delay = max(
                0.0, float(config.get("native_request_delay") or 1.0)
            )
        except (TypeError, ValueError):
            self._native_request_delay = 1.0
        self._native_engine = (
            NativeSubtitleEngine(self) if NativeSubtitleEngine is not None else None
        )
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
        """Expose health, manual scan and pending-queue endpoints."""

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
                "path": "/diagnose",
                "endpoint": self.api_diagnose,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "自检：字幕源、服务连通性与最近错误",
                "description": "不返回任何密钥，仅返回布尔状态与最近几次处理结果。",
            },
            {
                "path": "/inspect",
                "endpoint": self.api_inspect,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "检查单个视频的字幕情况",
                "description": "参数 path：视频绝对路径。返回目录内字幕文件、识别到的语言与内封字幕。",
            },
            {
                "path": "/queue",
                "endpoint": self.api_queue,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看待补全字幕队列",
                "description": "返回扫描/整理后仍缺字幕的视频清单。",
            },
            {
                "path": "/queue/download",
                "endpoint": self.api_queue_download,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动下载选中条目的字幕",
                "description": "参数 keys：队列条目键列表；all=true 表示重试全部。",
            },
            {
                "path": "/queue/clear",
                "endpoint": self.api_queue_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清理队列中已解决或文件已不存在的条目",
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

    # -- 待补全队列 API -----------------------------------------------

    async def api_diagnose(self) -> Any:
        """Self-check report: sources, service reachability, recent errors."""

        native = self._native_engine.describe() if self._native_engine else []
        healthy = await asyncio.to_thread(self._service_healthy)
        history = self._load_history()
        recent = []
        for item in history[:5]:
            result = item.get("result") or {}
            recent.append(
                {
                    "title": item.get("title"),
                    "status": result.get("status"),
                    "downloaded": len(result.get("downloaded") or []),
                    "errors": result.get("errors") or [],
                    "notes": result.get("notes") or [],
                }
            )
        return schemas.Response(
            success=True,
            message="诊断完成",
            data={
                "mode": self._native_mode,
                "native_ready": self._native_ready,
                "native_sources": native,
                "subtitle_best_key": bool(self._subtitle_best_api_key),
                "assrt_token": bool(self._assrt_token),
                "xunlei": self._native_xunlei,
                "shooter": self._native_shooter,
                "service_url": self._service_url,
                "service_healthy": healthy,
                "scan_dry_run": self._scan_dry_run,
                "queue": len(self._load_queue()),
                "recent": recent,
            },
        )

    async def api_inspect(self, path: str = "") -> Any:
        """Inspect one video: subtitle files beside it and detected languages."""

        video = Path(str(path or ""))
        if not path or not video.is_file():
            return schemas.Response(success=False, message=f"视频不存在：{path}")
        if self._native_engine is None:
            return schemas.Response(success=False, message="内置字幕引擎不可用")
        try:
            data = await self._native_engine.inspect(video)
        except Exception as exc:  # noqa: BLE001
            return schemas.Response(success=False, message=f"检查失败：{exc}")
        data["required"] = self._native_engine.required_langs(self._languages)
        return schemas.Response(success=True, message="检查完成", data=data)

    def api_queue(self) -> list[dict[str, Any]]:
        """Return the pending-subtitle queue."""

        return self._load_queue()

    def _resolve_queue_entries(
        self,
        queue: list[dict[str, Any]],
        ids: list[str],
        retry_all: bool = False,
    ) -> list[dict[str, Any]]:
        """Resolve checkbox ids (movie row or season row) to queue entries."""

        if retry_all:
            return list(queue)
        if not ids:
            return []
        selected = set(ids)
        result: list[dict[str, Any]] = []
        for entry in queue:
            key = str(entry.get("key") or "")
            group = (
                self._season_group_id(entry)
                if str(entry.get("media_type") or "movie") == "tv"
                else "m:" + key
            )
            if group in selected or key in selected:
                result.append(entry)
        return result

    async def api_queue_download(
        self, payload: Optional[dict] = Body(default=None)
    ) -> Any:
        """Download subtitles for the selected queue entries (background task)."""

        if not self._enabled:
            return schemas.Response(success=False, message="插件未启用")
        if self._queue_running:
            return schemas.Response(
                success=False, message="队列任务正在运行，请稍后再试"
            )

        payload = payload or {}
        keys = [str(key) for key in payload.get("keys") or [] if str(key)]
        retry_all = bool(payload.get("all"))
        if not keys and not retry_all:
            return schemas.Response(success=False, message="请先勾选要下载的条目")

        queue = self._load_queue()
        targets = self._resolve_queue_entries(queue, keys, retry_all)
        if not targets:
            return schemas.Response(
                success=False,
                message=f"队列中没有匹配的条目（收到 {len(keys)} 个选择）",
            )

        if self._native_mode != "off" and not self._native_ready:
            healthy = await asyncio.to_thread(self._service_healthy)
            if not healthy:
                return schemas.Response(
                    success=False,
                    message=(
                        "没有可用的字幕源：请填写 SubtitleBest API Key / ASSRT Token，"
                        "或启用迅雷字幕，或启动 SubtitleForge 服务"
                    ),
                )

        logger.info(
            f"SubtitleForge 队列手动下载：选择 {len(keys)} 项，匹配 {len(targets)} 个视频"
        )
        try:
            self._queue_task = asyncio.get_running_loop().create_task(
                self._queue_worker([str(entry.get("key")) for entry in targets])
            )
        except RuntimeError as err:
            logger.error(f"无法启动队列任务：{err}")
            return schemas.Response(success=False, message=f"无法启动后台任务：{err}")
        return schemas.Response(
            success=True,
            message=f"已开始为 {len(targets)} 个视频补全字幕，稍后刷新页面查看结果",
            data={"count": len(targets)},
        )

    async def api_queue_clear(self) -> Any:
        """Drop queue entries whose file is gone or whose subtitles are complete."""

        queue = self._load_queue()
        engine = self._native_engine
        keep: list[dict[str, Any]] = []
        removed = 0
        for entry in queue:
            path = Path(str(entry.get("path") or ""))
            if not path.is_file():
                removed += 1
                continue
            if engine is not None and entry.get("missing"):
                try:
                    have = await engine.scan_existing(path)
                    need = engine.required_langs(entry.get("missing") or [])
                    if need and set(need) <= have:
                        removed += 1
                        continue
                except Exception as err:  # noqa: BLE001 - cleanup is best effort
                    logger.debug(f"队列清理检查失败：{err}")
            keep.append(entry)
        self._save_queue(keep)
        return schemas.Response(
            success=True,
            message=f"已清理 {removed} 条，剩余 {len(keep)} 条",
            data={"removed": removed, "remaining": len(keep)},
        )

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
        payload = {
            "paths": self._split_list(self._scan_paths),
            "exclude_paths": self._split_list(self._scan_exclude_paths),
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
            result = await self._run_scan(payload)
            result["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result["trigger"] = "manual" if manual else "schedule"
            self.save_data(
                self._latest_scan_key,
                json.dumps(result, ensure_ascii=False),
            )
            logger.info(
                "SubtitleForge 媒体库扫描完成"
                f"{'（仅预览）' if result.get('dry_run') else ''}："
                f"发现 {result.get('discovered', 0)}，"
                f"检查 {result.get('checked', 0)}，"
                f"缺失 {result.get('queued', 0)}，"
                f"下载 {result.get('downloaded', 0)}"
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

    async def _run_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按 native_mode 执行扫描，返回与 `/v1/scan` 一致的结构。

        - auto（默认）：内置源可用时用内置引擎扫描（与「内置源优先」一致），
          内置源不可用或扫描异常时才交给服务。
        - native_only：只用内置引擎。
        - service_first：先服务，失败后再用内置引擎。
        - off：只用服务，保持原有行为。
        """

        mode = self._native_mode
        native_result: dict[str, Any] | None = None
        service_result: dict[str, Any] | None = None

        if mode == "off":
            service_result = await self._call_service_scan(payload)
        elif mode == "service_first":
            service_result = await self._call_service_scan(payload)
            if service_result.get("status") == "error":
                native_result = await self._native_scan(payload)
        elif mode == "native_only":
            native_result = await self._native_scan(payload)
        else:  # auto
            if self._native_ready:
                native_result = await self._native_scan(payload)
                if (
                    native_result.get("status") == "error"
                    and await asyncio.to_thread(self._service_healthy)
                ):
                    service_result = await self._call_service_scan(payload)
            else:
                logger.warning(
                    "SubtitleForge 内置源不可用（未配置 SubtitleBest API Key / ASSRT Token，"
                    "且迅雷源已关闭），本次扫描交给服务"
                )
                service_result = await self._call_service_scan(payload)

        result = self._merge_scan_results(native_result, service_result)
        try:
            self._queue_sync_from_scan(result)
            await self._enrich_queue_posters()
        except Exception as err:  # noqa: BLE001 - queue bookkeeping must not fail the scan
            logger.warning(f"更新待补全队列失败：{err}")
        return result

    async def _native_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one native library scan; never raises."""

        if not self._native_ready:
            return {"status": "error", "errors": ["没有可用的内置字幕源"]}
        try:
            result = await self._native_engine.scan(payload)
            logger.info(
                "SubtitleForge 内置扫描完成："
                f"检查 {result.get('checked', 0)}，"
                f"缺失 {result.get('queued', 0)}，"
                f"下载 {result.get('downloaded', 0)}"
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SubtitleForge 内置扫描失败：{type(exc).__name__}: {exc}")
            return {"status": "error", "errors": [str(exc)]}

    async def _call_service_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Call the SubtitleForge scan endpoint; never raises."""

        try:
            return await asyncio.to_thread(
                self._request_json, "POST", "/v1/scan", payload, 3600
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SubtitleForge 服务扫描失败：{type(exc).__name__}: {exc}")
            return {"status": "error", "errors": [str(exc)]}

    @staticmethod
    def _merge_scan_results(
        native: dict[str, Any] | None, service: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Merge native and service scan summaries without double counting."""

        if native is None and service is None:
            return {"status": "error", "errors": ["没有可用的字幕来源"]}
        if native is None:
            return service or {}
        if service is None:
            return native

        def number(source: dict[str, Any], key: str) -> int:
            try:
                return int(source.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "status": "ok",
            "dry_run": bool(native.get("dry_run") or service.get("dry_run")),
            "discovered": max(number(native, "discovered"), number(service, "discovered")),
            "checked": max(number(native, "checked"), number(service, "checked")),
            "existing": max(number(native, "existing"), number(service, "existing")),
            "queued": max(number(native, "queued"), number(service, "queued")),
            "queue_remaining": number(service, "queue_remaining"),
            "downloaded": number(native, "downloaded") + number(service, "downloaded"),
            "downloads_today": max(
                number(native, "downloads_today"), number(service, "downloads_today")
            ),
            "daily_download_limit": max(
                number(native, "daily_download_limit"),
                number(service, "daily_download_limit"),
            ),
            "items": list(native.get("items") or []) + list(service.get("items") or []),
            "errors": list(native.get("errors") or []) + list(service.get("errors") or []),
        }

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
            "SF_PROVIDER_ORDER": self._provider_order,
            "SF_SUBTITLE_BEST_API_KEY": self._subtitle_best_api_key,
            "SF_SUBTITLE_BEST_HEADER_TOKEN": self._subtitle_best_header_token,
            "SF_OPENSUBTITLES_API_KEY": self._opensubtitles_api_key,
            "SF_OPENSUBTITLES_USERNAME": self._opensubtitles_username,
            "SF_OPENSUBTITLES_PASSWORD": self._opensubtitles_password,
            "SF_ASSRT_TOKEN": self._assrt_token,
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

    def _read_service_env(self) -> dict[str, str]:
        """Read the service's private .env so native providers can reuse it."""

        env_file = self._service_dir / ".env"
        values: dict[str, str] = {}
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return values
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

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
        common_payload = {
            "title": str(getattr(mediainfo, "title", "") or ""),
            "original_title": getattr(mediainfo, "original_title", None),
            "english_title": getattr(mediainfo, "en_title", None),
            "year": self._optional_int(getattr(mediainfo, "year", None)),
            "imdb_id": getattr(mediainfo, "imdb_id", None),
            "poster": self._poster_url(getattr(mediainfo, "poster_path", None)),
            "tmdb_id": self._optional_int(getattr(mediainfo, "tmdb_id", None)),
            "media_type": media_type,
            "season": self._optional_int(getattr(meta, "begin_season", None)),
            "episode": self._optional_int(getattr(meta, "begin_episode", None)),
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
            result = await self._process_payload(payload)
            logger.info(
                f"SubtitleForge: {payload['title']} -> {result.get('status')}, "
                f"downloaded={len(result.get('downloaded') or [])}"
            )
            self._add_history(payload, result)
            self._queue_sync_from_process(payload, result)
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

    # -- 内置字幕源与服务编排 -----------------------------------------

    @property
    def _native_ready(self) -> bool:
        """内置引擎是否至少有一个可用来源。"""

        engine = self._native_engine
        return engine is not None and bool(engine.has_source())

    async def _process_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按 native_mode 决定内置源与服务的先后顺序，并合并结果。"""

        mode = self._native_mode
        native_result: dict[str, Any] | None = None
        service_result: dict[str, Any] | None = None

        if mode in {"auto", "native_only"} and self._native_ready:
            try:
                native_result = await self._native_engine.process(payload)
                logger.info(
                    f"SubtitleForge 内置源：{payload.get('title')} -> "
                    f"{native_result.get('status')}，"
                    f"下载 {len(native_result.get('downloaded') or [])}"
                )
            except Exception as exc:  # noqa: BLE001 - one path must not break the run
                logger.error(
                    f"SubtitleForge 内置源处理失败：{type(exc).__name__}: {exc}"
                )
                native_result = {
                    "status": "error",
                    "downloaded": [],
                    "errors": [str(exc)],
                }

        needs_service = mode in {"off", "service_first"} or (
            mode == "auto" and bool((native_result or {}).get("missing"))
        )
        if needs_service:
            service_result = await self._call_service_process(payload)

        return self._merge_process_results(native_result, service_result)

    async def _call_service_process(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Call the SubtitleForge service; never raises."""

        try:
            return await asyncio.to_thread(
                self._request_json, "POST", "/v1/process", payload
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SubtitleForge 服务处理失败：{type(exc).__name__}: {exc}")
            return {"status": "error", "downloaded": [], "errors": [str(exc)]}

    @staticmethod
    def _merge_process_results(
        native: dict[str, Any] | None, service: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Merge native and service results into one service-shaped result."""

        if native is None and service is None:
            return {
                "status": "error",
                "downloaded": [],
                "errors": ["没有可用的字幕来源"],
            }
        if native is None:
            return service or {}
        if service is None:
            return native

        downloaded = list(native.get("downloaded") or []) + list(
            service.get("downloaded") or []
        )
        errors = list(native.get("errors") or []) + list(service.get("errors") or [])
        notes = list(native.get("notes") or []) + list(service.get("notes") or [])
        missing = service.get("missing") or native.get("missing") or []
        if downloaded:
            status = "downloaded"
        elif "error" in {native.get("status"), service.get("status")}:
            status = "error"
        elif missing:
            status = "not_found"
        else:
            status = native.get("status") or service.get("status") or "ok"
        return {
            "status": status,
            "downloaded": downloaded,
            "errors": errors,
            "notes": notes,
            "missing": missing,
        }

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
                            "placeholder": "subtitle_best,opensubtitles,assrt,custom",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "字幕源密码留空表示保留 NAS 上已有的值。保存后插件会重载服务，健康页会显示每个源是否已配置。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "success",
                            "variant": "tonal",
                            "text": "内置字幕源：SubtitleBest、ASSRT、迅雷、Shooter 直接在 MoviePilot 进程内检索，不需要 SubtitleForge 服务；下面填写的 SubtitleBest API Key 与 ASSRT Token 会同时供内置源使用，也会自动读取服务 .env 里的同名配置。默认「自动」：整理和扫描都先用内置源，内置源不可用或出错时才交给服务。",
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "native_mode",
                            "label": "内置源与服务的使用顺序",
                            "items": [
                                {
                                    "title": "自动：内置源优先，服务兜底（推荐）",
                                    "value": "auto",
                                },
                                {
                                    "title": "仅内置源：完全不依赖 SubtitleForge 服务",
                                    "value": "native_only",
                                },
                                {
                                    "title": "服务优先：服务失败后再用内置源",
                                    "value": "service_first",
                                },
                                {
                                    "title": "仅服务：保持原有行为",
                                    "value": "off",
                                },
                            ],
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
                                            "model": "native_xunlei",
                                            "label": "迅雷字幕（文件 cid）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "native_shooter",
                                            "label": "Shooter（文件 hash）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "native_auto_sync",
                                            "label": "自动调轴（需 ffsubsync）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "native_encoding",
                                            "label": "字幕转 UTF-8",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "native_tag_style",
                                            "label": "内置源字幕语言标签",
                                            "items": [
                                                {
                                                    "title": "zh-CN / zh-TW（Emby、Jellyfin）",
                                                    "value": "iso",
                                                },
                                                {
                                                    "title": "chs / cht（老播放器）",
                                                    "value": "legacy",
                                                },
                                                {
                                                    "title": "zh-Hans / zh-Hant（Plex）",
                                                    "value": "plex",
                                                },
                                            ],
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
                                            "model": "native_max_subs",
                                            "label": "每个视频最多下载条数",
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
                                            "model": "native_request_delay",
                                            "label": "请求间隔（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
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
                            "label": "SubtitleBest 客户端 Token",
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
                            "placeholder": "http://subtitleforge:8600",
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
            "service_url": "http://subtitleforge:8600",
            "api_token": "",
            "languages": ["zh-Hans", "zh-Hant"],
            "overwrite": False,
            "notify": True,
            "timeout": 180,
            "dry_run": False,
            "manage_service": False,
            "service_dir": "/config/subtitleforge",
            "provider_order": "subtitle_best,opensubtitles,assrt,custom",
            "subtitle_best_api_key": "",
            "subtitle_best_header_token": "",
            "opensubtitles_api_key": "",
            "opensubtitles_username": "",
            "opensubtitles_password": "",
            "assrt_token": "",
            "custom_provider_url": "",
            "custom_provider_token": "",
            "scan_enabled": False,
            "scan_paths": (
                "/plex/电影/外语电影,/plex/电视剧/欧美剧,/plex/电视剧/日韩剧,"
                "/plex/电视剧/纪录片,/plex/动漫"
            ),
            "scan_exclude_paths": "/plex/电影/华语电影,/plex/电视剧/国产剧",
            "scan_video_extensions": (
                ".mkv,.mp4,.avi,.mov,.wmv,.m4v,.ts,.m2ts,.webm"
            ),
            "scan_interval_hours": 24,
            "scan_max_files": 10,
            "scan_daily_download_limit": 10,
            "scan_dry_run": True,
            "scan_provider_order": "",
            "scan_languages": ["zh-Hans"],
            "native_mode": "auto",
            "native_xunlei": True,
            "native_shooter": False,
            "native_tag_style": "iso",
            "native_auto_sync": False,
            "native_encoding": True,
            "native_max_subs": 2,
            "native_request_delay": 1.0,
        }

    def get_page(self) -> list[dict]:
        """Render the latest library scan and recent processing history."""

        history = self._load_history()
        latest_scan = self._load_latest_scan()
        cards: list[dict] = []
        queue = self._load_queue()
        if queue:
            cards.append(self._queue_card(queue))
        if self._native_engine is not None:
            sources = self._native_engine.describe()
            ready = [item["name"] for item in sources if item["ready"]]
            mode_label = {
                "auto": "自动（内置源优先）",
                "native_only": "仅内置源",
                "service_first": "服务优先",
                "off": "仅服务",
            }.get(self._native_mode, self._native_mode)
            cards.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-3", "variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "font-weight-bold"},
                                    "text": f"字幕源 · {mode_label}",
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption"},
                                    "text": (
                                        "内置可用："
                                        + ("、".join(ready) if ready else "无（填写 SubtitleBest API Key 或 ASSRT Token 即可启用）")
                                    ),
                                },
                            ],
                        }
                    ],
                }
            )
        if latest_scan:
            items = latest_scan.get("items") or []
            missing = [
                item
                for item in items
                if item.get("status")
                in {"dry_run", "queued", "not_found", "partial", "error"}
            ]
            cards.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-3", "variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "font-weight-bold"},
                                    "text": (
                                        f"媒体库扫描 · {latest_scan.get('time', '')} · "
                                        f"{'仅预览' if latest_scan.get('dry_run') else '自动补全'}"
                                    ),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption"},
                                    "text": (
                                        f"发现 {latest_scan.get('discovered', 0)} · "
                                        f"检查 {latest_scan.get('checked', 0)} · "
                                        f"已有 {latest_scan.get('existing', 0)} · "
                                        f"缺失入队 {latest_scan.get('queued', 0)} · "
                                        f"队列剩余 {latest_scan.get('queue_remaining', 0)} · "
                                        f"下载 {latest_scan.get('downloaded', 0)} · "
                                        f"今日 {latest_scan.get('downloads_today', 0)}/"
                                        f"{latest_scan.get('daily_download_limit', 0)}"
                                    ),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption mt-2"},
                                    "text": (
                                        "；".join(
                                            f"{item.get('title', '')}: "
                                            f"{self._status_label(item.get('status'))}"
                                            for item in missing[:5]
                                        )
                                        or "本轮没有发现需要补全的条目"
                                    ),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption text-warning mt-1"},
                                    "text": (
                                        f"本轮没有下载字幕：{int(latest_scan.get('queued') or 0)} 条已进入"
                                        "待补全队列，可在上方表格勾选后手动下载。"
                                    ),
                                } if (
                                    int(latest_scan.get("downloaded") or 0) == 0
                                    and int(latest_scan.get("queued") or 0) > 0
                                ) else {
                                    "component": "span",
                                    "props": {"class": "d-none"},
                                    "text": "",
                                },
                            ],
                        }
                    ],
                }
            )
        if not history and not latest_scan and not queue:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无记录。启用媒体库扫描后会定时检查外挂和内嵌中文字幕。",
                    },
                }
            ]
        for item in history:
            result = item.get("result") or {}
            downloaded = result.get("downloaded") or []
            cards.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-2"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "font-weight-bold"},
                                    "text": f"{item.get('time', '')} · {item.get('title', '')}",
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption"},
                                    "text": (
                                        f"状态: {result.get('status', 'unknown')} · "
                                        f"下载: {len(downloaded)} · "
                                        f"错误: {len(result.get('errors') or [])} · "
                                        f"提示: {len(result.get('notes') or [])}"
                                    ),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption mt-1"},
                                    "text": "\n".join(
                                        [
                                            f"· {Path(str(item.get('path') or '')).name} "
                                            f"[{item.get('language', '')}]"
                                            for item in downloaded[:3]
                                            if isinstance(item, dict)
                                        ]
                                        + [
                                            f"！{message}"
                                            for message in (
                                                (result.get("errors") or [])
                                                + (result.get("notes") or [])
                                            )[:2]
                                        ]
                                    ),
                                },
                            ],
                        }
                    ],
                }
            )
        return cards


    # -- 队列页面 -----------------------------------------------------

    @staticmethod
    def _season_group_id(entry: dict[str, Any]) -> str:
        """Stable id for one (title, season) group."""

        season = entry.get("season")
        raw = f"{entry.get('title') or ''}|{int(season) if season is not None else 0}"
        return "s:" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _status_rank(status: str) -> int:
        order = {"error": 4, "not_found": 3, "partial": 2, "queued": 1, "dry_run": 0}
        return order.get(str(status or ""), 0)

    def _queue_groups(
        self, queue: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split the queue into movie rows and one row per TV season."""

        movies: list[dict[str, Any]] = []
        buckets: dict[str, list[dict[str, Any]]] = {}

        for entry in queue:
            if str(entry.get("media_type") or "movie") != "tv":
                movies.append(
                    {
                        "id": "m:" + str(entry.get("key") or ""),
                        "kind": "movie",
                        "title": entry.get("title") or "",
                        "year": entry.get("year") or "",
                        "season": None,
                        "episodes": [],
                        "poster": entry.get("poster") or "",
                        "missing": sorted(set(entry.get("missing") or [])),
                        "status": str(entry.get("status") or ""),
                        "updated": str(entry.get("updated") or ""),
                        "entries": [entry],
                    }
                )
                continue
            buckets.setdefault(self._season_group_id(entry), []).append(entry)

        seasons: list[dict[str, Any]] = []
        for group_id, entries in buckets.items():
            entries.sort(key=lambda item: int(item.get("episode") or 0))
            episodes = sorted(
                int(item["episode"])
                for item in entries
                if item.get("episode") is not None
            )
            worst = max(entries, key=lambda item: self._status_rank(item.get("status")))
            seasons.append(
                {
                    "id": group_id,
                    "kind": "season",
                    "title": entries[0].get("title") or "",
                    "year": entries[0].get("year") or "",
                    "season": entries[0].get("season"),
                    "episodes": episodes,
                    "poster": next(
                        (item.get("poster") for item in entries if item.get("poster")),
                        "",
                    ),
                    "missing": sorted(
                        {lang for item in entries for lang in item.get("missing") or []}
                    ),
                    "status": str(worst.get("status") or ""),
                    "updated": max(str(item.get("updated") or "") for item in entries),
                    "entries": entries,
                }
            )

        movies.sort(key=lambda item: item.get("updated") or "", reverse=True)
        seasons.sort(
            key=lambda item: (item.get("title") or "", int(item.get("season") or 0))
        )
        return movies, seasons

    @staticmethod
    def _format_episodes(episodes: list[int]) -> str:
        """Compact episode list, e.g. 3 集（E01、E05、E09）."""

        if not episodes:
            return "-"
        shown = "、".join(f"E{episode:02d}" for episode in episodes[:6])
        if len(episodes) > 6:
            shown += f" 等 {len(episodes)} 集"
        return f"{len(episodes)} 集（{shown}）"

    def _queue_card(self, queue: list[dict[str, Any]]) -> dict[str, Any]:
        """Render the pending queue grouped by movies and TV seasons."""

        # 页面渲染时先尝试本地 NFO 海报，避免依赖 TMDB 接口
        poster_changed = False
        for entry in queue:
            poster = self._poster_from_nfo(Path(str(entry.get("path") or "")))
            if poster and entry.get("poster") != poster:
                entry["poster"] = poster
                poster_changed = True
        if poster_changed:
            self._save_queue(queue)

        movies, seasons = self._queue_groups(queue)
        episode_count = sum(len(item["entries"]) for item in seasons)
        content: list[dict[str, Any]] = []

        if self._scan_dry_run:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "class": "mb-2",
                        "text": (
                            "当前扫描为「仅预览」：只检查并写入队列，不会自动下载。"
                            "可以勾选下面条目手动补字幕，或在插件设置里关闭「仅预览」让扫描自动下载。"
                        ),
                    },
                }
            )
        content.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center ga-2 mb-2"},
                "content": [
                    {
                        "component": "VChip",
                        "props": {"variant": "tonal", "color": "primary"},
                        "text": f"待补全 {len(movies)} 部电影 · {len(seasons)} 季 {episode_count} 集",
                    },
                    {
                        "component": "VChip",
                        "props": {"variant": "tonal"},
                        "text": f"字幕源 {self._native_mode}",
                    },
                ],
            }
        )

        if movies:
            content.append(
                {
                    "component": "div",
                    "props": {"class": "text-subtitle-2 mt-2 mb-1"},
                    "text": f"电影（{len(movies)}）",
                }
            )
            content.append(self._queue_table(movies, season_view=False))
        if seasons:
            content.append(
                {
                    "component": "div",
                    "props": {"class": "text-subtitle-2 mt-4 mb-1"},
                    "text": f"剧集 · 按季（{len(seasons)} 季）",
                }
            )
            content.append(self._queue_table(seasons, season_view=True))

        content.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center ga-2 mt-3"},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "flat",
                            "prepend-icon": "mdi-download",
                            "onclick": self._queue_onclick("download"),
                        },
                        "text": "下载选中",
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "secondary",
                            "variant": "tonal",
                            "prepend-icon": "mdi-refresh",
                            "onclick": self._queue_onclick("retry"),
                        },
                        "text": "全部重试",
                    },
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "default",
                            "variant": "text",
                            "prepend-icon": "mdi-broom",
                            "onclick": self._queue_onclick("clear"),
                        },
                        "text": "清理队列",
                    },
                ],
            }
        )
        return {
            "component": "VCard",
            "props": {"class": "mb-3"},
            "content": [{"component": "VCardText", "content": content}],
        }

    def _queue_table(
        self, groups: list[dict[str, Any]], season_view: bool
    ) -> dict[str, Any]:
        """Build one table (movies or TV seasons)."""

        status_labels = {
            "dry_run": "待下载",
            "queued": "排队中",
            "not_found": "未找到",
            "partial": "部分缺失",
            "error": "失败",
            "downloaded": "已下载",
        }
        headers = ["", "海报", "标题"]
        if season_view:
            headers += ["季", "缺失集数"]
        headers += ["缺失语言", "状态", "最近尝试"]

        rows: list[dict[str, Any]] = []
        for group in groups:
            title = str(group.get("title") or "")
            year = str(group.get("year") or "")
            poster = str(group.get("poster") or "")
            status = status_labels.get(
                str(group.get("status") or ""), str(group.get("status") or "-")
            )
            reason = ""
            for entry in group.get("entries") or []:
                if entry.get("reason"):
                    reason = str(entry.get("reason"))
                    break
            cells: list[dict[str, Any]] = [
                {
                    "component": "td",
                    "content": [
                        {
                            "component": "input",
                            "props": {
                                "type": "checkbox",
                                "class": "csf-queue",
                                "value": str(group.get("id") or ""),
                                "aria-label": f"选择 {title}",
                            },
                        }
                    ],
                },
                {
                    "component": "td",
                    "content": [
                        (
                            {
                                "component": "VImg",
                                "props": {
                                    "src": poster,
                                    "alt": title,
                                    "width": 40,
                                    "height": 60,
                                    "cover": True,
                                    "class": "rounded",
                                },
                            }
                            if poster
                            else {
                                "component": "span",
                                "props": {"class": "text-caption text-grey"},
                                "text": "—",
                            }
                        )
                    ],
                },
                {
                    "component": "td",
                    "props": {"style": "max-width: 220px; white-space: normal"},
                    "text": f"{title} ({year})" if year else title,
                },
            ]
            if season_view:
                season = group.get("season")
                cells.append(
                    {
                        "component": "td",
                        "text": f"第{int(season)}季" if season is not None else "未标季",
                    }
                )
                cells.append(
                    {
                        "component": "td",
                        "text": self._format_episodes(group.get("episodes") or []),
                    }
                )
            cells.append(
                {
                    "component": "td",
                    "text": "、".join(group.get("missing") or []) or "-",
                }
            )
            cells.append(
                {
                    "component": "td",
                    "text": f"{status}｜{reason}" if reason else status,
                }
            )
            cells.append(
                {
                    "component": "td",
                    "props": {"class": "text-caption text-grey"},
                    "text": str(group.get("updated") or "-"),
                }
            )
            rows.append({"component": "tr", "content": cells})

        select_all = {
            "component": "input",
            "props": {
                "type": "checkbox",
                "aria-label": "全选待补全条目",
                "onclick": (
                    "var checked=this.checked;"
                    "Array.prototype.forEach.call("
                    "document.querySelectorAll('input.csf-queue'),"
                    "function(node){node.checked=checked;}"
                    ");"
                ),
            },
        }
        return {
            "component": "div",
            "props": {"style": "overflow-x:auto"},
            "content": [
                {
                    "component": "VTable",
                    "props": {"density": "compact", "hover": True},
                    "content": [
                        {
                            "component": "thead",
                            "content": [
                                {
                                    "component": "tr",
                                    "content": [
                                        {"component": "th", "content": [select_all]},
                                        *[
                                            {"component": "th", "text": name}
                                            for name in headers[1:]
                                        ],
                                    ],
                                }
                            ],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }
            ],
        }

    def _queue_onclick(self, action: str) -> str:
        """Build the inline JS for the queue buttons."""

        api = json.dumps(f"plugin/{self.__class__.__name__}/queue/download")
        clear_api = json.dumps(f"plugin/{self.__class__.__name__}/queue/clear")
        if action == "clear":
            return (
                "(function(){"
                "if(!confirm('清理文件已不存在或字幕已补齐的队列条目？')){return;}"
                f"window.MoviePilotAPI.post({clear_api},{{}})"
                ".then(function(r){alert((r&&r.message)||'已清理');location.reload();})"
                ".catch(function(err){console.error(err);alert('清理失败');});"
                "})()"
            )

        if action == "retry":
            guard = ""
            body = "{all:true}"
            prompt = "将重试队列中全部条目，继续？"
        else:
            guard = "if(!keys.length){alert('请先勾选要补字幕的视频');return;}"
            body = "{keys:keys}"
            prompt = "为选中的视频补全字幕？"

        return (
            "(function(){"
            "var nodes=document.querySelectorAll('input.csf-queue:checked');"
            "var keys=Array.prototype.map.call(nodes,function(node){return node.value;});"
            f"{guard}"
            f"if(!confirm('{prompt}')){{return;}}"
            f"window.MoviePilotAPI.post({api},{body})"
            ".then(function(r){"
            "if(!r||r.success===false){alert((r&&r.message)||'提交失败');return;}"
            "alert(r.message||'已提交');"
            "setTimeout(function(){location.reload();},1500);"
            "}).catch(function(err){console.error(err);alert('请求失败');});"
            "})()"
        )

    def stop_service(self) -> None:
        """Stop the optional local service process owned by this plugin."""

        self._stop_managed_service()

    # -- 待补全字幕队列 -----------------------------------------------

    @staticmethod
    def _queue_key_for(path: str) -> str:
        """Stable short key for a video path (used by the page checkboxes)."""

        return hashlib.md5(str(path).encode("utf-8")).hexdigest()[:12]

    def _load_queue(self) -> list[dict[str, Any]]:
        """Read the pending-subtitle queue."""

        value = self.get_data(self._queue_key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        return value if isinstance(value, list) else []

    def _save_queue(self, queue: list[dict[str, Any]]) -> None:
        """Persist the queue, newest first, bounded."""

        self.save_data(
            self._queue_key,
            json.dumps(queue[: self._queue_max], ensure_ascii=False),
        )

    @staticmethod
    def _looks_like_filename(text: str) -> bool:
        """True when a queue title is really a release filename."""

        value = str(text or "")
        if not value:
            return False
        if re.search(r"\.(mkv|mp4|avi|ts|m2ts|rmvb|mov|wmv)$", value, re.IGNORECASE):
            return True
        return bool(
            re.search(
                r"\b(1080p|2160p|720p|480p|WEB-?DL|WEBRip|BluRay|BDRip|H\.?26[45]|HEVC|"
                r"x26[45]|DDP|DTS|Atmos|HDR10?|REMUX)\b",
                value,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def _clean_title(title: str) -> str:
        """Strip release tags, episode markers and years from a display title."""

        original = str(title or "").strip()
        text = re.sub(r"\.(mkv|mp4|avi|ts|m2ts|rmvb|mov|wmv)$", "", original, flags=re.I)
        text = re.sub(r"[Ss]\d{1,2}\s*[Ee]\d{1,3}.*$", "", text)
        text = re.sub(r"\((19|20)\d{2}\).*$", "", text)
        text = re.sub(r"\b(19|20)\d{2}\b.*$", "", text)
        text = re.sub(
            r"\b(1080p|2160p|720p|480p|WEB-?DL|WEBRip|BluRay|BDRip|H\.?26[45]|HEVC|"
            r"x26[45]|DDP|DTS|Atmos|HDR10?|REMUX)\b.*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"[._]+", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" -")
        # 中文标题后面常跟发布组英文名，截到最后一个中文/数字为止
        if re.search(r"[\u4e00-\u9fff]", text):
            matches = list(re.finditer(r"[\u4e00-\u9fff0-9：:·、！？!?]", text))
            if matches:
                text = text[: matches[-1].end()].strip(" -")
        return text or original

    def _normalize_queue_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Turn a raw filename into a clean title plus season/episode."""

        path = str(item.get("path") or "")
        if not path:
            return item
        title = str(item.get("title") or "").strip()
        needs_parse = (
            not title
            or self._looks_like_filename(title)
            or (item.get("season") is None and item.get("episode") is None)
        )
        if needs_parse and self._native_engine is not None:
            try:
                req = self._native_engine.request_from_payload(
                    {"video_path": path, "title": ""}
                )
            except Exception as err:  # noqa: BLE001 - normalization is best effort
                logger.debug(f"队列标题解析失败：{err}")
                req = None
            if req is not None:
                if not title or self._looks_like_filename(title):
                    title = self._clean_title(req.title or Path(path).stem)
                if item.get("season") is None:
                    item["season"] = req.season
                if item.get("episode") is None:
                    item["episode"] = req.episode
                if not item.get("year"):
                    item["year"] = req.year
                if req.episode is not None:
                    item["media_type"] = "tv"
        item["title"] = title or Path(path).stem
        return item

    def _queue_upsert(
        self,
        queue: list[dict[str, Any]],
        item: dict[str, Any],
        status: str,
        reason: str = "",
    ) -> None:
        """Insert or refresh one queue entry."""

        item = self._normalize_queue_item(item)
        path = str(item.get("path") or "")
        if not path:
            return
        key = self._queue_key_for(path)
        existing = next((entry for entry in queue if entry.get("key") == key), None)
        entry = existing or {
            "key": key,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "attempts": 0,
        }
        missing = [str(value) for value in (item.get("missing") or [])]
        entry.update(
            {
                "path": path,
                "title": str(item.get("title") or Path(path).stem),
                "year": str(item.get("year") or ""),
                "media_type": str(
                    item.get("media_type")
                    or ("tv" if item.get("episode") is not None else "movie")
                ),
                "season": item.get("season"),
                "episode": item.get("episode"),
                "missing": missing or entry.get("missing") or [],
                "status": status,
                "reason": reason,
                "poster": str(item.get("poster") or entry.get("poster") or ""),
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        if existing is None:
            queue.insert(0, entry)

    def _queue_drop(self, queue: list[dict[str, Any]], path: str) -> bool:
        """Drop one entry by video path; returns whether something was removed."""

        key = self._queue_key_for(path)
        before = len(queue)
        queue[:] = [entry for entry in queue if entry.get("key") != key]
        return len(queue) != before

    def _queue_sync_from_scan(self, result: dict[str, Any]) -> None:
        """Reflect a scan result into the queue."""

        items = [item for item in result.get("items") or [] if isinstance(item, dict)]
        if not items:
            return
        queue = self._load_queue()
        changed = False
        for item in items:
            # 内置引擎用 path/missing，服务端用 video_path/missing_languages
            path = str(item.get("path") or item.get("video_path") or "")
            if not path:
                continue
            if self._native_engine is not None and self._native_engine.is_bdmv_segment(
                Path(path)
            ):
                logger.info(f"跳过蓝光原盘分片（按整盘处理）：{path}")
                changed = self._queue_drop(queue, path) or changed
                continue
            if item.get("downloaded"):
                changed = self._queue_drop(queue, path) or changed
                continue
            status = str(item.get("status") or "not_found")
            if status in {"downloaded", "ok", "completed"}:
                changed = self._queue_drop(queue, path) or changed
                continue
            normalized = {
                "path": path,
                "title": item.get("title") or "",
                "year": item.get("year") or "",
                "media_type": item.get("media_type") or "movie",
                "season": item.get("season"),
                "episode": item.get("episode"),
                "missing": item.get("missing") or item.get("missing_languages") or [],
                "poster": item.get("poster") or "",
            }
            reason = str(item.get("reason") or "") or "；".join(
                str(err) for err in (item.get("errors") or [])[:2]
            )
            self._queue_upsert(queue, normalized, status, reason)
            changed = True
        if changed:
            self._save_queue(queue)

    def _queue_sync_from_process(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Reflect a single-file result into the queue."""

        path = str(payload.get("video_path") or "")
        if not path:
            return
        queue = self._load_queue()
        if result.get("downloaded"):
            if self._queue_drop(queue, path):
                self._save_queue(queue)
            return
        if not (result.get("missing") or result.get("status") in {"not_found", "error"}):
            return
        item = {
            "path": path,
            "title": payload.get("title") or Path(path).stem,
            "year": payload.get("year") or "",
            "media_type": payload.get("media_type") or "movie",
            "season": payload.get("season"),
            "episode": payload.get("episode"),
            "missing": [str(value) for value in result.get("missing") or []],
            "poster": str(payload.get("poster") or ""),
        }
        reason = "；".join(
            str(err)
            for err in ((result.get("errors") or []) + (result.get("notes") or []))[:2]
        )
        self._queue_upsert(queue, item, str(result.get("status") or "not_found"), reason)
        self._save_queue(queue)

    @staticmethod
    def _poster_url(value: Any) -> str:
        """Normalize a TMDB poster path into a browser-usable URL."""

        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith("http"):
            return text
        try:
            from app.sdk.config import settings as cfg
        except ImportError:  # MoviePilot V2
            from app.core.config import settings as cfg
        builder = getattr(cfg, "TMDB_IMAGE_URL", None)
        if callable(builder):
            try:
                return str(builder(text))
            except Exception:  # noqa: BLE001 - fall back to the public domain
                pass
        return (
            "https://image.tmdb.org/t/p/w500"
            + (text if text.startswith("/") else f"/{text}")
        )

    @staticmethod
    def _poster_from_nfo(video: Path) -> str:
        """
        Read the poster URL from a scraped NFO beside the video.

        NFOs also carry actor thumbs (`<actor><thumb>…</thumb></actor>`), so
        those blocks are stripped first and only poster-ish tags are accepted.
        """

        folder = video.parent
        candidates = [
            folder / f"{video.stem}.nfo",
            folder / "movie.nfo",
            folder / "tvshow.nfo",
            folder.parent / "tvshow.nfo",
            folder.parent / "movie.nfo",
        ]
        for nfo in candidates:
            try:
                if not nfo.is_file():
                    continue
                text = nfo.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            # 去掉演员、剧照等无关条目，避免把演员头像当海报
            cleaned = text
            for block in ("actor", "actor_list", "fanart"):
                cleaned = re.sub(
                    rf"<{block}\b[^>]*>.*?</{block}>",
                    " ",
                    cleaned,
                    flags=re.IGNORECASE | re.DOTALL,
                )

            patterns = [
                r'<thumb[^>]*aspect\s*=\s*"poster"[^>]*>\s*([^<\s]+)\s*</thumb>',
                r"<poster[^>]*>\s*([^<\s]+)\s*</poster>",
                r'<thumb[^>]*type\s*=\s*"poster"[^>]*>\s*([^<\s]+)\s*</thumb>',
            ]
            for pattern in patterns:
                matched = re.search(pattern, cleaned, re.IGNORECASE)
                if matched:
                    url = matched.group(1).strip()
                    if url.startswith("http"):
                        return url
        return ""

    async def _enrich_queue_posters(self) -> None:
        """Fill missing posters for the first few queue entries (best effort)."""

        queue = self._load_queue()
        pending = [entry for entry in queue if not entry.get("poster")][:12]
        if not pending:
            return
        chain = getattr(self, "chain", None)
        if chain is None or not hasattr(chain, "async_match_tmdbinfo"):
            return

        changed = False
        for entry in pending:
            nfo_poster = self._poster_from_nfo(Path(str(entry.get("path") or "")))
            if nfo_poster:
                entry["poster"] = nfo_poster
                changed = True
                continue
            try:
                info = await chain.async_match_tmdbinfo(
                    name=str(entry.get("title") or ""),
                    mtype=(
                        MediaType.TV
                        if entry.get("episode") is not None
                        else MediaType.MOVIE
                    ),
                    year=str(entry.get("year") or "") or None,
                )
            except Exception as err:  # noqa: BLE001 - posters are optional
                logger.debug(f"队列海报识别失败：{err}")
                continue
            poster = ""
            if isinstance(info, dict):
                poster = str(info.get("poster_path") or info.get("poster") or "")
            elif info is not None:
                poster = str(getattr(info, "poster_path", "") or "")
            if not poster:
                continue
            entry["poster"] = self._poster_url(poster)
            changed = True
        if changed:
            self._save_queue(queue)

    async def _queue_worker(self, keys: list[str]) -> None:
        """Download subtitles for the selected queue entries, one by one."""

        self._queue_running = True
        try:
            for key in keys:
                entry = next(
                    (item for item in self._load_queue() if item.get("key") == key),
                    None,
                )
                if entry is None:
                    continue
                if self._native_engine is not None and self._native_engine.is_bdmv_segment(
                    Path(str(entry.get("path") or ""))
                ):
                    logger.info(f"跳过蓝光原盘分片：{entry.get('path')}")
                    queue = self._load_queue()
                    self._queue_drop(queue, str(entry.get("path") or ""))
                    self._save_queue(queue)
                    continue
                payload = {
                    "video_path": entry.get("path"),
                    "title": entry.get("title") or "",
                    "year": entry.get("year") or None,
                    "media_type": entry.get("media_type") or "movie",
                    "season": entry.get("season"),
                    "episode": entry.get("episode"),
                    "languages": entry.get("missing") or self._languages,
                    "overwrite": self._overwrite,
                    "dry_run": False,
                }
                try:
                    result = await self._process_payload(payload)
                except Exception as exc:  # noqa: BLE001 - keep the queue moving
                    logger.error(f"队列下载失败：{type(exc).__name__}: {exc}")
                    result = {"status": "error", "downloaded": [], "errors": [str(exc)]}

                queue = self._load_queue()
                current = next(
                    (item for item in queue if item.get("key") == key), None
                )
                if current is not None:
                    current["attempts"] = int(current.get("attempts") or 0) + 1
                    current["updated"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                if result.get("downloaded"):
                    self._queue_drop(queue, str(entry.get("path") or ""))
                    self._add_history(payload, result)
                else:
                    reason = "；".join(
                        str(err)
                        for err in (
                            (result.get("errors") or []) + (result.get("notes") or [])
                        )[:2]
                    )
                    logger.warning(
                        f"SubtitleForge 队列补全失败：{entry.get('title')}"
                        f" -> {result.get('status')} {reason}"
                    )
                    if current is not None:
                        current["status"] = str(result.get("status") or "not_found")
                        current["reason"] = reason
                        current["missing"] = [
                            str(value)
                            for value in (result.get("missing") or current.get("missing") or [])
                        ]
                self._save_queue(queue)
                await asyncio.sleep(max(0.5, self._native_request_delay))
        finally:
            self._queue_running = False

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
    def _status_label(status: Any) -> str:
        """Map SubtitleForge status codes to readable Chinese labels."""

        return {
            "queued": "排队中",
            "dry_run": "待下载",
            "not_found": "未找到",
            "partial": "部分缺失",
            "error": "失败",
            "downloaded": "已下载",
            "completed": "已完成",
            "skipped": "已跳过",
            "ok": "正常",
            "existing": "已有字幕",
            "busy": "忙碌",
        }.get(str(status or ""), str(status or "-"))

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
