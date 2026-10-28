"""ASSRT.net subtitle provider."""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..models import DownloadedAsset, SearchRequest, SubtitleCandidate, SubtitleLanguage
from .base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaError,
    ProviderUnavailableError,
    SubtitleProvider,
)


class AssrtProvider(SubtitleProvider):
    """ASSRT provider using its token API."""

    name = "assrt"
    base_url = "https://api.assrt.net/v1"

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token.strip()

    @property
    def configured(self) -> bool:
        """Return whether an ASSRT token is available."""

        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        """Build ASSRT API headers."""

        return {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}

    @staticmethod
    def _language(item: dict[str, Any]) -> SubtitleLanguage | None:
        """Infer simplified/traditional language from ASSRT flags and labels."""

        lang = item.get("lang") if isinstance(item.get("lang"), dict) else {}
        text = f"{lang.get('desc', '')} {lang.get('langlist', '')}".lower()
        if any(marker in text for marker in ("繁", "traditional", "cht")):
            return SubtitleLanguage.TRADITIONAL
        if any(marker in text for marker in ("简", "simplified", "chs", "chinese")):
            return SubtitleLanguage.SIMPLIFIED
        return None

    @staticmethod
    def _format(value: Any) -> str:
        """Normalize a provider subtype to a file extension."""

        text = str(value or "").lower().strip().lstrip(".")
        return text if text in {"srt", "ass", "ssa", "vtt", "sub"} else "unknown"

    @staticmethod
    def _season_episode(item: dict[str, Any]) -> tuple[int | None, int | None]:
        """Extract season and episode numbers from common release names."""

        text = f"{item.get('native_name', '')} {item.get('videoname', '')}"
        match = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", text, re.IGNORECASE)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Search by the most useful Chinese or English title."""

        if not self._token:
            raise ProviderNotConfigured("ASSRT token is not configured")
        queries = [
            request.query or request.title,
            request.original_title,
            request.english_title,
        ]
        unique_queries = [item.strip() for item in queries if item and item.strip()]
        candidates: dict[str, SubtitleCandidate] = {}
        for query in dict.fromkeys(unique_queries):
            try:
                response = await self._client.get(
                    f"{self.base_url}/sub/search",
                    params={"q": query, "cnt": 20, "pos": 0},
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(
                    f"ASSRT search failed: {type(exc).__name__}"
                ) from exc
            if response.status_code in {401, 403}:
                raise ProviderError("ASSRT rejected the token")
            if response.status_code == 429:
                raise ProviderQuotaError("ASSRT search is rate limited")
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    f"ASSRT search temporarily unavailable "
                    f"(HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                raise ProviderError(
                    f"ASSRT search returned HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError("ASSRT returned invalid JSON") from exc
            if not isinstance(payload, dict) or int(payload.get("status", -1)) != 0:
                raise ProviderError("ASSRT returned a non-zero status")
            subs = (
                ((payload.get("sub") or {}).get("subs") or [])
                if isinstance(payload.get("sub"), dict)
                else []
            )
            for item in subs if isinstance(subs, list) else []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                subtitle_id = str(item["id"])
                season, episode = self._season_episode(item)
                candidate = SubtitleCandidate(
                    provider=self.name,
                    candidate_id=subtitle_id,
                    title=str(
                        item.get("native_name") or item.get("videoname") or subtitle_id
                    ),
                    release_name=str(item.get("videoname") or "") or None,
                    language=self._language(item),
                    format=self._format(item.get("subtype")),
                    season=season,
                    episode=episode,
                    score=_as_float(item.get("vote_score")),
                    machine_translated=bool(item.get("vote_machine_translate")),
                    download_ref={"subtitle_id": subtitle_id},
                )
                candidates[subtitle_id] = candidate
            if candidates:
                break
        return list(candidates.values())

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Resolve a temporary URL from details and download it."""

        subtitle_id = candidate.download_ref.get("subtitle_id")
        if not subtitle_id:
            raise ProviderError("ASSRT candidate has no subtitle id")
        try:
            response = await self._client.get(
                f"{self.base_url}/sub/detail",
                params={"id": subtitle_id},
                headers=self._headers(),
            )
            if response.status_code >= 400:
                if response.status_code == 429:
                    raise ProviderQuotaError("ASSRT download is rate limited")
                if response.status_code >= 500:
                    raise ProviderUnavailableError(
                        f"ASSRT detail temporarily unavailable "
                        f"(HTTP {response.status_code})"
                    )
                raise ProviderError(
                    f"ASSRT detail returned HTTP {response.status_code}"
                )
            payload = response.json()
            subs = (
                ((payload.get("sub") or {}).get("subs") or [])
                if isinstance(payload, dict)
                else []
            )
            detail = next(
                (
                    item
                    for item in subs
                    if isinstance(item, dict)
                    and str(item.get("id")) == str(subtitle_id)
                ),
                None,
            )
            url = detail.get("url") if detail else None
            if not isinstance(url, str) or not url:
                raise ProviderError("ASSRT detail has no download URL")
            file_name = str(detail.get("filename") or f"assrt-{subtitle_id}.srt")
            file_response = await self._client.get(url)
            if file_response.status_code >= 400:
                if file_response.status_code == 429:
                    raise ProviderQuotaError("ASSRT file download is rate limited")
                if file_response.status_code >= 500:
                    raise ProviderUnavailableError(
                        f"ASSRT file download temporarily unavailable "
                        f"(HTTP {file_response.status_code})"
                    )
                raise ProviderError(
                    f"ASSRT file download returned HTTP {file_response.status_code}"
                )
            return DownloadedAsset(content=file_response.content, file_name=file_name)
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderUnavailableError(
                f"ASSRT download failed: {type(exc).__name__}"
            ) from exc


def _as_float(value: Any) -> float:
    """Convert provider score values to floats."""

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
