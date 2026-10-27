"""Protocol adapters for Emby, Jellyfin, and Plex watched-media scans."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.sdk.network import RequestUtils

from .policy import (
    as_bool,
    candidate_key,
    classify_category,
    iso_timestamp,
    normalize_record,
    newest_timestamp,
    percentage,
    record_block_reason,
    split_values,
)

EMBY_ITEM_FIELDS = (
    "Path,DateCreated,Genres,ProviderIds,RunTimeTicks,SeriesId,SeriesName,"
    "ParentIndexNumber,IndexNumber,UserDataPlayCount,UserDataLastPlayedDate,"
    "UserDataPlayedPercentage,UserDataIsFavorite"
)


class AdapterError(RuntimeError):
    """Raised when a media server cannot satisfy a scan or deletion request."""


def _value(obj: Any, name: str, default: Any = None) -> Any:
    """Read either a mapping key or an object attribute."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _type_value(value: Any) -> str:
    """Return an enum value or plain string service type."""
    return str(getattr(value, "value", value) or "").strip().lower()


def _standardize_host(host: Any) -> str:
    text = str(host or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = f"http://{text}"
    return text.rstrip("/") + "/"


def _service_config(service: Any) -> dict[str, Any]:
    config = _value(_value(service, "config"), "config", {})
    return dict(config) if isinstance(config, Mapping) else {}


def _response_json(response: Any) -> Any:
    """Read and close a RequestUtils response."""
    if response is None:
        raise AdapterError("媒体服务器无响应")
    try:
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            raise AdapterError(f"媒体服务器返回 HTTP {status or '未知'}")
        return response.json()
    except AdapterError:
        raise
    except Exception as error:
        raise AdapterError("媒体服务器返回了无法解析的数据") from error
    finally:
        try:
            response.close()
        except Exception:
            pass


def _items(payload: Any, key: str = "Items") -> list[dict[str, Any]]:
    """Read a list field from a response object."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _series_record(
    series_id: str,
    episodes: Sequence[Mapping[str, Any]],
    series_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a normalized series record from all currently stored episodes."""
    episode_records = [normalize_record(item) for item in episodes]
    series_meta = normalize_record(series_meta or {})
    paths = [
        path
        for episode in episode_records
        for path in (episode.get("paths") or [])
    ]
    title = (
        series_meta.get("title")
        or (episode_records[0].get("series_title") if episode_records else "")
        or series_id
    )
    genres = list(series_meta.get("genres") or [])
    if not genres:
        for episode in episode_records:
            genres.extend(episode.get("genres") or [])
    return normalize_record(
        {
            "kind": "series",
            "item_id": series_id,
            "series_id": series_id,
            "title": title,
            "series_title": title,
            "paths": paths,
            "episodes": episode_records,
            "played": bool(episode_records)
            and all(as_bool(episode.get("played")) for episode in episode_records),
            "percentage": min(
                (percentage(episode) for episode in episode_records),
                default=0.0,
            ),
            "last_played": newest_timestamp(
                [episode.get("last_played") for episode in episode_records]
            ),
            "added_at": newest_timestamp(
                [episode.get("added_at") for episode in episode_records]
            ),
            "favorite": any(as_bool(episode.get("favorite")) for episode in episode_records),
            "genres": genres,
        }
    )


def _candidate_from_record(
    *,
    service_name: str,
    service_type: str,
    user_name: str,
    user_id: str,
    category: str,
    record: Mapping[str, Any],
    library_name: str,
    scan_id: str,
) -> dict[str, Any]:
    """Create a serializable candidate with an opaque selection key."""
    item_id = str(record.get("item_id") or "").strip()
    kind = str(record.get("kind") or "").strip()
    paths = split_values(record.get("paths"))
    episodes = list(record.get("episodes") or [])
    return {
        "key": candidate_key(service_name, kind, item_id),
        "scan_id": scan_id,
        "server": service_name,
        "server_type": service_type,
        "user": user_name,
        "user_id": user_id,
        "category": category,
        "kind": kind,
        "item_id": item_id,
        "series_id": str(record.get("series_id") or "").strip(),
        "title": str(record.get("title") or "").strip(),
        "series_title": str(record.get("series_title") or "").strip(),
        "season": record.get("season"),
        "episode": record.get("episode"),
        "episode_count": len(episodes),
        "path": paths[0] if paths else "",
        "paths": paths,
        "played": as_bool(record.get("played")),
        "percentage": percentage(record),
        "last_played": iso_timestamp(record.get("last_played")),
        "added_at": iso_timestamp(record.get("added_at")),
        "favorite": as_bool(record.get("favorite")),
        "library": library_name,
        "genres": split_values(record.get("genres")),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


def _collect_candidates(
    *,
    records: Sequence[Mapping[str, Any]],
    options: Mapping[str, Any],
    service_name: str,
    service_type: str,
    user_name: str,
    user_id: str,
    library_name: str,
    base_category: str,
    scan_id: str,
    limit: int,
) -> dict[str, Any]:
    """Apply watched/safety policy and group records into selectable candidates."""
    categories = set(split_values(options.get("categories")))
    tv_mode = str(options.get("tv_mode") or "episode")
    anime_keywords = split_values(options.get("anime_keywords"))
    skipped: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    truncated = False

    normalized = [normalize_record(record) for record in records]
    movies = [record for record in normalized if record.get("kind") == "movie"]
    episodes = [record for record in normalized if record.get("kind") == "episode"]
    series_by_id = {
        str(record.get("item_id")): record
        for record in normalized
        if record.get("kind") == "series" and record.get("item_id")
    }

    def add_candidate(category: str, record: Mapping[str, Any]) -> None:
        nonlocal truncated
        if category not in categories:
            return
        reason = record_block_reason(record, options)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            return
        candidate = _candidate_from_record(
            service_name=service_name,
            service_type=service_type,
            user_name=user_name,
            user_id=user_id,
            category=category,
            record=record,
            library_name=library_name,
            scan_id=scan_id,
        )
        if candidate["key"] in seen_keys:
            return
        if len(candidates) >= limit:
            truncated = True
            return
        seen_keys.add(candidate["key"])
        candidates.append(candidate)

    for record in movies:
        category = classify_category(base_category, library_name, record, anime_keywords)
        add_candidate(category, record)

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for episode in episodes:
        series_id = str(episode.get("series_id") or "").strip()
        if not series_id:
            if tv_mode == "episode":
                category = classify_category(base_category, library_name, episode, anime_keywords)
                add_candidate(category, episode)
            else:
                skipped["分集缺少剧集标识"] = skipped.get("分集缺少剧集标识", 0) + 1
            continue
        grouped.setdefault(series_id, []).append(episode)

    if tv_mode == "episode":
        for group in grouped.values():
            for episode in group:
                category = classify_category(base_category, library_name, episode, anime_keywords)
                add_candidate(category, episode)
    else:
        for series_id, group in grouped.items():
            series_meta = series_by_id.get(series_id)
            combined_for_category = normalize_record(
                {
                    "title": (series_meta or {}).get("title"),
                    "series_title": (series_meta or {}).get("title")
                    or (group[0].get("series_title") if group else ""),
                    "genres": (series_meta or {}).get("genres") or [],
                    "paths": [
                        path
                        for episode in group
                        for path in (episode.get("paths") or [])
                    ],
                }
            )
            category = classify_category(
                base_category,
                library_name,
                combined_for_category,
                anime_keywords,
            )
            add_candidate(category, _series_record(series_id, group, series_meta))

        # A series without episode records cannot prove that every stored
        # episode was watched, so it is deliberately not a deletion candidate.
        for series_id, record in series_by_id.items():
            if series_id in grouped:
                continue
            category = classify_category(base_category, library_name, record, anime_keywords)
            if category in categories:
                skipped["未找到已入库分集，无法确认整剧已看完"] = (
                    skipped.get("未找到已入库分集，无法确认整剧已看完", 0) + 1
                )

    return {
        "candidates": candidates,
        "skipped": skipped,
        "truncated": truncated,
    }


class MediaAdapter(ABC):
    """Common interface used by the plugin's scan and delete orchestration."""

    service_type = ""

    def __init__(self, service_name: str, service: Any):
        self.service_name = service_name
        self.service = service
        self.config = _service_config(service)
        self.instance = _value(service, "instance")

    @abstractmethod
    def scan(self, options: Mapping[str, Any], scan_id: str) -> dict[str, Any]:
        """Scan supported libraries and return candidates and skip statistics."""

    @abstractmethod
    def refresh(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Re-read current media state immediately before deletion."""

    @abstractmethod
    def delete(self, candidate: Mapping[str, Any]) -> str:
        """Delete one item and return a human-readable success message."""


class EmbyFamilyAdapter(MediaAdapter):
    """Shared Emby/Jellyfin HTTP implementation."""

    def __init__(self, service_name: str, service: Any, service_type: str):
        super().__init__(service_name, service)
        self.service_type = service_type
        self.host = _standardize_host(
            self.config.get("host") or _value(self.instance, "_host")
        )
        self.api_key = str(
            self.config.get("apikey")
            or _value(self.instance, "_apikey")
            or ""
        ).strip()
        self.configured_username = str(self.config.get("username") or "").strip()
        self.instance_user_id = str(_value(self.instance, "user") or "").strip()
        if not self.host or not self.api_key:
            raise AdapterError(f"{self.service_type} 地址或 API Key 未配置")

    def _api_path(self, path: str) -> str:
        clean = path.lstrip("/")
        if self.service_type == "emby" and not clean.startswith("emby/"):
            clean = f"emby/{clean}"
        return clean

    def _url(self, path: str) -> str:
        return self.host + self._api_path(path)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.service_type == "jellyfin":
            headers["Authorization"] = f'MediaBrowser Token="{self.api_key}"'
        else:
            headers["X-Emby-Token"] = self.api_key
        return headers

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        request_params = dict(params or {})
        request_params["api_key"] = self.api_key
        try:
            response = RequestUtils(headers=self._headers()).get_res(
                self._url(path), request_params
            )
        except Exception as error:
            raise AdapterError(f"连接 {self.service_type} 失败") from error
        return _response_json(response)

    def _delete(self, path: str) -> str:
        try:
            response = RequestUtils(headers=self._headers()).delete_res(
                self._url(path),
                params={"api_key": self.api_key},
            )
        except Exception as error:
            raise AdapterError(f"连接 {self.service_type} 失败") from error
        if response is None:
            raise AdapterError("媒体服务器无响应")
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        finally:
            try:
                response.close()
            except Exception:
                pass
        if status not in {200, 202, 204}:
            raise AdapterError(f"删除接口返回 HTTP {status or '未知'}")
        return "媒体服务器已接受删除请求"

    def _users(self) -> list[dict[str, Any]]:
        return _items(self._get_json("Users"), key="")

    def _resolve_user(self, requested_username: str = "") -> tuple[str, str]:
        users = self._users()
        if not users:
            raise AdapterError(f"{self.service_type} 未返回可用用户")
        requested = requested_username.strip()
        if requested:
            for user in users:
                if str(user.get("Name") or "") == requested:
                    return requested, str(user.get("Id") or "")
            raise AdapterError(f"{self.service_type} 中找不到用户 {requested}")
        if self.instance_user_id:
            for user in users:
                if str(user.get("Id") or "") == self.instance_user_id:
                    return str(user.get("Name") or requested), self.instance_user_id
        if self.configured_username:
            for user in users:
                if str(user.get("Name") or "") == self.configured_username:
                    return self.configured_username, str(user.get("Id") or "")
        for user in users:
            policy = user.get("Policy") or {}
            if policy.get("IsAdministrator"):
                return str(user.get("Name") or ""), str(user.get("Id") or "")
        first = users[0]
        return str(first.get("Name") or ""), str(first.get("Id") or "")

    def _libraries(self, user_id: str) -> list[dict[str, Any]]:
        return _items(self._get_json(f"Users/{user_id}/Views"), key="")

    def _list_items(
        self,
        user_id: str,
        *,
        parent_id: str = "",
        item_types: str = "Movie,Series,Episode",
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        start = 0
        page_size = 200
        for _ in range(200):
            params: dict[str, Any] = {
                "Recursive": "true",
                "IncludeItemTypes": item_types,
                "Fields": EMBY_ITEM_FIELDS,
                "EnableUserData": "true",
                "CollapseBoxSetItems": "false",
                "Limit": page_size,
                "StartIndex": start,
            }
            if parent_id:
                params["ParentId"] = parent_id
            payload = self._get_json(f"Users/{user_id}/Items", params)
            page = _items(payload)
            result.extend(page)
            total = payload.get("TotalRecordCount") if isinstance(payload, Mapping) else None
            start += len(page)
            if not page or len(page) < page_size:
                break
            if isinstance(total, int) and start >= total:
                break
        return result

    def _get_item(self, user_id: str, item_id: str) -> dict[str, Any]:
        payload = self._get_json(
            f"Users/{user_id}/Items",
            {
                "Ids": item_id,
                "Recursive": "true",
                "Fields": EMBY_ITEM_FIELDS,
                "EnableUserData": "true",
                "Limit": 1,
            },
        )
        items = _items(payload)
        if not items:
            raise AdapterError("媒体条目已不存在")
        return items[0]

    def _record(self, item: Mapping[str, Any]) -> dict[str, Any]:
        item_type = str(item.get("Type") or "").lower()
        user_data = item.get("UserData") or {}
        played = as_bool(user_data.get("Played"))
        position = float(user_data.get("PlaybackPositionTicks") or 0)
        runtime = float(item.get("RunTimeTicks") or 0)
        played_percentage = (
            position / runtime * 100
            if runtime > 0 and position > 0
            else (100.0 if played else 0.0)
        )
        if user_data.get("PlayedPercentage") is not None:
            try:
                played_percentage = float(user_data.get("PlayedPercentage"))
            except (TypeError, ValueError):
                pass
        kind = {
            "movie": "movie",
            "series": "series",
            "episode": "episode",
        }.get(item_type, item_type)
        path = str(item.get("Path") or "").strip()
        series_id = str(
            item.get("SeriesId")
            or (item.get("Id") if kind == "series" else "")
            or ""
        ).strip()
        return normalize_record(
            {
                "kind": kind,
                "item_id": str(item.get("Id") or ""),
                "series_id": series_id,
                "title": str(item.get("Name") or ""),
                "series_title": str(item.get("SeriesName") or item.get("Name") or "")
                if kind == "episode"
                else str(item.get("Name") or ""),
                "season": item.get("ParentIndexNumber"),
                "episode": item.get("IndexNumber"),
                "paths": [path] if path else [],
                "played": played,
                "percentage": played_percentage,
                "last_played": user_data.get("LastPlayedDate"),
                "added_at": item.get("DateCreated"),
                "favorite": as_bool(user_data.get("IsFavorite")),
                "genres": item.get("Genres") or [],
            }
        )

    def scan(self, options: Mapping[str, Any], scan_id: str) -> dict[str, Any]:
        user_name, user_id = self._resolve_user(str(options.get("server_user") or ""))
        if not user_id:
            raise AdapterError(f"{self.service_type} 用户标识为空")
        libraries = self._libraries(user_id)
        categories = set(split_values(options.get("categories")))
        limit = int(options.get("max_candidates") or 500)
        candidates: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        scanned = 0
        truncated = False
        errors: list[str] = []

        for library in libraries:
            collection_type = str(library.get("CollectionType") or "").lower()
            if collection_type == "movies":
                base_category = "movie"
            elif collection_type in {"tvshows", "tvshow"}:
                base_category = "tv"
            else:
                continue
            if (
                base_category == "movie"
                and not ({"movie", "anime"} & categories)
            ) or (
                base_category == "tv"
                and not ({"tv", "anime"} & categories)
            ):
                continue
            library_id = str(library.get("Id") or "")
            library_name = str(library.get("Name") or library_id)
            if not library_id:
                continue
            try:
                items = self._list_items(user_id, parent_id=library_id)
                records = [self._record(item) for item in items]
                scanned += len(records)
                result = _collect_candidates(
                    records=records,
                    options=options,
                    service_name=self.service_name,
                    service_type=self.service_type,
                    user_name=user_name,
                    user_id=user_id,
                    library_name=library_name,
                    base_category=base_category,
                    scan_id=scan_id,
                    limit=max(limit - len(candidates), 0),
                )
                candidates.extend(result["candidates"])
                truncated = truncated or bool(result["truncated"])
                for reason, count in result["skipped"].items():
                    skipped[reason] = skipped.get(reason, 0) + int(count)
            except AdapterError as error:
                errors.append(f"{library_name}: {error}")

        return {
            "server": self.service_name,
            "server_type": self.service_type,
            "user": user_name,
            "user_id": user_id,
            "scanned": scanned,
            "candidates": candidates,
            "skipped": skipped,
            "truncated": truncated,
            "errors": errors,
        }

    def refresh(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        user_id = str(candidate.get("user_id") or "")
        if not user_id:
            raise AdapterError("候选缺少媒体服务器用户标识")
        item_id = str(candidate.get("item_id") or "")
        if not item_id:
            raise AdapterError("候选缺少媒体条目标识")
        if candidate.get("kind") == "series":
            episodes = [
                self._record(item)
                for item in self._list_items(
                    user_id,
                    parent_id=item_id,
                    item_types="Episode",
                )
            ]
            series_meta: dict[str, Any] = {}
            try:
                series_meta = self._record(self._get_item(user_id, item_id))
            except AdapterError:
                # Episodes still provide enough evidence for the all-watched
                # check if the parent series metadata has disappeared.
                pass
            return _series_record(item_id, episodes, series_meta)
        return self._record(self._get_item(user_id, item_id))

    def delete(self, candidate: Mapping[str, Any]) -> str:
        item_id = str(candidate.get("item_id") or "")
        if not item_id:
            raise AdapterError("候选缺少媒体条目标识")
        return self._delete(f"Items/{item_id}")


class PlexAdapter(MediaAdapter):
    """Plex HTTP adapter using the current server token's user state."""

    service_type = "plex"

    def __init__(self, service_name: str, service: Any):
        super().__init__(service_name, service)
        self.host = _standardize_host(
            self.config.get("host") or _value(self.instance, "_host")
        )
        self.token = str(
            self.config.get("token")
            or self.config.get("apikey")
            or _value(self.instance, "_token")
            or ""
        ).strip()
        if not self.host or not self.token:
            raise AdapterError("Plex 地址或 Token 未配置")

    def _url(self, path: str) -> str:
        return self.host + path.lstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Plex-Token": self.token,
        }

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        request_params = dict(params or {})
        request_params["X-Plex-Token"] = self.token
        try:
            response = RequestUtils(headers=self._headers()).get_res(
                self._url(path), request_params
            )
        except Exception as error:
            raise AdapterError("连接 Plex 失败") from error
        return _response_json(response)

    @staticmethod
    def _metadata(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, Mapping):
            return []
        container = payload.get("MediaContainer") or {}
        if not isinstance(container, Mapping):
            return []
        metadata = container.get("Metadata") or []
        return [item for item in metadata if isinstance(item, dict)]

    @staticmethod
    def _sections(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, Mapping):
            return []
        container = payload.get("MediaContainer") or {}
        if not isinstance(container, Mapping):
            return []
        directories = container.get("Directory") or []
        return [item for item in directories if isinstance(item, dict)]

    def _record(self, item: Mapping[str, Any]) -> dict[str, Any]:
        item_type = str(item.get("type") or "").lower()
        view_count = int(item.get("viewCount") or 0)
        view_offset = float(item.get("viewOffset") or 0)
        duration = float(item.get("duration") or 0)
        if duration > 0 and view_offset > 0:
            played_percentage = view_offset / duration * 100
        elif view_count > 0:
            played_percentage = 100.0
        else:
            played_percentage = 0.0
        paths: list[str] = []
        for media in item.get("Media") or []:
            if not isinstance(media, Mapping):
                continue
            direct = media.get("file")
            if direct:
                paths.append(str(direct))
            for part in media.get("Part") or []:
                if isinstance(part, Mapping) and part.get("file"):
                    paths.append(str(part.get("file")))
        if item.get("file"):
            paths.append(str(item.get("file")))
        genres = [
            str(genre.get("tag"))
            for genre in (item.get("Genre") or [])
            if isinstance(genre, Mapping) and genre.get("tag")
        ]
        # Plex does not expose a reliable favorite flag on every server
        # version. Treating a positive user rating as protected is the safer
        # behavior and can be disabled by turning off favorite protection.
        favorite = as_bool(item.get("isFavorite")) or bool(item.get("userRating"))
        series_id = str(
            item.get("grandparentRatingKey")
            or (item.get("ratingKey") if item_type == "show" else "")
            or ""
        )
        series_title = str(
            item.get("grandparentTitle")
            or item.get("title")
            or ""
        )
        return normalize_record(
            {
                "kind": {
                    "movie": "movie",
                    "show": "series",
                    "episode": "episode",
                }.get(item_type, item_type),
                "item_id": str(item.get("ratingKey") or item.get("key") or ""),
                "series_id": series_id,
                "title": str(item.get("title") or ""),
                "series_title": (
                    series_title
                    if item_type == "episode"
                    else str(item.get("title") or "")
                ),
                "season": item.get("parentIndex"),
                "episode": item.get("index"),
                "paths": paths,
                "played": view_count > 0 or as_bool(item.get("isPlayed")),
                "percentage": played_percentage,
                "last_played": item.get("lastViewedAt"),
                "added_at": item.get("addedAt"),
                "favorite": favorite,
                "genres": genres,
            }
        )

    def _section_items(self, section_key: str, item_type: int) -> list[dict[str, Any]]:
        params = {
            "type": item_type,
            "includeGuids": 1,
            "includeMedia": 1,
            "includeUserState": 1,
        }
        payload = self._get_json(
            f"/library/sections/{section_key}/all",
            params,
        )
        metadata = self._metadata(payload)
        if metadata or item_type != 4:
            return metadata
        # Some older Plex servers ignore type=4 on section endpoints. A
        # second unfiltered request still lets the caller filter episodes.
        return self._metadata(
            self._get_json(
                f"/library/sections/{section_key}/all",
                {
                    "includeGuids": 1,
                    "includeMedia": 1,
                    "includeUserState": 1,
                },
            )
        )

    def scan(self, options: Mapping[str, Any], scan_id: str) -> dict[str, Any]:
        categories = set(split_values(options.get("categories")))
        limit = int(options.get("max_candidates") or 500)
        sections = self._sections(self._get_json("/library/sections"))
        candidates: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        scanned = 0
        truncated = False
        errors: list[str] = []

        for section in sections:
            section_type = str(section.get("type") or "").lower()
            if section_type == "movie":
                base_category = "movie"
                plex_type = 1
            elif section_type == "show":
                base_category = "tv"
                plex_type = 4
            else:
                continue
            if (
                base_category == "movie"
                and not ({"movie", "anime"} & categories)
            ) or (
                base_category == "tv"
                and not ({"tv", "anime"} & categories)
            ):
                continue
            section_key = str(section.get("key") or "")
            library_name = str(section.get("title") or section_key)
            if not section_key:
                continue
            try:
                metadata = self._section_items(section_key, plex_type)
                records = [self._record(item) for item in metadata]
                if base_category == "tv":
                    records = [record for record in records if record.get("kind") == "episode"]
                scanned += len(records)
                result = _collect_candidates(
                    records=records,
                    options=options,
                    service_name=self.service_name,
                    service_type=self.service_type,
                    user_name="Plex Token 用户",
                    user_id="token",
                    library_name=library_name,
                    base_category=base_category,
                    scan_id=scan_id,
                    limit=max(limit - len(candidates), 0),
                )
                candidates.extend(result["candidates"])
                truncated = truncated or bool(result["truncated"])
                for reason, count in result["skipped"].items():
                    skipped[reason] = skipped.get(reason, 0) + int(count)
            except AdapterError as error:
                errors.append(f"{library_name}: {error}")

        return {
            "server": self.service_name,
            "server_type": self.service_type,
            "user": "Plex Token 用户",
            "user_id": "token",
            "scanned": scanned,
            "candidates": candidates,
            "skipped": skipped,
            "truncated": truncated,
            "errors": errors,
        }

    def refresh(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        item_id = str(candidate.get("item_id") or "")
        if not item_id:
            raise AdapterError("候选缺少 Plex 条目标识")
        if candidate.get("kind") == "series":
            metadata = self._metadata(
                self._get_json(
                    f"/library/metadata/{item_id}/allLeaves",
                    {"includeMedia": 1, "includeUserState": 1},
                )
            )
            episodes = [
                self._record(item)
                for item in metadata
                if str(item.get("type") or "").lower() == "episode"
            ]
            return _series_record(item_id, episodes)
        metadata = self._metadata(
            self._get_json(
                f"/library/metadata/{item_id}",
                {"includeMedia": 1, "includeUserState": 1},
            )
        )
        if not metadata:
            raise AdapterError("Plex 条目已不存在")
        return self._record(metadata[0])

    def delete(self, candidate: Mapping[str, Any]) -> str:
        item_id = str(candidate.get("item_id") or "")
        if not item_id:
            raise AdapterError("候选缺少 Plex 条目标识")
        try:
            response = RequestUtils(headers=self._headers()).delete_res(
                self._url(f"/library/metadata/{item_id}"),
                params={"X-Plex-Token": self.token},
            )
        except Exception as error:
            raise AdapterError("连接 Plex 失败") from error
        if response is None:
            raise AdapterError("Plex 无响应")
        try:
            status = int(getattr(response, "status_code", 0) or 0)
        finally:
            try:
                response.close()
            except Exception:
                pass
        if status not in {200, 202, 204}:
            raise AdapterError(f"Plex 删除接口返回 HTTP {status or '未知'}")
        return "Plex 已接受删除请求"


def build_adapter(service_name: str, service: Any) -> MediaAdapter:
    """Create a supported adapter from a MediaServerHelper service."""
    service_type = _type_value(_value(service, "type"))
    if service_type in {"emby", "jellyfin"}:
        return EmbyFamilyAdapter(service_name, service, service_type)
    if service_type == "plex":
        return PlexAdapter(service_name, service)
    raise AdapterError(f"暂不支持媒体服务器类型：{service_type or '未知'}")
