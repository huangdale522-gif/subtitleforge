"""SubtitleBest share-sub API provider."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import (
    DownloadedAsset,
    MediaType,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from .base import ProviderError, ProviderNotConfigured, SubtitleProvider


class SubtitleBestProvider(SubtitleProvider):
    """Use the SubtitleBest API when an IMDb id and account key are available."""

    name = "subtitle_best"
    base_url = "https://api.subtitle.best/share-sub/v1"

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        header_token: str,
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._header_token = header_token.strip()

    @property
    def configured(self) -> bool:
        """Return whether both the account and API client credentials exist."""

        return bool(self._api_key and self._header_token)

    def _headers(self) -> dict[str, str]:
        """Build API headers without storing them on candidates."""

        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._header_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _language(value: Any) -> SubtitleLanguage | None:
        """Map SubtitleBest's language ids to simplified or traditional Chinese."""

        try:
            language_id = int(value)
        except (TypeError, ValueError):
            return None
        if language_id in {1, 3, 7, 10}:
            return SubtitleLanguage.SIMPLIFIED
        if language_id in {2, 4, 8, 11}:
            return SubtitleLanguage.TRADITIONAL
        return None

    @staticmethod
    def _format(value: Any) -> str:
        """Normalize a subtitle extension."""

        text = str(value or "").lower().strip().lstrip(".")
        return text if text in {"srt", "ass", "ssa", "vtt", "sub"} else "unknown"

    @staticmethod
    def _imdb(value: str | None) -> str:
        """Normalize an IMDb id expected by SubtitleBest."""

        text = (value or "").strip()
        if not text:
            return ""
        return text if text.startswith("tt") else f"tt{text}"

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Call one SubtitleBest endpoint."""

        try:
            response = await self._client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"SubtitleBest request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("SubtitleBest rejected the configured credentials")
        if response.status_code == 429:
            raise ProviderError("SubtitleBest is rate limited")
        if response.status_code >= 400:
            raise ProviderError(
                f"SubtitleBest returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("SubtitleBest returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError("SubtitleBest returned an unexpected response")
        if int(body.get("status", 0) or 0) != 0:
            raise ProviderError("SubtitleBest returned a non-zero status")
        return body

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Search a movie or one TV episode by IMDb id."""

        if not self.configured:
            raise ProviderNotConfigured(
                "SubtitleBest API key and header token are not configured"
            )
        imdb_id = self._imdb(request.imdb_id)
        if not imdb_id:
            return []
        if request.media_type is MediaType.TV:
            if request.season is None or request.episode is None:
                return []
            body = await self._post(
                "/search-tv-eps",
                {
                    "imdb_id": imdb_id,
                    "season": request.season,
                    "episode": request.episode,
                    "api_key": self._api_key,
                },
            )
        else:
            body = await self._post(
                "/search-movie",
                {"imdb_id": imdb_id, "api_key": self._api_key},
            )
        items = body.get("subtitles") or []
        if not isinstance(items, list):
            raise ProviderError("SubtitleBest returned an invalid subtitle list")
        candidates: list[SubtitleCandidate] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("sub_sha256"):
                continue
            language_id = item.get("language")
            language = self._language(language_id)
            if language is None:
                continue
            season = request.season if request.media_type is MediaType.TV else None
            episode = request.episode if request.media_type is MediaType.TV else None
            candidates.append(
                SubtitleCandidate(
                    provider=self.name,
                    candidate_id=str(item["sub_sha256"]),
                    title=str(item.get("title") or item["sub_sha256"]),
                    language=language,
                    format=self._format(item.get("ext")),
                    season=_as_int(item.get("season")) or season,
                    episode=_as_int(item.get("episode")) or episode,
                    download_ref={
                        "sub_sha256": str(item["sub_sha256"]),
                        "imdb_id": imdb_id,
                        "is_movie": request.media_type is MediaType.MOVIE,
                        "season": _as_int(item.get("season")) or season or 0,
                        "episode": _as_int(item.get("episode")) or episode or 0,
                        "language": int(language_id),
                        "token": str(item.get("token") or ""),
                    },
                )
            )
        return candidates

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Resolve a short-lived download URL and fetch the subtitle."""

        payload = {
            "sub_sha256": candidate.download_ref.get("sub_sha256", ""),
            "imdb_id": candidate.download_ref.get("imdb_id", ""),
            "is_movie": bool(candidate.download_ref.get("is_movie")),
            "season": int(candidate.download_ref.get("season") or 0),
            "episode": int(candidate.download_ref.get("episode") or 0),
            "season_package_id": "",
            "language": int(candidate.download_ref.get("language") or 0),
            "api_key": self._api_key,
            "token": str(candidate.download_ref.get("token") or ""),
        }
        body = await self._post("/get-dl-url", payload)
        url = body.get("download_link")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise ProviderError("SubtitleBest did not return a valid download URL")
        try:
            response = await self._client.get(url, headers={"Accept": "*/*"})
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"SubtitleBest download failed: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"SubtitleBest download returned HTTP {response.status_code}"
            )
        file_name = candidate.title
        if candidate.format != "unknown" and not file_name.lower().endswith(
            f".{candidate.format}"
        ):
            file_name = f"{file_name}.{candidate.format}"
        return DownloadedAsset(content=response.content, file_name=file_name)


def _as_int(value: Any) -> int | None:
    """Convert optional API integers."""

    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
