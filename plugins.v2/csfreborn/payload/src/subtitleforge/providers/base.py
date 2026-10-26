"""Provider contracts used by SubtitleForge."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import DownloadedAsset, SearchRequest, SubtitleCandidate


class ProviderError(RuntimeError):
    """A recoverable provider failure."""


class ProviderNotConfigured(ProviderError):
    """Raised when a provider has no usable credentials."""


class SubtitleProvider(ABC):
    """Interface implemented by every subtitle source."""

    name: str

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Return whether this provider can be queried."""

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        """Return normalized candidates for one media request."""

    @abstractmethod
    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        """Download a candidate and return its bytes."""

    async def close(self) -> None:
        """Release provider resources."""
