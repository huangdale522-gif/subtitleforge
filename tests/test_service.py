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
from subtitleforge.providers.base import SubtitleProvider
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
