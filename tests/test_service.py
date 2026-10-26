from pathlib import Path

import httpx
import pytest

from subtitleforge.config import Settings
from subtitleforge.models import (
    DownloadedAsset,
    ProcessRequest,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from subtitleforge.providers.base import ProviderQuotaError, SubtitleProvider
from subtitleforge.service import SubtitleService


class FakeProvider(SubtitleProvider):
    name = "custom"

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        return [
            SubtitleCandidate(
                provider=self.name,
                candidate_id="fake",
                title=request.title,
                release_name=f"{request.title}.2024.srt",
                language=SubtitleLanguage.SIMPLIFIED,
                format="srt",
                year=request.year,
            )
        ]

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        return DownloadedAsset(
            content="1\n00:00:00,000 --> 00:00:01,000\n简体中文字幕\n".encode(),
            file_name="Movie.srt",
        )


class EmptyProvider(SubtitleProvider):
    def __init__(self, name: str) -> None:
        self.name = name
        self.search_calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        return []

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        raise AssertionError("empty provider cannot download")


class ResultProvider(EmptyProvider):
    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        return [
            SubtitleCandidate(
                provider=self.name,
                candidate_id=f"{self.name}-result",
                title=request.title,
                language=SubtitleLanguage.SIMPLIFIED,
            )
        ]


class QuotaProvider(EmptyProvider):
    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        raise ProviderQuotaError("quota exhausted")


class RejectingDownloadProvider(ResultProvider):
    def __init__(self) -> None:
        super().__init__("custom")
        self.download_calls = 0

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        return [
            SubtitleCandidate(
                provider=self.name,
                candidate_id=f"candidate-{index}",
                title=request.title,
                language=SubtitleLanguage.SIMPLIFIED,
            )
            for index in range(10)
        ]

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        self.download_calls += 1
        raise ValueError("unusable candidate")


@pytest.mark.asyncio
async def test_process_installs_missing_subtitle(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    settings = Settings(
        media_roots=str(tmp_path), data_dir=tmp_path / "data", provider_order="custom"
    )
    service = SubtitleService(settings, client=httpx.AsyncClient())
    service.providers["custom"] = FakeProvider()
    try:
        result = await service.process(
            ProcessRequest(
                video_path=str(video),
                title="Movie",
                year=2024,
                languages=[SubtitleLanguage.SIMPLIFIED],
            )
        )
    finally:
        await service.close()
    assert result.status == "completed"
    assert (tmp_path / "Movie.chi.zh-cn.srt").exists()


@pytest.mark.asyncio
async def test_process_rejects_path_outside_media_root(tmp_path: Path) -> None:
    settings = Settings(media_roots=str(tmp_path / "media"), data_dir=tmp_path / "data")
    service = SubtitleService(settings, client=httpx.AsyncClient())
    try:
        with pytest.raises(ValueError, match="outside"):
            await service.process(
                ProcessRequest(
                    video_path=str(tmp_path / "other" / "Movie.mkv"),
                    title="Movie",
                )
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_provider_failover_stops_after_first_match(tmp_path: Path) -> None:
    settings = Settings(
        media_roots=str(tmp_path), data_dir=tmp_path / "data", provider_order="custom"
    )
    service = SubtitleService(settings, client=httpx.AsyncClient())
    empty = EmptyProvider("opensubtitles")
    result_provider = ResultProvider("assrt")
    unused = EmptyProvider("custom")
    service.providers.update(
        {
            "opensubtitles": empty,
            "assrt": result_provider,
            "custom": unused,
        }
    )
    try:
        candidates, _ = await service.search(
            SearchRequest(title="Movie"),
            provider_order=["opensubtitles", "assrt", "custom"],
            provider_failover=True,
        )
    finally:
        await service.close()
    assert candidates
    assert empty.search_calls == 1
    assert result_provider.search_calls == 1
    assert unused.search_calls == 0


@pytest.mark.asyncio
async def test_provider_quota_error_activates_cooldown(tmp_path: Path) -> None:
    settings = Settings(
        media_roots=str(tmp_path),
        data_dir=tmp_path / "data",
        provider_cooldown_hours=24,
    )
    service = SubtitleService(settings, client=httpx.AsyncClient())
    quota_provider = QuotaProvider("assrt")
    fallback = ResultProvider("opensubtitles")
    service.providers.update(
        {"assrt": quota_provider, "opensubtitles": fallback}
    )
    order = ["assrt", "opensubtitles"]
    try:
        first, first_errors = await service.search(
            SearchRequest(title="Movie"),
            provider_order=order,
            provider_failover=True,
        )
        second, second_errors = await service.search(
            SearchRequest(title="Movie"),
            provider_order=order,
            provider_failover=True,
        )
    finally:
        await service.close()
    assert first and second
    assert first_errors and not second_errors
    assert quota_provider.search_calls == 1
    assert fallback.search_calls == 2


@pytest.mark.asyncio
async def test_provider_quota_cooldown_survives_restart(tmp_path: Path) -> None:
    settings = Settings(
        media_roots=str(tmp_path),
        data_dir=tmp_path / "data",
        provider_cooldown_hours=24,
    )
    first_service = SubtitleService(settings, client=httpx.AsyncClient())
    quota_provider = QuotaProvider("assrt")
    first_service.providers["assrt"] = quota_provider
    try:
        await first_service.search(
            SearchRequest(title="Movie"), provider_order=["assrt"]
        )
    finally:
        await first_service.close()

    second_service = SubtitleService(settings, client=httpx.AsyncClient())
    same_provider = QuotaProvider("assrt")
    second_service.providers["assrt"] = same_provider
    try:
        await second_service.search(
            SearchRequest(title="Another Movie"), provider_order=["assrt"]
        )
    finally:
        await second_service.close()
    assert quota_provider.search_calls == 1
    assert same_provider.search_calls == 0


@pytest.mark.asyncio
async def test_download_attempts_are_capped_per_file(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    settings = Settings(
        media_roots=str(tmp_path),
        data_dir=tmp_path / "data",
        provider_order="custom",
        max_download_attempts_per_file=3,
    )
    service = SubtitleService(settings, client=httpx.AsyncClient())
    provider = RejectingDownloadProvider()
    service.providers["custom"] = provider
    try:
        result = await service.process(
            ProcessRequest(
                video_path=str(video),
                title="Movie",
                languages=[SubtitleLanguage.SIMPLIFIED],
            )
        )
    finally:
        await service.close()
    assert result.status == "not_found"
    assert provider.download_calls == 3
