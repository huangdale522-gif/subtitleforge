"""Shooter subtitle provider using the upstream ChineseSubFinder protocol."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import DownloadedAsset, SearchRequest, SubtitleCandidate
from .base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaError,
    SubtitleProvider,
)


class ShooterProvider(SubtitleProvider):
    """Query Shooter by the video's multi-sample MD5 fingerprint."""

    name = "shooter"
    base_url = "https://www.shooter.cn/api/subapi.php"

    def __init__(self, client: httpx.AsyncClient, enabled: bool = False) -> None:
        self._client = client
        self._enabled = enabled

    @property
    def configured(self) -> bool:
        """Return whether this optional provider is enabled."""

        return self._enabled

    @staticmethod
    def compute_hash(path: Path) -> str:
        """Compute Shooter's four-sample MD5 fingerprint."""

        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if size < 0xF000:
            return ""
        positions = (4 * 1024, int(size / 3 * 2), int(size / 3), size - 8 * 1024)
        parts: list[str] = []
        try:
            with path.open("rb") as handle:
                for position in positions:
                    handle.seek(position)
                    parts.append(hashlib.md5(handle.read(4 * 1024)).hexdigest())
        except OSError:
            return ""
        return ";".join(parts)

    @staticmethod
    def _format(value: Any, url: str) -> str:
        """Return a supported extension from the provider or URL."""

        text = str(value or "").casefold().strip().lstrip(".")
        if not text:
            text = Path(urlsplit(url).path).suffix.casefold().lstrip(".")
        return text if text in {"srt", "ass", "ssa", "vtt", "sub"} else "unknown"

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Search the exact video fingerprint."""

        if not self._enabled:
            raise ProviderNotConfigured("Shooter provider is disabled")
        raw_video_path = getattr(request, "video_path", None)
        if not raw_video_path:
            return []
        video_path = Path(str(raw_video_path)).expanduser()
        file_hash = self.compute_hash(video_path)
        if not file_hash:
            return []
        try:
            response = await self._client.post(
                self.base_url,
                data={
                    "filehash": file_hash,
                    "pathinfo": video_path.name,
                    "format": "json",
                    "lang": "Chn",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Shooter search failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ProviderQuotaError("Shooter search is rate limited")
        if response.status_code >= 400:
            raise ProviderError(
                f"Shooter search returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Shooter returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise ProviderError("Shooter returned an unexpected response")

        candidates: list[SubtitleCandidate] = []
        for collection_index, collection in enumerate(payload):
            if not isinstance(collection, dict):
                continue
            files = collection.get("files") or []
            for file_index, item in enumerate(files if isinstance(files, list) else []):
                if not isinstance(item, dict):
                    continue
                url = str(item.get("link") or "").strip()
                if urlsplit(url).scheme not in {"http", "https"}:
                    continue
                suffix = self._format(item.get("ext"), url)
                release_name = f"shooter-{collection_index}-{file_index}.{suffix}"
                candidates.append(
                    SubtitleCandidate(
                        provider=self.name,
                        candidate_id=f"{collection_index}-{file_index}",
                        title=f"{request.title} {collection.get('desc') or release_name}".strip(),
                        release_name=release_name,
                        language=None,
                        format=suffix,
                        season=request.season,
                        episode=request.episode,
                        year=request.year,
                        score=10.0,
                        download_ref={
                            "url": url,
                            "file_name": release_name,
                            "exact_file_match": True,
                            "delay": _as_int(collection.get("delay")) or 0,
                        },
                    )
                )
        return candidates

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Download a Shooter subtitle URL."""

        url = candidate.download_ref.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise ProviderError("Shooter candidate has no valid download URL")
        try:
            response = await self._client.get(url, headers={"Accept": "*/*"})
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Shooter download failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ProviderQuotaError("Shooter download is rate limited")
        if response.status_code >= 400:
            raise ProviderError(
                f"Shooter download returned HTTP {response.status_code}"
            )
        return DownloadedAsset(
            content=response.content,
            file_name=str(
                candidate.download_ref.get("file_name")
                or candidate.release_name
                or f"shooter-{candidate.candidate_id}.srt"
            ),
        )


def _as_int(value: Any) -> int | None:
    """Convert an optional provider value to an integer."""

    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
