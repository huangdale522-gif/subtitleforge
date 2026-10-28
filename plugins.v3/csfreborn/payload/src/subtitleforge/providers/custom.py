"""A small HTTP manifest provider for user-owned subtitle sources."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import (
    DownloadedAsset,
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


class CustomHttpProvider(SubtitleProvider):
    """Consume a user-supplied JSON subtitle index.

    The endpoint must accept GET query parameters and return either a JSON list
    or ``{"items": [...]}``. Each item may contain ``title``, ``release_name``,
    ``language``, ``format``, ``url``, ``season``, ``episode``, ``year`` and
    ``score``.
    """

    name = "custom"

    def __init__(self, client: httpx.AsyncClient, url: str, token: str = "") -> None:
        self._client = client
        self._url = url.strip()
        self._token = token.strip()

    @property
    def configured(self) -> bool:
        """Return whether a manifest URL is configured."""

        return bool(self._url)

    def _headers(self) -> dict[str, str]:
        """Build optional bearer authentication headers."""

        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _language(value: str | None) -> SubtitleLanguage | None:
        """Map common language labels to the service vocabulary."""

        text = (value or "").lower()
        if any(
            marker in text
            for marker in ("繁", "traditional", "zh-tw", "zh-hant", "cht")
        ):
            return SubtitleLanguage.TRADITIONAL
        if any(
            marker in text
            for marker in ("简", "simplified", "zh-cn", "zh-hans", "chs", "chinese")
        ):
            return SubtitleLanguage.SIMPLIFIED
        return None

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Query the manifest endpoint with normalized search parameters."""

        if not self._url:
            raise ProviderNotConfigured("Custom provider URL is not configured")
        params = {
            "q": request.query or request.title,
            "original_title": request.original_title or "",
            "english_title": request.english_title or "",
            "year": request.year or "",
            "imdb_id": request.imdb_id or "",
            "tmdb_id": request.tmdb_id or "",
            "media_type": request.media_type.value,
            "season": request.season if request.season is not None else "",
            "episode": request.episode if request.episode is not None else "",
            "languages": ",".join(item.value for item in request.languages),
        }
        try:
            response = await self._client.get(
                self._url, params=params, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Custom provider search failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ProviderQuotaError("Custom provider search is rate limited")
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                "Custom provider search temporarily unavailable "
                f"(HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise ProviderError(f"Custom provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Custom provider returned invalid JSON") from exc
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ProviderError(
                "Custom provider response must be a list or contain an items list"
            )
        candidates: list[SubtitleCandidate] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            candidates.append(
                SubtitleCandidate(
                    provider=self.name,
                    candidate_id=str(item.get("id") or index),
                    title=str(
                        item.get("title") or item.get("release_name") or "custom"
                    ),
                    release_name=str(item.get("release_name") or "") or None,
                    language=self._language(str(item.get("language") or "")),
                    format=str(item.get("format") or "unknown").lower().lstrip("."),
                    season=_as_int(item.get("season")),
                    episode=_as_int(item.get("episode")),
                    year=_as_int(item.get("year")),
                    score=_as_float(item.get("score")),
                    download_count=_as_int(item.get("download_count")) or 0,
                    machine_translated=bool(item.get("machine_translated")),
                    download_ref={"url": str(item["url"])},
                )
            )
        return candidates

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Download the URL supplied by the manifest."""

        url = candidate.download_ref.get("url")
        if not isinstance(url, str) or not url:
            raise ProviderError("Custom candidate has no URL")
        headers = {"Accept": "*/*"}
        # Never forward the manifest bearer token to a different host.
        if urlsplit(url).netloc == urlsplit(self._url).netloc:
            headers.update(self._headers())
        try:
            response = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Custom provider download failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ProviderQuotaError("Custom provider download is rate limited")
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                "Custom provider download temporarily unavailable "
                f"(HTTP {response.status_code})"
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"Custom provider download returned HTTP {response.status_code}"
            )
        file_name = (
            candidate.release_name
            or f"custom-{candidate.candidate_id}.{candidate.format}"
        )
        return DownloadedAsset(content=response.content, file_name=file_name)


def _as_int(value: Any) -> int | None:
    """Convert provider values to optional integers."""

    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    """Convert provider values to floats."""

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
