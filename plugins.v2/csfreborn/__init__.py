"""MoviePilot V2/V3 adapter for SubtitleForge."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.schemas.types import EventType

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


class CSFReborn(_PluginBase):
    """Complete subtitles after transfers and during scheduled library scans."""

    plugin_name = "SubtitleForge 字幕补全"
    plugin_desc = (
        "监听 MoviePilot 整理完成事件，并定时扫描媒体库，为缺少中文字幕的视频自动补全。"
    )
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.3.1"
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
        self._scan_max_files = 20
        self._scan_dry_run = True
        self._scan_provider_order = ""
        self._managed_process: subprocess.Popen | None = None
        self._scan_running = False
        self._history_key = "download_history"
        self._latest_scan_key = "latest_scan"
        self._history_max = 30

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
                100, max(1, int(config.get("scan_max_files") or 20))
            )
        except (TypeError, ValueError):
            self._scan_max_files = 20
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
        """Expose health and manual scan endpoints."""

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

    @eventmanager.register(EventType.PluginAction)
    async def on_plugin_action(self, event: Event) -> None:
        """Run a scan when the MoviePilot remote command is invoked."""

        data = event.event_data if isinstance(event.event_data, dict) else {}
        if data.get("action") != "subtitleforge_scan":
            return
        result = await self.scan_library(manual=True)
        self.post_message(
            channel=data.get("channel"),
            mtype=MessageType.Plugin,
            title="SubtitleForge 字幕扫描",
            text=(
                f"状态：{result.get('status', 'unknown')}；"
                f"检查：{result.get('checked', 0)}；"
                f"下载：{result.get('downloaded', 0)}"
            ),
            userid=data.get("user"),
        )

    async def scan_library(self, manual: bool = False) -> dict[str, Any]:
        """Scan a bounded batch and download missing Chinese subtitles."""

        if not self._enabled:
            return {"status": "disabled", "message": "插件尚未启用"}
        if self._scan_running:
            return {"status": "busy", "message": "已有媒体库扫描正在运行"}
        self._scan_running = True
        payload = {
            "paths": self._split_list(self._scan_paths),
            "exclude_paths": self._split_list(self._scan_exclude_paths),
            "video_extensions": self._split_list(self._scan_video_extensions),
            "languages": self._languages,
            "max_files": self._scan_max_files,
            "overwrite": self._overwrite,
            "dry_run": self._scan_dry_run,
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
                max(600, self._scan_max_files * 45),
            )
            result["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result["trigger"] = "manual" if manual else "schedule"
            self.save_data(
                self._latest_scan_key,
                json.dumps(result, ensure_ascii=False),
            )
            logger.info(
                "SubtitleForge 媒体库扫描完成："
                f"发现 {result.get('discovered', 0)}，"
                f"检查 {result.get('checked', 0)}，"
                f"下载 {result.get('downloaded', 0)}"
            )
            if self._notify and int(result.get("downloaded") or 0) > 0:
                self.post_message(
                    mtype=MessageType.Plugin,
                    title="SubtitleForge 媒体库扫描完成",
                    text=(
                        f"检查 {result.get('checked', 0)} 个视频，"
                        f"新增 {result.get('downloaded', 0)} 条中文字幕。"
                    ),
                )
            return result
        except Exception as exc:  # noqa: BLE001 - scheduler must survive failures
            message = f"{type(exc).__name__}: {exc}"
            logger.error(f"SubtitleForge 媒体库扫描失败：{message}")
            return {"status": "error", "errors": [message]}
        finally:
            self._scan_running = False

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
            names = "、".join(
                f"{item.get('language')}:{item.get('candidate')}"
                for item in aggregate["downloaded"]
            )
            self.post_message(
                mtype=MessageType.Plugin,
                title=f"字幕已补全 - {common_payload['title']}",
                text=names,
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
                            "text": "媒体库扫描会检查外挂和内嵌中文字幕。默认先预览，每轮只处理一批视频；确认识别无误后再关闭“仅预览”。",
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
                                            "label": "每轮最多处理",
                                            "type": "number",
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
                            "placeholder": "留空则使用上方字幕源顺序",
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
            "scan_max_files": 20,
            "scan_dry_run": True,
            "scan_provider_order": "",
        }

    def get_page(self) -> list[dict]:
        """Render the latest library scan and recent processing history."""

        history = self._load_history()
        latest_scan = self._load_latest_scan()
        cards: list[dict] = []
        if latest_scan:
            items = latest_scan.get("items") or []
            missing = [
                item
                for item in items
                if item.get("status") in {"dry_run", "not_found", "partial", "error"}
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
                                        f"下载 {latest_scan.get('downloaded', 0)} · "
                                        f"待后续 {latest_scan.get('remaining', 0)}"
                                    ),
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-caption mt-2"},
                                    "text": (
                                        "；".join(
                                            f"{item.get('title', '')}: "
                                            f"{item.get('status', '')}"
                                            for item in missing[:5]
                                        )
                                        or "本轮没有发现需要补全的条目"
                                    ),
                                },
                            ],
                        }
                    ],
                }
            )
        if not history and not latest_scan:
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
                                        f"错误: {len(result.get('errors') or [])}"
                                    ),
                                },
                            ],
                        }
                    ],
                }
            )
        return cards

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
