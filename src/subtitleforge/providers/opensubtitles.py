"""OpenSubtitles.com REST API provider."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..models import (
    DownloadedAsset,
    MediaType,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from .base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaError,
    ProviderUnavailableError,
    SubtitleProvider,
)


class OpenSubtitlesProvider(SubtitleProvider):
    """Official OpenSubtitles provider using API-key search and JWT downloads."""

    name = "opensubtitles"
    base_url = "https://api.opensubtitles.com/api/v1"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        username: str = "",
        password: str = "",
        user_agent: str = "SubtitleForge/0.4.2",
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._username = username.strip()
        self._password = password
        self._user_agent = user_agent
        self._jwt: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        """Search needs an API key; download needs a complete account."""

        return bool(self._api_key and self._username and self._password)

    def _headers(self, authenticated: bool = False) -> dict[str, str]:
        """Build request headers without ever logging secret values."""

        headers = {
            "Accept": "application/json",
            "Api-Key": self._api_key,
            "User-Agent": self._user_agent,
            "Content-Type": "application/json",
        }
        if authenticated and self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"
        return headers

    @staticmethod
    def _language(value: str) -> SubtitleLanguage | None:
        """Map an OpenSubtitles language code to the service vocabulary."""

        lowered = value.strip().lower()
        if lowered in {"zh-cn", "zh-hans", "zho-hans", "chi", "chs"}:
            return SubtitleLanguage.SIMPLIFIED
        if lowered in {"zh-tw", "zh-hant", "zho-hant", "cht"}:
            return SubtitleLanguage.TRADITIONAL
        return None

    @staticmethod
    def _format(file_name: str | None) -> str:
        """Extract a subtitle extension from a provider file name."""

        if not file_name or "." not in file_name:
            return "unknown"
        return file_name.rsplit(".", 1)[-1].lower()

    def _search_params(self, request: SearchRequest) -> dict[str, Any]:
        """Build media-id-first search parameters."""

        params: dict[str, Any] = {
            "languages": ",".join(
                "zh-cn" if item is SubtitleLanguage.SIMPLIFIED else "zh-tw"
                for item in request.languages
            ),
            "type": "episode" if request.media_type is MediaType.TV else "movie",
        }
        query = (
            request.query or request.english_title or request.original_title or ""
        ).strip()
        if request.media_type is MediaType.TV:
            if request.imdb_id:
                params["parent_imdb_id"] = request.imdb_id.removeprefix("tt")
            elif request.tmdb_id:
                params["parent_tmdb_id"] = request.tmdb_id
            if request.season is not None:
                params["season_number"] = request.season
            if request.episode is not None:
                params["episode_number"] = request.episode
        else:
            if request.imdb_id:
                params["imdb_id"] = request.imdb_id.removeprefix("tt")
            elif request.tmdb_id:
                params["tmdb_id"] = request.tmdb_id
        if query and not any(
            key in params
            for key in ("imdb_id", "tmdb_id", "parent_imdb_id", "parent_tmdb_id")
        ):
            params["query"] = query
        if request.year and "query" in params:
            params["year"] = request.year
        return params

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Search by TMDB/IMDb id, falling back to a title query."""

        if not self._api_key:
            raise ProviderNotConfigured("OpenSubtitles API key is not configured")
        candidates = await self._search_params_once(self._search_params(request))
        if candidates:
            return candidates
        title_query = (
            request.query or request.english_title or request.original_title or ""
        ).strip()
        if title_query and "query" not in self._search_params(request):
            fallback: dict[str, Any] = {
                "languages": self._search_params(request)["languages"],
                "type": self._search_params(request)["type"],
                "query": title_query,
            }
            if request.year:
                fallback["year"] = request.year
            if request.media_type is MediaType.TV and request.season is not None:
                fallback["season_number"] = request.season
            return await self._search_params_once(fallback)
        return []

    async def _search_params_once(
        self,
        params: dict[str, Any],
    ) -> list[SubtitleCandidate]:
        """Execute one OpenSubtitles search request."""

        try:
            response = await self._client.get(
                f"{self.base_url}/subtitles",
                params=params,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"OpenSubtitles search failed: {type(exc).__name__}"
            ) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("OpenSubtitles rejected the API key")
        if response.status_code == 429:
            raise ProviderQuotaError("OpenSubtitles search is rate limited")
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"OpenSubtitles search temporarily unavailable "
                f"(HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"OpenSubtitles search returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("OpenSubtitles returned invalid JSON") from exc
        candidates: list[SubtitleCandidate] = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes") or {}
            if not isinstance(attributes, dict):
                continue
            files = attributes.get("files") or []
            for file_item in files if isinstance(files, list) else []:
                if not isinstance(file_item, dict) or not file_item.get("file_id"):
                    continue
                file_name = str(file_item.get("file_name") or "")
                candidate = SubtitleCandidate(
                    provider=self.name,
                    candidate_id=f"{item.get('id', '')}:{file_item['file_id']}",
                    title=str(
                        attributes.get("release")
                        or file_name
                        or item.get("id")
                        or "OpenSubtitles"
                    ),
                    release_name=file_name or None,
                    language=self._language(str(attributes.get("language") or "")),
                    format=self._format(file_name),
                    season=_as_int(
                        (attributes.get("feature_details") or {}).get("season_number")
                    ),
                    episode=_as_int(
                        (attributes.get("feature_details") or {}).get("episode_number")
                    ),
                    year=_as_int((attributes.get("feature_details") or {}).get("year")),
                    score=float(attributes.get("ratings") or 0),
                    download_count=_as_int(attributes.get("download_count")) or 0,
                    machine_translated=bool(
                        attributes.get("machine_translated")
                        or attributes.get("ai_translated")
                    ),
                    download_ref={"file_id": int(file_item["file_id"])},
                )
                candidates.append(candidate)
        return candidates

    async def _login(self) -> None:
        """Create or refresh the download session."""

        if not self.configured:
            raise ProviderNotConfigured(
                "OpenSubtitles username/password are not configured"
            )
        async with self._login_lock:
            if self._jwt:
                return
            try:
                response = await self._client.post(
                    f"{self.base_url}/login",
                    json={"username": self._username, "password": self._password},
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(
                    f"OpenSubtitles login failed: {type(exc).__name__}"
                ) from exc
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    "OpenSubtitles login temporarily unavailable "
                    f"(HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"OpenSubtitles login returned HTTP {response.status_code}"
                )
            try:
                token = response.json().get("token")
            except (ValueError, AttributeError) as exc:
                raise ProviderError(
                    "OpenSubtitles login returned invalid JSON"
                ) from exc
            if not token:
                raise ProviderError("OpenSubtitles login did not return a token")
            self._jwt = str(token)

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Resolve a temporary link and download the subtitle file."""

        file_id = candidate.download_ref.get("file_id")
        if not isinstance(file_id, int):
            raise ProviderError("OpenSubtitles candidate has no file id")
        await self._login()
        try:
            response = await self._client.post(
                f"{self.base_url}/download",
                json={"file_id": file_id},
                headers=self._headers(authenticated=True),
            )
            if response.status_code in {401, 403}:
                self._jwt = None
                await self._login()
                response = await self._client.post(
                    f"{self.base_url}/download",
                    json={"file_id": file_id},
                    headers=self._headers(authenticated=True),
                )
            if response.status_code == 429:
                raise ProviderQuotaError("OpenSubtitles download quota is exhausted")
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    "OpenSubtitles download temporarily unavailable "
                    f"(HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"OpenSubtitles download returned HTTP {response.status_code}"
                )
            payload = response.json()
            link = payload.get("link") if isinstance(payload, dict) else None
            if not isinstance(link, str) or not link:
                raise ProviderError("OpenSubtitles download response has no link")
            file_name = str(
                payload.get("file_name") or candidate.release_name or "subtitle.srt"
            )
            file_response = await self._client.get(link)
            if file_response.status_code >= 500:
                raise ProviderUnavailableError(
                    "OpenSubtitles file download temporarily unavailable "
                    f"(HTTP {file_response.status_code})"
                )
            if file_response.status_code >= 400:
                raise ProviderError(
                    f"OpenSubtitles file download returned HTTP {file_response.status_code}"
                )
            return DownloadedAsset(content=file_response.content, file_name=file_name)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"OpenSubtitles download failed: {type(exc).__name__}"
            ) from exc

    async def close(self) -> None:
        """Forget the in-memory download session."""

        self._jwt = None


def _as_int(value: Any) -> int | None:
    """Convert provider values to optional integers."""

    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
