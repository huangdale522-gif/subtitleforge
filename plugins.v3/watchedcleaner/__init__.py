"""Selective watched-media cleaner for MoviePilot V3."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import Body

from app import schemas
from app.sdk.logging import logger
from app.sdk.events import Event, eventmanager
from app.sdk.plugin import _PluginBase
from app.sdk.services import MediaServerHelper
from app.schemas.types import EventType, MessageType, NotificationChannel

from .adapters import AdapterError, MediaAdapter, build_adapter
from .policy import (
    CATEGORIES,
    DEFAULT_ANIME_KEYWORDS,
    as_bool,
    bounded_float,
    bounded_int,
    days_since,
    normalize_categories,
    normalize_tv_mode,
    record_block_reason,
    split_values,
    valid_roots,
)


class WatchedCleaner(_PluginBase):
    """Preview watched media and delete only manually selected candidates."""

    plugin_name = "已观看媒体清理"
    plugin_desc = (
        "扫描 Emby/Jellyfin/Plex 的已观看电影、剧集和动漫，"
        "支持按季清理、Telegram 提醒和人工确认后安全删除。"
    )
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.2.0"
    plugin_author = "huangdale522-gif"
    author_url = "https://github.com/huangdale522-gif"
    plugin_config_prefix = "watchedcleaner_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _server_name = ""
    _server_user = ""
    _categories = list(CATEGORIES)
    _tv_mode = "season"
    _watched_threshold = 90.0
    _min_days_since_played = 7
    _min_days_since_added = 7
    _protect_favorites = True
    _anime_keywords = list(DEFAULT_ANIME_KEYWORDS)
    _allowed_roots = ""
    _allow_delete = False
    _dry_run = True
    _max_candidates = 300
    _max_delete_batch = 20
    _scan_ttl_hours = 24
    _history_limit = 500
    _notify_enabled = False
    _notify_user = ""
    _notify_scan_results = True
    _notify_delete_results = True
    _age_reminder_enabled = False
    _age_reminder_days = 365
    _age_reminder_interval_hours = 168
    _age_reminder_limit = 10
    _age_reminder_cooldown_days = 30

    _operation_lock: RLock
    _state_lock: RLock
    _last_scan: Optional[dict[str, Any]] = None
    _state_loaded = False

    def init_plugin(self, config: dict = None) -> None:
        """Load configuration without starting any automatic deletion task."""
        config = config or {}
        self._enabled = as_bool(config.get("enabled"), False)
        self._server_name = str(config.get("server") or "").strip()
        self._server_user = str(config.get("server_user") or "").strip()
        self._categories = normalize_categories(config.get("categories"))
        self._tv_mode = normalize_tv_mode(config.get("tv_mode"))
        self._watched_threshold = bounded_float(
            config.get("watched_threshold"), 90.0, 1.0, 100.0
        )
        self._min_days_since_played = bounded_int(
            config.get("min_days_since_played"), 7, 0, 3650
        )
        self._min_days_since_added = bounded_int(
            config.get("min_days_since_added"), 7, 0, 3650
        )
        self._protect_favorites = as_bool(
            config.get("protect_favorites"), True
        )
        keywords = split_values(config.get("anime_keywords"))
        self._anime_keywords = keywords or list(DEFAULT_ANIME_KEYWORDS)
        self._allowed_roots = str(config.get("allowed_roots") or "").strip()
        self._allow_delete = as_bool(config.get("allow_delete"), False)
        self._dry_run = as_bool(config.get("dry_run"), True)
        self._max_candidates = bounded_int(
            config.get("max_candidates"), 300, 1, 2000
        )
        self._max_delete_batch = bounded_int(
            config.get("max_delete_batch"), 20, 1, 200
        )
        self._scan_ttl_hours = bounded_int(
            config.get("scan_ttl_hours"), 24, 1, 168
        )
        self._history_limit = bounded_int(
            config.get("history_limit"), 500, 20, 5000
        )
        self._notify_enabled = as_bool(config.get("notify_enabled"), False)
        self._notify_user = str(config.get("notify_user") or "").strip()
        self._notify_scan_results = as_bool(
            config.get("notify_scan_results"), True
        )
        self._notify_delete_results = as_bool(
            config.get("notify_delete_results"), True
        )
        self._age_reminder_enabled = as_bool(
            config.get("age_reminder_enabled"), False
        )
        self._age_reminder_days = bounded_int(
            config.get("age_reminder_days"), 365, 1, 3650
        )
        self._age_reminder_interval_hours = bounded_int(
            config.get("age_reminder_interval_hours"), 168, 1, 720
        )
        self._age_reminder_limit = bounded_int(
            config.get("age_reminder_limit"), 10, 1, 100
        )
        self._age_reminder_cooldown_days = bounded_int(
            config.get("age_reminder_cooldown_days"), 30, 1, 3650
        )
        if not hasattr(self, "_operation_lock"):
            self._operation_lock = RLock()
        if not hasattr(self, "_state_lock"):
            self._state_lock = RLock()
        self._last_scan = None
        self._state_loaded = False

    def get_state(self) -> bool:
        """Return whether the plugin is enabled."""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """This plugin deliberately exposes no remote command."""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """Register a read-only reminder scan; it never deletes automatically."""
        if not (
            self._enabled
            and self._notify_enabled
            and self._valid_notify_user()
            and self._age_reminder_enabled
        ):
            return []
        return [
            {
                "id": "WatchedCleanerAgeReminder",
                "name": "已观看媒体年度清理提醒",
                "trigger": "interval",
                "func": self.scan_age_reminders,
                "kwargs": {"hours": self._age_reminder_interval_hours},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """Register scan, delete, status, and audit APIs."""
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "扫描已观看媒体",
                "description": "按当前配置扫描并保存可人工勾选的候选，不会删除媒体。",
            },
            {
                "path": "/delete",
                "endpoint": self.api_delete,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "预演或删除选中的已观看媒体",
                "description": "仅接受最近一次扫描中的候选键，并再次校验观看状态与路径白名单。",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看删除审计记录",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看插件与最近扫描状态",
            },
            {
                "path": "/options",
                "endpoint": self.api_options,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看支持的媒体服务器",
            },
            {
                "path": "/age-reminders/run",
                "endpoint": self.api_age_reminders,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即检查超过保留期限的已观看媒体",
                "description": (
                    "扫描已观看媒体，并通过 Telegram 逐条询问是否删除；"
                    "不会自动删除任何文件。"
                ),
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """Build the V3 Vuetify configuration form."""
        server_items = [{"title": "全部支持的服务器", "value": ""}]
        server_items.extend(self._server_options())
        fields = [
            self._field("VSwitch", "enabled", "启用插件", md=4),
            self._field(
                "VSwitch",
                "dry_run",
                "仅预演，不执行删除",
                md=4,
            ),
            self._field(
                "VSwitch",
                "allow_delete",
                "允许实际删除（危险）",
                md=4,
            ),
            self._field(
                "VSelect",
                "server",
                "媒体服务器",
                md=7,
                items=server_items,
                **{"clearable": True},
            ),
            self._field(
                "VTextField",
                "server_user",
                "观看状态用户（Emby/Jellyfin）",
                md=5,
                placeholder="留空使用服务器当前管理用户",
                **{"persistent-hint": True},
                hint="Plex 只能使用 Token 对应用户的观看状态。",
            ),
            self._field(
                "VSelect",
                "categories",
                "扫描分类",
                md=7,
                items=[
                    {"title": "电影", "value": "movie"},
                    {"title": "剧集", "value": "tv"},
                    {"title": "动漫", "value": "anime"},
                ],
                multiple=True,
                chips=True,
            ),
            self._field(
                "VSelect",
                "tv_mode",
                "剧集/动漫清理粒度",
                md=5,
                items=[
                    {"title": "按季：整季看完后删除该季", "value": "season"},
                    {"title": "按整剧：全剧看完后删除整部剧", "value": "series"},
                    {"title": "仅删除已看的单集", "value": "episode"},
                ],
            ),
            self._field(
                "VTextField",
                "watched_threshold",
                "观看进度阈值（%）",
                md=3,
                type="number",
                min=1,
                max=100,
            ),
            self._field(
                "VTextField",
                "min_days_since_played",
                "播放后保护天数",
                md=3,
                type="number",
                min=0,
                max=3650,
            ),
            self._field(
                "VTextField",
                "min_days_since_added",
                "入库后保护天数",
                md=3,
                type="number",
                min=0,
                max=3650,
            ),
            self._field(
                "VSwitch",
                "protect_favorites",
                "收藏条目永不删除",
                md=3,
            ),
            self._field(
                "VTextField",
                "anime_keywords",
                "动漫识别关键词",
                placeholder="动漫, 动画, 番剧, anime, animation",
            ),
            self._field(
                "VTextarea",
                "allowed_roots",
                "允许删除的媒体服务器路径白名单",
                placeholder="/volume1/Media\n/mnt/media",
                rows=3,
                **{"persistent-hint": True},
                hint=(
                    "填写媒体服务器看到的 NAS 路径，不是 MoviePilot 容器内的映射路径。"
                    "每行一个根目录；留空时禁止实际删除。"
                ),
            ),
            self._field(
                "VTextField",
                "max_candidates",
                "单次扫描最多候选数",
                md=4,
                type="number",
                min=1,
                max=2000,
            ),
            self._field(
                "VTextField",
                "max_delete_batch",
                "单次最多删除数",
                md=4,
                type="number",
                min=1,
                max=200,
            ),
            self._field(
                "VTextField",
                "scan_ttl_hours",
                "候选有效期（小时）",
                md=4,
                type="number",
                min=1,
                max=168,
            ),
            self._field(
                "VTextField",
                "history_limit",
                "审计记录保留条数",
                md=4,
                type="number",
                min=20,
                max=5000,
            ),
            self._field(
                "VSwitch",
                "notify_enabled",
                "启用 Telegram 推送",
                md=4,
            ),
            self._field(
                "VTextField",
                "notify_user",
                "Telegram 用户 ID",
                md=8,
                placeholder="例如 123456789",
                **{"persistent-hint": True},
                hint=(
                    "年度删除按钮只接受该用户点击。可向 Telegram 的 "
                    "@userinfobot 发送消息获取数字 ID。"
                ),
            ),
            self._field(
                "VSwitch",
                "notify_scan_results",
                "推送扫描结果",
                md=4,
            ),
            self._field(
                "VSwitch",
                "notify_delete_results",
                "推送删除结果",
                md=4,
            ),
            self._field(
                "VSwitch",
                "age_reminder_enabled",
                "启用超过保留期限的年度询问",
                md=4,
            ),
            self._field(
                "VTextField",
                "age_reminder_days",
                "入库超过多少天后询问",
                md=3,
                type="number",
                min=1,
                max=3650,
            ),
            self._field(
                "VTextField",
                "age_reminder_interval_hours",
                "自动检查间隔（小时）",
                md=3,
                type="number",
                min=1,
                max=720,
            ),
            self._field(
                "VTextField",
                "age_reminder_limit",
                "每轮最多询问条数",
                md=3,
                type="number",
                min=1,
                max=100,
            ),
            self._field(
                "VTextField",
                "age_reminder_cooldown_days",
                "未处理提醒重复间隔（天）",
                md=3,
                type="number",
                min=1,
                max=3650,
            ),
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "error",
                            "variant": "tonal",
                            "class": "mb-4",
                            "title": "删除操作不可撤销",
                            "text": (
                                "请先保持“仅预演”开启并完成扫描，核对候选后再关闭预演。"
                                "实际删除还要求开启“允许实际删除”并配置路径白名单。"
                            ),
                        },
                    },
                    {"component": "VRow", "content": fields},
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mt-3",
                            "text": (
                                "插件通过媒体服务器 API 删除，不会直接操作 NAS 文件系统。"
                                "Emby/Jellyfin API Key 和 Plex Token 必须具有删除权限。"
                            ),
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "class": "mt-3",
                            "text": (
                                "年度询问会按媒体服务器返回的入库时间筛选已观看内容。"
                                "启用前请先在 MoviePilot 配置 Telegram 通知，并填写"
                                " Telegram 数字用户 ID；插件只会询问，不会自动删除。"
                            ),
                        },
                    },
                ],
            }
        ], self._current_config()

    def get_page(self) -> List[dict]:
        """Render the latest scan with explicit per-item checkboxes."""
        scan = self._load_scan()
        candidates = list(scan.get("candidates") or [])
        content: List[dict] = []

        if self._dry_run:
            content.append(
                self._alert(
                    "info",
                    "当前为预演模式：勾选后只会重新校验并显示拟删除结果，不会删除任何媒体。",
                )
            )
        elif not self._allow_delete:
            content.append(
                self._alert(
                    "warning",
                    "预演已关闭，但“允许实际删除”未开启；删除接口仍会拒绝执行。",
                )
            )
        elif not valid_roots(split_values(self._allowed_roots)):
            content.append(
                self._alert(
                    "warning",
                    "尚未配置有效的媒体服务器路径白名单；删除接口会拒绝执行。",
                )
            )
        else:
            content.append(
                self._alert(
                    "error",
                    "实际删除模式已开启。每次最多处理配置的批量上限，且执行前会再次校验观看状态、收藏、时间和路径。",
                )
            )

        if self._age_reminder_enabled and not self._notify_enabled:
            content.append(
                self._alert(
                    "warning",
                    "年度询问已开启，但 Telegram 推送未启用，不会发送提醒。",
                )
            )
        elif self._age_reminder_enabled and not self._valid_notify_user():
            content.append(
                self._alert(
                    "warning",
                    "年度询问已开启，但未填写 Telegram 用户 ID，"
                    "为避免群聊误点删除，插件不会发送提醒。",
                )
            )

        actions: List[dict[str, Any]] = [
            {
                "component": "VBtn",
                "props": {
                    "color": "primary",
                    "variant": "tonal",
                    "prepend-icon": "mdi-magnify-scan",
                    "onclick": self._scan_onclick(),
                },
                "text": "扫描已观看媒体",
            }
        ]
        if (
            self._notify_enabled
            and self._valid_notify_user()
            and self._age_reminder_enabled
        ):
            actions.append(
                {
                    "component": "VBtn",
                    "props": {
                        "color": "warning",
                        "variant": "tonal",
                        "prepend-icon": "mdi-bell-alert",
                        "onclick": self._age_reminder_onclick(),
                    },
                    "text": "立即检查超期内容",
                }
            )
        actions.extend(
            [
                {
                    "component": "VChip",
                    "props": {"variant": "tonal"},
                    "text": f"候选 {len(candidates)}",
                },
                {
                    "component": "VChip",
                    "props": {"variant": "tonal", "color": "default"},
                    "text": f"批量上限 {self._max_delete_batch}",
                },
                {
                    "component": "VChip",
                    "props": {
                        "variant": "tonal",
                        "color": (
                            "warning"
                            if self._age_reminder_enabled
                            else "default"
                        ),
                    },
                    "text": (
                        f"年度询问 {self._age_reminder_days} 天"
                        if self._age_reminder_enabled
                        else "年度询问关闭"
                    ),
                },
            ]
        )
        content.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center ga-2 my-3"},
                "content": actions,
            }
        )

        if scan:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "class": "mb-3",
                        "text": (
                            f"扫描时间 {scan.get('created_at') or '-'}；"
                            f"扫描条目 {scan.get('scanned', 0)}；"
                            f"候选 {len(candidates)}；"
                            f"有效期至 {scan.get('expires_at') or '-'}。"
                        ),
                    },
                }
            )
            skipped = scan.get("skipped") or {}
            if skipped:
                summary = "；".join(
                    f"{reason}：{count}"
                    for reason, count in sorted(skipped.items())
                )
                content.append(self._alert("warning", f"已自动跳过：{summary}"))
            errors = scan.get("errors") or []
            if errors:
                content.append(
                    self._alert("error", "扫描错误：" + "；".join(errors))
                )

        reminder_state = self._load_age_reminder_state()
        reminder_counts = self._age_reminder_counts(reminder_state)
        if reminder_counts["pending"]:
            content.append(
                self._alert(
                    "info",
                    f"Telegram 年度询问待处理 {reminder_counts['pending']} 条；"
                    f"已保留 {reminder_counts['kept']} 条；"
                    f"已通过 Telegram 删除 {reminder_counts['deleted']} 条。",
                )
            )

        if not candidates:
            content.append(
                self._alert(
                    "info",
                    "暂无候选。请检查插件是否启用、媒体服务器是否已配置，以及观看阈值和保护天数。",
                )
            )
            return content

        content.append(self._candidate_table(candidates))
        if self._dry_run:
            content.append(
                {
                    "component": "VBtn",
                    "props": {
                        "color": "primary",
                        "variant": "tonal",
                        "prepend-icon": "mdi-eye-check",
                        "class": "mt-3",
                        "onclick": self._delete_onclick(True),
                    },
                    "text": "预演删除选中项",
                }
            )
        else:
            ready = self._allow_delete and bool(
                valid_roots(split_values(self._allowed_roots))
            )
            content.append(
                {
                    "component": "VBtn",
                    "props": {
                        "color": "error",
                        "variant": "flat",
                        "prepend-icon": "mdi-delete-forever",
                        "class": "mt-3",
                        "disabled": not ready,
                        "onclick": self._delete_onclick(False),
                    },
                    "text": "永久删除选中项",
                }
            )
        return content

    def stop_service(self) -> None:
        """Disable new API operations; network calls finish in their own request."""
        self._enabled = False

    def api_scan(self, payload: Optional[dict] = Body(default=None)) -> schemas.Response:
        """Scan watched media and persist a selectable candidate snapshot."""
        if not self._enabled:
            return self._failure("插件未启用")
        if not self._operation_lock.acquire(blocking=False):
            return self._failure("已有扫描或删除任务正在执行")
        try:
            options = self._scan_options(payload or {})
            services, discovery_error = self._discover_services()
            if discovery_error:
                return self._failure(discovery_error)
            selected_services = {
                name: service
                for name, service in services.items()
                if not options["server"] or name == options["server"]
            }
            if not selected_services:
                return self._failure("没有找到匹配且受支持的媒体服务器")

            scan_id = uuid.uuid4().hex
            created = datetime.now(timezone.utc)
            expires = created + timedelta(hours=self._scan_ttl_hours)
            candidates: list[dict[str, Any]] = []
            skipped: dict[str, int] = {}
            errors: list[str] = []
            scanned = 0
            truncated = False
            servers: list[dict[str, Any]] = []

            for name, service in sorted(selected_services.items()):
                try:
                    adapter = build_adapter(name, service)
                    result = adapter.scan(options, scan_id)
                    scanned += int(result.get("scanned") or 0)
                    candidates.extend(result.get("candidates") or [])
                    truncated = truncated or bool(result.get("truncated"))
                    errors.extend(result.get("errors") or [])
                    for reason, count in (result.get("skipped") or {}).items():
                        skipped[reason] = skipped.get(reason, 0) + int(count)
                    servers.append(
                        {
                            "name": name,
                            "type": adapter.service_type,
                            "user": result.get("user"),
                        }
                    )
                except Exception as error:
                    message = f"{name}: {error}"
                    errors.append(message)
                    logger.warning(f"[已观看媒体清理] {message}")

            if not servers:
                detail = "；".join(errors) or "所有媒体服务器均未返回扫描结果"
                return self._failure(f"扫描失败：{detail}")
            if len(candidates) > self._max_candidates:
                candidates = candidates[: self._max_candidates]
                truncated = True
            state = {
                "version": 1,
                "scan_id": scan_id,
                "created_at": created.isoformat(),
                "expires_at": expires.isoformat(),
                "options": self._public_scan_options(options),
                "servers": servers,
                "scanned": scanned,
                "candidates": candidates,
                "skipped": skipped,
                "errors": errors,
                "truncated": truncated,
            }
            self._store_scan(state)
            message = (
                f"扫描完成：{len(candidates)} 个候选，"
                f"{sum(skipped.values())} 个安全跳过"
            )
            if truncated:
                message += "；候选达到上限，结果已截断"
            if errors:
                message += f"；{len(errors)} 个错误"
            self._notify_scan_completed(state, message)
            return schemas.Response(
                success=True,
                message=message,
                data={
                    "scan_id": scan_id,
                    "created_at": state["created_at"],
                    "expires_at": state["expires_at"],
                    "scanned": scanned,
                    "candidates": candidates,
                    "skipped": skipped,
                    "errors": errors,
                    "truncated": truncated,
                },
            )
        except Exception as error:
            logger.exception(f"[已观看媒体清理] 扫描失败：{error}")
            return self._failure(f"扫描失败：{error}")
        finally:
            self._operation_lock.release()

    def api_delete(self, payload: Optional[dict] = Body(default=None)) -> schemas.Response:
        """Revalidate selected candidates and either preview or delete them."""
        if not self._enabled:
            return self._failure("插件未启用")
        if not self._operation_lock.acquire(blocking=False):
            return self._failure("已有扫描或删除任务正在执行")
        try:
            payload = payload or {}
            confirmation = str(payload.get("confirmation") or "").strip()
            if self._dry_run:
                if confirmation not in {"DRY_RUN", "DELETE"}:
                    return self._failure("预演请求缺少 confirmation=DRY_RUN")
            elif confirmation != "DELETE":
                return self._failure("实际删除必须提交 confirmation=DELETE")

            if not self._dry_run and not self._allow_delete:
                return self._failure("“允许实际删除”未开启，拒绝执行")
            roots = split_values(self._allowed_roots)
            if not self._dry_run and not valid_roots(roots):
                return self._failure("未配置有效的媒体服务器路径白名单，拒绝执行")

            keys = split_values(payload.get("candidate_keys"))
            if not keys:
                return self._failure("未选择任何候选")
            if len(keys) > self._max_delete_batch:
                return self._failure(
                    f"单次最多处理 {self._max_delete_batch} 个候选"
                )
            if any(
                len(key) != 24
                or any(char not in "0123456789abcdef" for char in key)
                for key in keys
            ):
                return self._failure("候选键格式无效，请重新扫描")

            scan = self._load_scan()
            if not scan:
                return self._failure("没有可用的扫描结果，请先扫描")
            expires_at = self._parse_datetime(scan.get("expires_at"))
            if expires_at and expires_at <= datetime.now(timezone.utc):
                return self._failure("扫描结果已过期，请重新扫描")
            candidate_map = {
                str(candidate.get("key")): candidate
                for candidate in (scan.get("candidates") or [])
                if candidate.get("key")
            }
            missing = [key for key in keys if key not in candidate_map]
            if missing:
                return self._failure("部分候选已失效，请重新扫描后再选择")

            services, discovery_error = self._discover_services()
            if discovery_error:
                return self._failure(discovery_error)
            adapter_cache: dict[str, MediaAdapter] = {}
            policy = self._delete_policy()
            results: list[dict[str, Any]] = []
            audit: list[dict[str, Any]] = []
            deleted_keys: set[str] = set()
            counts = {"deleted": 0, "dry_run": 0, "skipped": 0, "failed": 0}

            for key in keys:
                candidate = candidate_map[key]
                server_name = str(candidate.get("server") or "")
                result = self._result_base(candidate)
                try:
                    service = services.get(server_name)
                    if not service:
                        raise AdapterError("原媒体服务器已不可用")
                    adapter = adapter_cache.get(server_name)
                    if adapter is None:
                        adapter = build_adapter(server_name, service)
                        adapter_cache[server_name] = adapter
                    current = adapter.refresh(candidate)
                    current_paths = self._record_paths(current)
                    if current_paths:
                        result["path"] = current_paths[0]
                    reason = record_block_reason(current, policy)
                    if reason:
                        result.update(status="skipped", message=reason)
                        counts["skipped"] += 1
                    elif self._dry_run:
                        result.update(status="dry_run", message="预演通过，可执行删除")
                        counts["dry_run"] += 1
                    else:
                        message = adapter.delete(candidate)
                        result.update(status="deleted", message=message)
                        deleted_keys.add(key)
                        counts["deleted"] += 1
                except Exception as error:
                    result.update(status="failed", message=str(error))
                    counts["failed"] += 1
                    logger.warning(
                        f"[已观看媒体清理] 处理 {candidate.get('title')} 失败：{error}"
                    )
                results.append(result)
                audit.append(
                    {
                        **result,
                        "time": datetime.now(timezone.utc).isoformat(),
                        "action": "dry_run" if self._dry_run else "delete",
                        "scan_id": scan.get("scan_id"),
                    }
                )

            if deleted_keys:
                scan["candidates"] = [
                    candidate
                    for candidate in (scan.get("candidates") or [])
                    if candidate.get("key") not in deleted_keys
                ]
                self._store_scan(scan)
            self._append_history(audit)

            message = (
                f"处理完成：删除 {counts['deleted']}，预演通过 {counts['dry_run']}，"
                f"跳过 {counts['skipped']}，失败 {counts['failed']}"
            )
            self._notify_delete_completed(counts, results)
            return schemas.Response(
                success=counts["failed"] == 0,
                message=message,
                data={
                    "dry_run": self._dry_run,
                    "counts": counts,
                    "results": results,
                },
            )
        except Exception as error:
            logger.exception(f"[已观看媒体清理] 删除流程失败：{error}")
            return self._failure(f"删除流程失败：{error}")
        finally:
            self._operation_lock.release()

    def api_history(self, limit: int = 100) -> schemas.Response:
        """Return recent audit entries, newest first."""
        try:
            history = self._load_history()
            safe_limit = bounded_int(limit, 100, 1, self._history_limit)
            return schemas.Response(
                success=True,
                data={"items": history[:safe_limit], "total": len(history)},
            )
        except Exception as error:
            return self._failure(f"读取审计记录失败：{error}")

    def api_status(self) -> schemas.Response:
        """Return the current safety mode and latest scan summary."""
        scan = self._load_scan()
        reminder_state = self._load_age_reminder_state()
        reminder_counts = self._age_reminder_counts(reminder_state)
        return schemas.Response(
            success=True,
            data={
                "enabled": self._enabled,
                "dry_run": self._dry_run,
                "allow_delete": self._allow_delete,
                "tv_mode": self._tv_mode,
                "has_allowed_roots": bool(valid_roots(split_values(self._allowed_roots))),
                "telegram": {
                    "enabled": self._notify_enabled,
                    "has_user": self._valid_notify_user(),
                },
                "age_reminder": {
                    "enabled": self._age_reminder_enabled,
                    "days": self._age_reminder_days,
                    "interval_hours": self._age_reminder_interval_hours,
                    "last_run_at": reminder_state.get("last_run_at"),
                    "last_run": reminder_state.get("last_run") or {},
                    "counts": reminder_counts,
                },
                "scan": {
                    "scan_id": scan.get("scan_id"),
                    "created_at": scan.get("created_at"),
                    "expires_at": scan.get("expires_at"),
                    "candidates": len(scan.get("candidates") or []),
                }
                if scan
                else None,
            },
        )

    def api_options(self) -> schemas.Response:
        """Return supported, currently configured media servers."""
        services, error = self._discover_services()
        if error:
            return self._failure(error)
        return schemas.Response(
            success=True,
            data={
                "servers": [
                    {"name": name, "type": service_type}
                    for name, service_type in sorted(
                        (name, self._service_type(service))
                        for name, service in services.items()
                    )
                ]
            },
        )

    def api_age_reminders(
        self, payload: Optional[dict] = Body(default=None)
    ) -> schemas.Response:
        """Run a read-only age scan and send Telegram confirmation requests."""
        del payload
        try:
            result = self._run_age_reminders()
        except Exception as error:
            logger.exception(f"[已观看媒体清理] 年度询问失败：{error}")
            return self._failure(f"年度询问失败：{error}")
        if not result.get("success"):
            return self._failure(str(result.get("message") or "年度询问失败"))
        return schemas.Response(
            success=True,
            message=(
                f"年度询问完成：发送 {result.get('sent', 0)} 条，"
                f"剩余待处理 {result.get('remaining', 0)} 条"
            ),
            data=result,
        )

    def _current_config(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "server": self._server_name,
            "server_user": self._server_user,
            "categories": self._categories,
            "tv_mode": self._tv_mode,
            "watched_threshold": self._watched_threshold,
            "min_days_since_played": self._min_days_since_played,
            "min_days_since_added": self._min_days_since_added,
            "protect_favorites": self._protect_favorites,
            "anime_keywords": ", ".join(self._anime_keywords),
            "allowed_roots": self._allowed_roots,
            "allow_delete": self._allow_delete,
            "dry_run": self._dry_run,
            "max_candidates": self._max_candidates,
            "max_delete_batch": self._max_delete_batch,
            "scan_ttl_hours": self._scan_ttl_hours,
            "history_limit": self._history_limit,
            "notify_enabled": self._notify_enabled,
            "notify_user": self._notify_user,
            "notify_scan_results": self._notify_scan_results,
            "notify_delete_results": self._notify_delete_results,
            "age_reminder_enabled": self._age_reminder_enabled,
            "age_reminder_days": self._age_reminder_days,
            "age_reminder_interval_hours": self._age_reminder_interval_hours,
            "age_reminder_limit": self._age_reminder_limit,
            "age_reminder_cooldown_days": self._age_reminder_cooldown_days,
        }

    def _scan_options(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        def value(name: str, default: Any) -> Any:
            return payload[name] if name in payload else default

        return {
            "server": str(value("server", self._server_name) or "").strip(),
            "server_user": str(
                value("server_user", self._server_user) or ""
            ).strip(),
            "categories": normalize_categories(
                value("categories", self._categories)
            ),
            "tv_mode": normalize_tv_mode(value("tv_mode", self._tv_mode)),
            "watched_threshold": bounded_float(
                value("watched_threshold", self._watched_threshold),
                90.0,
                1.0,
                100.0,
            ),
            "min_days_since_played": bounded_int(
                value("min_days_since_played", self._min_days_since_played),
                7,
                0,
                3650,
            ),
            "min_days_since_added": bounded_int(
                value("min_days_since_added", self._min_days_since_added),
                7,
                0,
                3650,
            ),
            "protect_favorites": as_bool(
                value("protect_favorites", self._protect_favorites), True
            ),
            "anime_keywords": split_values(
                value("anime_keywords", self._anime_keywords)
            )
            or list(DEFAULT_ANIME_KEYWORDS),
            "allowed_roots": split_values(self._allowed_roots),
            # Scanning must remain useful before the user has finalized the
            # NAS path whitelist. Actual deletion always rechecks it.
            "require_allowed_roots": False,
            "max_candidates": bounded_int(
                value("max_candidates", self._max_candidates),
                300,
                1,
                2000,
            ),
        }

    def _delete_policy(self) -> dict[str, Any]:
        return {
            "watched_threshold": self._watched_threshold,
            "min_days_since_played": self._min_days_since_played,
            "min_days_since_added": self._min_days_since_added,
            "protect_favorites": self._protect_favorites,
            "allowed_roots": split_values(self._allowed_roots),
            "require_allowed_roots": True,
        }

    @staticmethod
    def _public_scan_options(options: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in options.items()
            if key not in {"allowed_roots"}
        }

    def _discover_services(self) -> tuple[dict[str, Any], str]:
        try:
            raw = MediaServerHelper().get_services()
        except Exception as error:
            return {}, f"读取媒体服务器失败：{error}"
        services = {
            str(name): service
            for name, service in raw.items()
            if name
            and self._service_type(service) in {"emby", "jellyfin", "plex"}
        }
        if not services:
            return {}, "没有启用 Emby、Jellyfin 或 Plex 媒体服务器"
        return services, ""

    def _server_options(self) -> list[dict[str, str]]:
        services, _ = self._discover_services()
        return [
            {
                "title": f"{name} ({self._service_type(service)})",
                "value": name,
            }
            for name, service in sorted(services.items())
        ]

    @staticmethod
    def _service_type(service: Any) -> str:
        value = getattr(service, "type", "")
        return str(getattr(value, "value", value) or "").strip().lower()

    def _load_scan(self) -> dict[str, Any]:
        with self._state_lock:
            if not self._state_loaded:
                raw = self._safe_get_data("last_scan")
                self._last_scan = raw if isinstance(raw, dict) else {}
                self._state_loaded = True
            return dict(self._last_scan or {})

    def _store_scan(self, scan: Mapping[str, Any]) -> None:
        with self._state_lock:
            self._last_scan = dict(scan)
            self._state_loaded = True
            self.save_data("last_scan", self._last_scan)

    def _load_history(self) -> list[dict[str, Any]]:
        raw = self._safe_get_data("history")
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

    def _append_history(self, records: List[dict[str, Any]]) -> None:
        if not records:
            return
        history = records + self._load_history()
        self.save_data("history", history[: self._history_limit])
        logger.info(
            f"[已观看媒体清理] 写入 {len(records)} 条审计记录，"
            f"当前保留 {min(len(history), self._history_limit)} 条"
        )

    def scan_age_reminders(self) -> dict[str, Any]:
        """Scheduled entry point: scan and ask, but never delete automatically."""
        try:
            return self._run_age_reminders()
        except Exception as error:
            logger.exception(f"[已观看媒体清理] 定时年度询问失败：{error}")
            return {"success": False, "message": str(error)}

    def _run_age_reminders(self) -> dict[str, Any]:
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if not self._notify_enabled:
            return {"success": False, "message": "Telegram 推送未启用"}
        if not self._valid_notify_user():
            return {
                "success": False,
                "message": "请填写有效的 Telegram 数字用户 ID，以避免他人误点删除",
            }
        if not self._age_reminder_enabled:
            return {"success": False, "message": "年度询问未启用"}

        scan_response = self.api_scan({})
        if not getattr(scan_response, "success", False):
            return {
                "success": False,
                "message": str(
                    getattr(scan_response, "message", "") or "扫描失败"
                ),
            }

        scan = self._load_scan()
        state = self._load_age_reminder_state()
        now = datetime.now(timezone.utc)
        eligible = self._eligible_age_reminder_candidates(
            list(scan.get("candidates") or []),
            state,
            now=now,
        )
        selected = eligible[: self._age_reminder_limit]
        sent = 0
        failed = 0
        for candidate in selected:
            key = str(candidate.get("key") or "")
            if not self._send_age_reminder(candidate):
                failed += 1
                continue
            items = state.setdefault("items", {})
            previous = items.get(key) if isinstance(items, dict) else {}
            previous = previous if isinstance(previous, dict) else {}
            items[key] = {
                **previous,
                "status": "pending",
                "recipient": self._notify_user,
                "first_notified_at": (
                    previous.get("first_notified_at") or now.isoformat()
                ),
                "last_notified_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "scan_id": scan.get("scan_id"),
                "candidate": dict(candidate),
            }
            self._store_age_reminder_state(state)
            sent += 1

        remaining = max(len(eligible) - sent, 0)
        summary = {
            "success": True,
            "scanned": int(scan.get("scanned") or 0),
            "age_candidates": len(eligible),
            "sent": sent,
            "failed": failed,
            "remaining": remaining,
            "scan_id": scan.get("scan_id"),
            "message": (
                f"超过 {self._age_reminder_days} 天的已观看候选 "
                f"{len(eligible)} 条，已发送 {sent} 条"
            ),
        }
        state["last_run_at"] = now.isoformat()
        state["last_run"] = summary
        self._store_age_reminder_state(state)
        logger.info(f"[已观看媒体清理] 年度询问：{summary}")
        return summary

    def _eligible_age_reminder_candidates(
        self,
        candidates: List[dict[str, Any]],
        state: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> List[dict[str, Any]]:
        """Filter candidates by age and persisted per-item cooldown."""
        current = now or datetime.now(timezone.utc)
        raw_items = state.get("items")
        items = raw_items if isinstance(raw_items, Mapping) else {}
        eligible: List[dict[str, Any]] = []
        for candidate in candidates:
            key = str(candidate.get("key") or "")
            if not self._valid_candidate_key(key):
                continue
            age_days = days_since(candidate.get("added_at"), current)
            if age_days is None or age_days < self._age_reminder_days:
                continue
            raw_item = items.get(key)
            item = raw_item if isinstance(raw_item, Mapping) else {}
            status = str(item.get("status") or "")
            if status in {"kept", "deleted"}:
                continue
            elapsed = days_since(item.get("last_notified_at"), current)
            if (
                elapsed is not None
                and elapsed < self._age_reminder_cooldown_days
            ):
                continue
            enriched = dict(candidate)
            enriched["age_days"] = round(age_days, 1)
            enriched["reminder_status"] = status or "new"
            eligible.append(enriched)
        eligible.sort(
            key=lambda item: float(item.get("age_days") or 0),
            reverse=True,
        )
        return eligible

    def _send_age_reminder(self, candidate: Mapping[str, Any]) -> bool:
        key = str(candidate.get("key") or "")
        if not self._valid_candidate_key(key):
            return False
        category = {
            "movie": "电影",
            "tv": "剧集",
            "anime": "动漫",
        }.get(str(candidate.get("category") or ""), "媒体")
        kind = str(candidate.get("kind") or "")
        if kind == "season":
            action_label = "删除这一季"
        elif kind == "series":
            action_label = "删除整部剧"
        elif kind == "episode":
            action_label = "删除这一集"
        else:
            action_label = "删除这部电影"
        display = self._candidate_display(candidate)
        added_at = str(candidate.get("added_at") or "-")
        age_days = int(float(candidate.get("age_days") or 0))
        path = str(candidate.get("path") or "-")
        if len(path) > 300:
            path = path[:297] + "..."
        text = "\n".join(
            [
                f"分类：{category}",
                f"内容：{display}",
                f"媒体服务器：{candidate.get('server') or '-'}",
                f"入库时间：{added_at}（已 {age_days} 天）",
                f"观看进度：{float(candidate.get('percentage') or 0):.1f}%",
                f"路径：{path}",
                "",
                "请选择是否删除。插件不会自动执行删除。",
            ]
        )
        buttons = [
            [
                {
                    "text": action_label,
                    "callback_data": self._age_reminder_callback_data(
                        "d", key
                    ),
                },
                {
                    "text": "保留，不再询问",
                    "callback_data": self._age_reminder_callback_data(
                        "k", key
                    ),
                },
            ]
        ]
        return self._send_plugin_message(
            title=f"入库超过 {self._age_reminder_days} 天，是否删除？",
            text=text,
            buttons=buttons,
            userid=self._notify_user,
        )

    def _age_reminder_callback_data(self, action: str, key: str) -> str:
        return f"[PLUGIN]{self.__class__.__name__}|{action}:{key}"

    @staticmethod
    def _candidate_display(candidate: Mapping[str, Any]) -> str:
        kind = str(candidate.get("kind") or "")
        title = str(candidate.get("title") or "-")
        if kind == "season":
            series_title = str(candidate.get("series_title") or title)
            season = candidate.get("season")
            season_label = (
                f"第{season}季"
                if season is not None
                else str(candidate.get("season_title") or "未知季")
            )
            return f"{series_title} {season_label}"
        if kind == "episode":
            series_title = str(candidate.get("series_title") or title)
            season = candidate.get("season")
            episode = candidate.get("episode")
            episode_label = ""
            if season is not None and episode is not None:
                try:
                    episode_label = (
                        f" S{int(season):02d}E{int(episode):02d}"
                    )
                except (TypeError, ValueError):
                    episode_label = f" S{season}E{episode}"
            return f"{series_title}{episode_label} - {title}"
        return title

    def _send_plugin_message(
        self,
        *,
        title: str,
        text: str,
        buttons: Optional[List[List[dict[str, Any]]]] = None,
        userid: Optional[str] = None,
        source: Optional[str] = None,
    ) -> bool:
        if not self._notify_enabled:
            return False
        try:
            self.post_message(
                channel=NotificationChannel.Telegram,
                mtype=MessageType.Plugin,
                title=title,
                text=text,
                userid=self._notify_user or userid,
                source=source,
                buttons=buttons,
            )
            return True
        except Exception as error:
            logger.warning(f"[已观看媒体清理] Telegram 推送失败：{error}")
            return False

    def _notify_scan_completed(
        self, state: Mapping[str, Any], summary: str
    ) -> None:
        if not self._notify_scan_results:
            return
        errors = list(state.get("errors") or [])
        text = "\n".join(
            [
                summary,
                f"扫描条目：{state.get('scanned', 0)}",
                f"候选数量：{len(state.get('candidates') or [])}",
                f"扫描时间：{state.get('created_at') or '-'}",
                *(
                    [f"错误：{'；'.join(str(item) for item in errors[:3])}"]
                    if errors
                    else []
                ),
            ]
        )
        self._send_plugin_message(title="已观看媒体扫描结果", text=text)

    def _notify_delete_completed(
        self,
        counts: Mapping[str, Any],
        results: List[dict[str, Any]],
    ) -> None:
        if not self._notify_delete_results:
            return
        details = []
        for result in results[:10]:
            title = str(result.get("title") or "-")
            status = str(result.get("status") or "-")
            details.append(f"{title}：{status} {result.get('message') or ''}".strip())
        if len(results) > 10:
            details.append(f"另有 {len(results) - 10} 条未展开")
        title = "删除预演完成" if self._dry_run else "媒体删除完成"
        text = "\n".join(
            [
                (
                    f"删除 {counts.get('deleted', 0)}，"
                    f"预演通过 {counts.get('dry_run', 0)}，"
                    f"跳过 {counts.get('skipped', 0)}，"
                    f"失败 {counts.get('failed', 0)}"
                ),
                *details,
            ]
        )
        self._send_plugin_message(title=title, text=text)

    def _load_age_reminder_state(self) -> dict[str, Any]:
        with self._state_lock:
            raw = self._safe_get_data("age_reminders")
        state = dict(raw) if isinstance(raw, Mapping) else {}
        items = state.get("items")
        state["version"] = 1
        state["items"] = dict(items) if isinstance(items, Mapping) else {}
        return state

    def _store_age_reminder_state(self, state: Mapping[str, Any]) -> None:
        with self._state_lock:
            normalized = dict(state)
            normalized["version"] = 1
            items = normalized.get("items")
            normalized["items"] = (
                dict(items) if isinstance(items, Mapping) else {}
            )
            if len(normalized["items"]) > 2000:
                def updated_at(entry: Any) -> str:
                    value = entry[1]
                    return (
                        str(value.get("updated_at") or "")
                        if isinstance(value, Mapping)
                        else ""
                    )

                ordered = sorted(
                    normalized["items"].items(),
                    key=updated_at,
                    reverse=True,
                )
                normalized["items"] = dict(ordered[:2000])
            self.save_data("age_reminders", normalized)

    def _update_age_reminder_item(
        self, key: str, **updates: Any
    ) -> dict[str, Any]:
        state = self._load_age_reminder_state()
        item = state["items"].get(key)
        item = dict(item) if isinstance(item, Mapping) else {}
        item.update(updates)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["items"][key] = item
        self._store_age_reminder_state(state)
        return item

    @staticmethod
    def _age_reminder_counts(state: Mapping[str, Any]) -> dict[str, int]:
        items = state.get("items")
        items = items if isinstance(items, Mapping) else {}
        counts = {
            "pending": 0,
            "kept": 0,
            "deleted": 0,
            "blocked": 0,
            "failed": 0,
        }
        for item in items.values():
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "")
            if status in counts:
                counts[status] += 1
        return counts

    @eventmanager.register(EventType.MessageAction)
    def on_message_action(self, event: Event) -> None:
        """Handle Telegram delete/keep callbacks for age reminders."""
        data = event.event_data if isinstance(event.event_data, Mapping) else {}
        if data.get("plugin_id") != self.__class__.__name__:
            return
        callback_text = str(data.get("text") or "")
        action, separator, key = callback_text.partition(":")
        if not separator or action not in {"d", "k"}:
            return
        if not self._valid_candidate_key(key):
            self._reply_to_event(data, "提醒无效", "候选标识格式不正确。")
            return

        state = self._load_age_reminder_state()
        item = state["items"].get(key)
        if not isinstance(item, Mapping):
            self._reply_to_event(
                data,
                "提醒已失效",
                "没有找到这条提醒，请等待下一次扫描或重新扫描。",
            )
            return

        userid = str(data.get("userid") or "")
        recipient = str(item.get("recipient") or "")
        if not userid or userid != recipient or userid != self._notify_user:
            self._reply_to_event(
                data,
                "无权处理",
                "只有收到该提醒的 Telegram 用户才能操作。",
            )
            return

        status = str(item.get("status") or "")
        if status == "kept":
            self._reply_to_event(data, "已经保留", "这条内容不会再次询问。")
            return
        if status == "deleted":
            self._reply_to_event(data, "已经删除", "这条媒体已完成删除。")
            return
        if action == "k":
            self._handle_age_reminder_keep(data, key, item)
        else:
            self._handle_age_reminder_delete(data, key, item)

    def _handle_age_reminder_keep(
        self,
        event_data: Mapping[str, Any],
        key: str,
        item: Mapping[str, Any],
    ) -> None:
        candidate = item.get("candidate")
        candidate = candidate if isinstance(candidate, Mapping) else {}
        try:
            self._update_age_reminder_item(
                key,
                status="kept",
                kept_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as error:
            logger.warning(
                f"[已观看媒体清理] 保存保留状态失败：{error}"
            )
            self._reply_to_event(
                event_data,
                "保存失败",
                "未能保存保留状态，请稍后重试。",
            )
            return
        self._reply_to_event(
            event_data,
            "已保留",
            f"{self._candidate_display(candidate)} 已保留，后续不再询问。",
        )

    def _handle_age_reminder_delete(
        self,
        event_data: Mapping[str, Any],
        key: str,
        item: Mapping[str, Any],
    ) -> None:
        if not self._enabled:
            self._reply_to_event(event_data, "插件未启用", "未执行删除。")
            return
        if not self._notify_enabled or not self._valid_notify_user():
            self._reply_to_event(
                event_data,
                "Telegram 推送已关闭",
                "未执行删除。请先重新启用 Telegram 推送和用户校验。",
            )
            return
        if self._dry_run:
            self._reply_to_event(
                event_data,
                "当前仍为预演模式",
                "未删除任何文件。请先在插件设置中关闭预演模式。",
            )
            return
        if not self._allow_delete:
            self._reply_to_event(
                event_data,
                "实际删除未开启",
                "未删除任何文件。请先开启“允许实际删除”。",
            )
            return
        roots = split_values(self._allowed_roots)
        if not valid_roots(roots):
            self._reply_to_event(
                event_data,
                "路径白名单无效",
                "未删除任何文件。请先配置媒体服务器路径白名单。",
            )
            return
        if not self._operation_lock.acquire(blocking=False):
            self._reply_to_event(
                event_data,
                "任务正在执行",
                "请等待当前扫描或删除完成后再试。",
            )
            return

        candidate = item.get("candidate")
        candidate = candidate if isinstance(candidate, Mapping) else {}
        result = self._result_base(candidate)
        try:
            latest_state = self._load_age_reminder_state()
            latest_item = latest_state["items"].get(key)
            latest_item = (
                latest_item if isinstance(latest_item, Mapping) else {}
            )
            if latest_item.get("status") not in {
                "pending",
                "blocked",
                "failed",
            }:
                self._reply_to_event(
                    event_data,
                    "提醒状态已变化",
                    "请重新扫描后再操作。",
                )
                return

            services, discovery_error = self._discover_services()
            if discovery_error:
                raise AdapterError(discovery_error)
            server_name = str(candidate.get("server") or "")
            service = services.get(server_name)
            if not service:
                raise AdapterError("原媒体服务器已不可用")
            adapter = build_adapter(server_name, service)
            current = adapter.refresh(candidate)
            current_paths = self._record_paths(current)
            if current_paths:
                result["path"] = current_paths[0]
            reason = record_block_reason(current, self._delete_policy())
            if reason:
                result.update(status="skipped", message=reason)
                self._safe_update_age_reminder_item(
                    key,
                    status="blocked",
                    last_checked_at=datetime.now(timezone.utc).isoformat(),
                    message=reason,
                )
                self._append_telegram_audit(result, item.get("scan_id"))
                self._reply_to_event(
                    event_data,
                    "安全校验未通过",
                    f"{self._candidate_display(candidate)}：{reason}",
                )
                return

            message = adapter.delete(candidate)
            result.update(status="deleted", message=message)
            self._safe_update_age_reminder_item(
                key,
                status="deleted",
                deleted_at=datetime.now(timezone.utc).isoformat(),
                message=message,
            )
            try:
                self._remove_scan_candidate(key)
            except Exception as cleanup_error:
                logger.warning(
                    f"[已观看媒体清理] 删除后更新候选快照失败：{cleanup_error}"
                )
            self._append_telegram_audit(result, item.get("scan_id"))
            self._reply_to_event(
                event_data,
                "删除完成",
                f"{self._candidate_display(candidate)}：{message}",
            )
        except Exception as error:
            result.update(status="failed", message=str(error))
            self._safe_update_age_reminder_item(
                key,
                status="failed",
                last_checked_at=datetime.now(timezone.utc).isoformat(),
                message=str(error),
            )
            self._append_telegram_audit(result, item.get("scan_id"))
            logger.warning(
                f"[已观看媒体清理] Telegram 删除失败：{error}"
            )
            self._reply_to_event(
                event_data,
                "删除失败",
                f"{self._candidate_display(candidate)}：{error}",
            )
        finally:
            self._operation_lock.release()

    def _safe_update_age_reminder_item(
        self, key: str, **updates: Any
    ) -> None:
        try:
            self._update_age_reminder_item(key, **updates)
        except Exception as error:
            logger.warning(
                f"[已观看媒体清理] 更新年度提醒状态失败：{error}"
            )

    def _append_telegram_audit(
        self, result: Mapping[str, Any], scan_id: Any
    ) -> None:
        try:
            self._append_history(
                [
                    {
                        **result,
                        "time": datetime.now(timezone.utc).isoformat(),
                        "action": "telegram_delete",
                        "scan_id": scan_id,
                    }
                ]
            )
        except Exception as error:
            logger.warning(
                f"[已观看媒体清理] 写入 Telegram 审计记录失败：{error}"
            )

    def _reply_to_event(
        self,
        event_data: Mapping[str, Any],
        title: str,
        text: str,
    ) -> None:
        try:
            self.post_message(
                channel=event_data.get("channel")
                or NotificationChannel.Telegram,
                mtype=MessageType.Plugin,
                title=title,
                text=text,
                userid=event_data.get("userid"),
                source=event_data.get("source"),
            )
        except Exception as error:
            logger.warning(
                f"[已观看媒体清理] Telegram 回调回复失败：{error}"
            )

    def _remove_scan_candidate(self, key: str) -> None:
        scan = self._load_scan()
        candidates = list(scan.get("candidates") or [])
        remaining = [
            candidate
            for candidate in candidates
            if str(candidate.get("key") or "") != key
        ]
        if len(remaining) == len(candidates):
            return
        scan["candidates"] = remaining
        self._store_scan(scan)

    @staticmethod
    def _valid_candidate_key(key: str) -> bool:
        return (
            len(key) == 24
            and all(char in "0123456789abcdef" for char in key)
        )

    def _valid_notify_user(self) -> bool:
        return bool(self._notify_user and self._notify_user.isdigit())

    def _safe_get_data(self, key: str) -> Any:
        try:
            value = self.get_data(key)
        except Exception as error:
            logger.warning(f"[已观看媒体清理] 读取数据 {key} 失败：{error}")
            return None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return None
        return value

    def _candidate_table(self, candidates: List[dict[str, Any]]) -> dict[str, Any]:
        headers = [
            "",
            "分类",
            "粒度",
            "标题",
            "服务器 / 用户",
            "季集",
            "进度",
            "最近播放",
            "路径",
        ]
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            kind = str(candidate.get("kind") or "")
            if kind == "movie":
                kind_label = "电影"
                episode_label = "-"
            elif kind == "season":
                kind_label = "整季"
                season = candidate.get("season")
                season_label = (
                    f"第{season}季"
                    if season is not None
                    else str(candidate.get("season_title") or "未知季")
                )
                episode_label = (
                    f"{season_label} · "
                    f"{candidate.get('episode_count', 0)} 集"
                )
            elif kind == "series":
                kind_label = "整剧"
                episode_label = f"{candidate.get('episode_count', 0)} 集"
            else:
                kind_label = "单集"
                season = candidate.get("season")
                episode = candidate.get("episode")
                episode_label = (
                    f"S{int(season):02d}E{int(episode):02d}"
                    if season is not None and episode is not None
                    else "-"
                )
            category = {
                "movie": "电影",
                "tv": "剧集",
                "anime": "动漫",
            }.get(str(candidate.get("category")), "-")
            row = {
                "component": "tr",
                "content": [
                    {
                        "component": "td",
                        "content": [
                            {
                                "component": "input",
                                "props": {
                                    "type": "checkbox",
                                    "class": "wc-candidate",
                                    "value": str(candidate.get("key") or ""),
                                    "aria-label": f"选择 {candidate.get('title')}",
                                },
                            }
                        ],
                    },
                    {"component": "td", "text": category},
                    {"component": "td", "text": kind_label},
                    {"component": "td", "text": str(candidate.get("title") or "-")},
                    {
                        "component": "td",
                        "text": (
                            f"{candidate.get('server') or '-'} / "
                            f"{candidate.get('user') or '-'}"
                        ),
                    },
                    {"component": "td", "text": episode_label},
                    {
                        "component": "td",
                        "text": f"{float(candidate.get('percentage') or 0):.1f}%",
                    },
                    {
                        "component": "td",
                        "text": str(candidate.get("last_played") or "-"),
                    },
                    {
                        "component": "td",
                        "text": str(candidate.get("path") or "-"),
                        "props": {"class": "text-caption"},
                    },
                ],
            }
            rows.append(row)
        select_all = {
            "component": "input",
            "props": {
                "type": "checkbox",
                "aria-label": "全选候选",
                "onclick": (
                    "var checked=this.checked;"
                    "Array.prototype.forEach.call("
                    "document.querySelectorAll('input.wc-candidate'),"
                    "function(node){node.checked=checked;}"
                    ");"
                ),
            },
        }
        return {
            "component": "VTable",
            "props": {"density": "compact", "hover": True},
            "content": [
                {
                    "component": "thead",
                    "content": [
                        {
                            "component": "tr",
                            "content": [
                                {
                                    "component": "th",
                                    "content": [select_all],
                                },
                                *[
                                    {"component": "th", "text": title}
                                    for title in headers[1:]
                                ],
                            ],
                        }
                    ],
                },
                {"component": "tbody", "content": rows},
            ],
        }

    def _scan_onclick(self) -> str:
        api = json.dumps(f"plugin/{self.__class__.__name__}/scan")
        return (
            "(function(){"
            f"window.MoviePilotAPI.post({api},{{}})"
            ".then(function(r){"
            "if(!r||r.success===false){alert((r&&r.message)||'扫描失败');return;}"
            "alert(r.message||'扫描完成');location.reload();"
            "}).catch(function(err){console.error(err);alert('扫描失败');});"
            "})()"
        )

    def _age_reminder_onclick(self) -> str:
        api = json.dumps(
            f"plugin/{self.__class__.__name__}/age-reminders/run"
        )
        return (
            "(function(){"
            "if(!confirm('将扫描已观看媒体，并通过 Telegram 逐条询问是否删除；"
            "不会自动删除。继续？')){return;}"
            f"window.MoviePilotAPI.post({api},{{}})"
            ".then(function(r){"
            "if(!r||r.success===false){alert((r&&r.message)||'年度询问失败');return;}"
            "alert(r.message||'年度询问完成');location.reload();"
            "}).catch(function(err){console.error(err);alert('年度询问失败');});"
            "})()"
        )

    def _delete_onclick(self, dry_run: bool) -> str:
        api = json.dumps(f"plugin/{self.__class__.__name__}/delete")
        confirmation = "DRY_RUN" if dry_run else "DELETE"
        if dry_run:
            confirm_text = "将重新校验选中项并执行删除预演，不会删除文件。继续？"
            phrase_check = ""
        else:
            confirm_text = "将永久删除选中媒体的文件，此操作不可撤销。继续？"
            phrase_check = (
                "var phrase=prompt('请输入 DELETE 确认实际删除');"
                "if(phrase!=='DELETE'){alert('确认文字不匹配，已取消');return;}"
            )
        return (
            "(function(){"
            "var nodes=document.querySelectorAll('input.wc-candidate:checked');"
            "var keys=Array.prototype.map.call(nodes,function(node){return node.value;});"
            "if(!keys.length){alert('请先勾选候选');return;}"
            f"if(!confirm({json.dumps(confirm_text, ensure_ascii=False)})){{return;}}"
            f"{phrase_check}"
            f"window.MoviePilotAPI.post({api},"
            f"{{candidate_keys:keys,confirmation:{json.dumps(confirmation)}}})"
            ".then(function(r){"
            "if(!r){alert('请求失败');return;}"
            "alert(r.message||(r.success?'处理完成':'处理失败'));"
            "if(r.data){location.reload();}"
            "}).catch(function(err){console.error(err);alert('请求失败');});"
            "})()"
        )

    @staticmethod
    def _alert(alert_type: str, text: str) -> dict[str, Any]:
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
    def _field(
        component: str,
        model: str,
        label: str,
        md: Optional[int] = None,
        **props: Any,
    ) -> dict[str, Any]:
        column: dict[str, Any] = {"cols": 12}
        if md:
            column["md"] = md
        return {
            "component": "VCol",
            "props": column,
            "content": [
                {
                    "component": component,
                    "props": {"model": model, "label": label, **props},
                }
            ],
        }

    @staticmethod
    def _failure(message: str) -> schemas.Response:
        return schemas.Response(success=False, message=message)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _result_base(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "key": candidate.get("key"),
            "server": candidate.get("server"),
            "server_type": candidate.get("server_type"),
            "user": candidate.get("user"),
            "category": candidate.get("category"),
            "kind": candidate.get("kind"),
            "title": candidate.get("title"),
            "season_id": candidate.get("season_id"),
            "season": candidate.get("season"),
            "episode": candidate.get("episode"),
            "path": candidate.get("path"),
            "status": "",
            "message": "",
        }

    @staticmethod
    def _record_paths(record: Mapping[str, Any]) -> list[str]:
        if record.get("kind") in {"season", "series"}:
            return [
                path
                for episode in (record.get("episodes") or [])
                for path in (episode.get("paths") or [])
            ]
        return list(record.get("paths") or [])
