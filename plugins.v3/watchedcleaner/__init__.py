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
from app.sdk.plugin import _PluginBase
from app.sdk.services import MediaServerHelper

from .adapters import AdapterError, MediaAdapter, build_adapter
from .policy import (
    CATEGORIES,
    DEFAULT_ANIME_KEYWORDS,
    as_bool,
    bounded_float,
    bounded_int,
    normalize_categories,
    normalize_tv_mode,
    record_block_reason,
    split_values,
    valid_roots,
)


class WatchedCleaner(_PluginBase):
    """Preview watched media and delete only manually selected candidates."""

    plugin_name = "已观看媒体清理"
    plugin_desc = "扫描 Emby/Jellyfin/Plex 的已观看电影、剧集和动漫，人工勾选后安全删除。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.0"
    plugin_author = "huangdale522-gif"
    author_url = "https://github.com/huangdale522-gif"
    plugin_config_prefix = "watchedcleaner_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _server_name = ""
    _server_user = ""
    _categories = list(CATEGORIES)
    _tv_mode = "episode"
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
                "电视剧删除粒度",
                md=5,
                items=[
                    {"title": "仅删除已看的单集", "value": "episode"},
                    {"title": "整剧都看完后删除整部剧", "value": "series"},
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

        content.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap align-center ga-2 my-3"},
                "content": [
                    {
                        "component": "VBtn",
                        "props": {
                            "color": "primary",
                            "variant": "tonal",
                            "prepend-icon": "mdi-magnify-scan",
                            "onclick": self._scan_onclick(),
                        },
                        "text": "扫描已观看媒体",
                    },
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
                ],
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
        return schemas.Response(
            success=True,
            data={
                "enabled": self._enabled,
                "dry_run": self._dry_run,
                "allow_delete": self._allow_delete,
                "has_allowed_roots": bool(valid_roots(split_values(self._allowed_roots))),
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
            "season": candidate.get("season"),
            "episode": candidate.get("episode"),
            "path": candidate.get("path"),
            "status": "",
            "message": "",
        }

    @staticmethod
    def _record_paths(record: Mapping[str, Any]) -> list[str]:
        if record.get("kind") == "series":
            return [
                path
                for episode in (record.get("episodes") or [])
                for path in (episode.get("paths") or [])
            ]
        return list(record.get("paths") or [])
