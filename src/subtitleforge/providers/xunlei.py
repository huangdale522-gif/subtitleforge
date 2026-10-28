"""Xunlei file-fingerprint subtitle provider."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..models import DownloadedAsset, SearchRequest, SubtitleCandidate, SubtitleLanguage
from .base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaError,
    SubtitleProvider,
)


class XunleiProvider(SubtitleProvider):
    """Look up subtitles by the video's Xunlei CID fingerprint."""

    name = "xunlei"
    base_url = "http://sub.xmp.sandai.net:8000/subxl"

    def __init__(self, client: httpx.AsyncClient, enabled: bool = True) -> None:
        self._client = client
        self._enabled = enabled

    @property
    def configured(self) -> bool:
        """Return whether this credential-free provider is enabled."""

        return self._enabled

    @staticmethod
    def _language(value: Any) -> SubtitleLanguage | None:
        """Map Xunlei's Chinese language labels to the service vocabulary."""

        text = str(value or "").casefold()
        if any(marker in text for marker in ("繁", "cht", "traditional", "繁体", "繁體")):
            return SubtitleLanguage.TRADITIONAL
        if any(
            marker in text
            for marker in ("简", "chs", "simplified", "简体", "簡體", "中文")
        ):
            return SubtitleLanguage.SIMPLIFIED
        return None

    @staticmethod
    def compute_cid(path: Path) -> str:
        """Compute Xunlei's SHA-1 fingerprint from three 20 KiB samples."""

        try:
            size = path.stat().st_size
        except OSError:
            return ""
        if size < 0xF000:
            return ""
        buffer_size = 0x5000
        digest = hashlib.sha1()
        try:
            with path.open("rb") as handle:
                for position in (0, int(size / 3), size - buffer_size):
                    handle.seek(position)
                    digest.update(handle.read(buffer_size))
        except OSError:
            return ""
        return digest.hexdigest().upper()

    @staticmethod
    def _format(file_name: str) -> str:
        """Return a supported subtitle suffix."""

        suffix = Path(file_name).suffix.casefold().lstrip(".")
        return suffix if suffix in {"srt", "ass", "ssa", "vtt", "sub"} else "unknown"

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Look up the exact video file by its Xunlei CID."""

        if not self._enabled:
            raise ProviderNotConfigured("Xunlei provider is disabled")
        raw_video_path = getattr(request, "video_path", None)
        if not raw_video_path:
            return []
        video_path = Path(str(raw_video_path)).expanduser()
        cid = self.compute_cid(video_path)
        if not cid:
            return []
        try:
            response = await self._client.get(f"{self.base_url}/{cid}.json")
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Xunlei search failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ProviderQuotaError("Xunlei search is rate limited")
        if response.status_code >= 400:
            raise ProviderError(
                f"Xunlei search returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Xunlei returned invalid JSON") from exc
        items = payload.get("sublist", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise ProviderError("Xunlei returned an invalid subtitle list")

        candidates: list[SubtitleCandidate] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            url = str(item.get("surl") or "").strip()
            if urlsplit(url).scheme not in {"http", "https"}:
                continue
            release_name = str(
                item.get("sname") or f"xunlei-{cid}-{index}.srt"
            ).strip()
            score = _as_float(item.get("rate"))
            downloads = _as_int(item.get("svote")) or 0
            candidates.append(
                SubtitleCandidate(
                    provider=self.name,
                    candidate_id=str(item.get("scid") or item.get("cid") or index),
                    title=f"{request.title} {release_name}".strip(),
                    release_name=release_name,
                    language=self._language(item.get("language")),
                    format=self._format(release_name),
                    season=request.season,
                    episode=request.episode,
                    year=request.year,
                    score=score,
                    download_count=downloads,
                    download_ref={
                        "url": url,
                        "file_name": release_name,
                        "exact_file_match": True,
                        "roffset": _as_int(item.get("roffset")) or 0,
                    },
                )
            )
        return candidates

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Download a subtitle URL returned by Xunlei."""

        url = candidate.download_ref.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise ProviderError("Xunlei candidate has no valid download URL")
        try:
            response = await self._client.get(url, headers={"Accept": "*/*"})
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Xunlei download failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 429:
            raise ProviderQuotaError("Xunlei download is rate limited")
        if response.status_code >= 400:
            raise ProviderError(
                f"Xunlei download returned HTTP {response.status_code}"
            )
        file_name = str(
            candidate.download_ref.get("file_name")
            or candidate.release_name
            or f"xunlei-{candidate.candidate_id}.srt"
        )
        return DownloadedAsset(content=response.content, file_name=file_name)


def _as_int(value: Any) -> int | None:
    """Convert an optional provider value to an integer."""

    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float:
    """Convert a provider score to a float."""

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
