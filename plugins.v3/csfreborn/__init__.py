"""MoviePilot V3 adapter for SubtitleForge."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.schemas.types import EventType, MessageType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase


class CSFReborn(_PluginBase):
    """Send completed media files to SubtitleForge for subtitle completion."""

    plugin_name = "SubtitleForge 字幕补全"
    plugin_desc = (
        "监听 MoviePilot 整理完成事件，调用自建 SubtitleForge 服务下载缺失字幕。"
    )
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "0.1.0"
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
        self._history_key = "download_history"
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
        try:
            self._timeout = max(10, int(config.get("timeout") or 180))
        except (TypeError, ValueError):
            self._timeout = 180

    def get_state(self) -> bool:
        """Return whether the plugin is enabled."""

        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """This plugin has no remote commands."""

        return []

    def get_api(self) -> list[dict[str, Any]]:
        """Expose a health endpoint for the plugin page."""

        return [
            {
                "path": "/health",
                "endpoint": self.health,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "检查 SubtitleForge 服务",
            }
        ]

    async def health(self) -> dict[str, Any]:
        """Check the configured SubtitleForge service."""

        try:
            return await asyncio.to_thread(self._request_json, "GET", "/healthz", None)
        except Exception as exc:  # noqa: BLE001 - API should return a useful error
            return {"status": "error", "error": str(exc)}

    @eventmanager.register(EventType.TransferComplete)
    async def on_transfer_complete(self, event: Event) -> None:
        """Process each successfully transferred video."""

        if not self._enabled:
            return
        data = event.event_data if isinstance(event.event_data, dict) else {}
        transferinfo = data.get("transferinfo")
        target = getattr(transferinfo, "target_item", None)
        mediainfo = data.get("mediainfo")
        meta = data.get("meta")
        path = getattr(target, "path", None)
        if not path:
            logger.debug("SubtitleForge: TransferComplete has no target path")
            return
        if mediainfo is None:
            logger.debug("SubtitleForge: TransferComplete has no mediainfo")
            return
        media_type = self._media_type(getattr(mediainfo, "type", None))
        payload = {
            "video_path": str(path),
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
        if not payload["title"]:
            logger.warning("SubtitleForge: media title is empty; skip subtitle search")
            return
        try:
            result = await asyncio.to_thread(
                self._request_json, "POST", "/v1/process", payload
            )
        except Exception as exc:  # noqa: BLE001 - event handlers must not crash the host
            logger.error(f"SubtitleForge 处理失败：{type(exc).__name__}: {exc}")
            self._add_history(payload, {"status": "error", "errors": [str(exc)]})
            return
        logger.info(
            f"SubtitleForge: {payload['title']} -> {result.get('status')}, "
            f"downloaded={len(result.get('downloaded') or [])}"
        )
        self._add_history(payload, result)
        if self._notify and result.get("downloaded"):
            names = "、".join(
                f"{item.get('language')}:{item.get('candidate')}"
                for item in result["downloaded"]
            )
            self.post_message(
                mtype=MessageType.Plugin,
                title=f"字幕已补全 - {payload['title']}",
                text=names,
            )

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None
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
            with urlopen(request, timeout=self._timeout) as response:
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
        }

    def get_page(self) -> list[dict]:
        """Render recent processing history."""

        history = self._load_history()
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无记录。启用后会在 MoviePilot 整理完成时自动请求 SubtitleForge。",
                    },
                }
            ]
        cards: list[dict] = []
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
        """No persistent resources are held by this adapter."""

        return

    def _load_history(self) -> list[dict]:
        """Read recent history from plugin data."""

        value = self.get_data(self._history_key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        return value if isinstance(value, list) else []

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


__all__ = ["CSFReborn"]
