import asyncio
from pathlib import Path

import httpx
import pytest

from subtitleforge.config import Settings
from subtitleforge.models import (
    DownloadedAsset,
    MediaType,
    ScanRequest,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from subtitleforge.providers.base import SubtitleProvider
from subtitleforge.scanner import LibraryScanner, parse_media_path
from subtitleforge.service import SubtitleService


class ScanProvider(SubtitleProvider):
    name = "custom"

    def __init__(self) -> None:
        self.search_calls = 0
        self.download_calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        return [
            SubtitleCandidate(
                provider=self.name,
                candidate_id="scan-test",
                title=request.title,
                release_name=f"{request.title}.zh-cn.srt",
                language=SubtitleLanguage.SIMPLIFIED,
                format="srt",
            )
        ]

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        self.download_calls += 1
        return DownloadedAsset(
            content="1\n00:00:00,000 --> 00:00:01,000\n简体中文字幕\n".encode(),
            file_name="subtitle.zh-cn.srt",
        )


class NoResultProvider(ScanProvider):
    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        return []


class SlowScanProvider(ScanProvider):
    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        await asyncio.sleep(0.02)
        return await super().search(request)


def make_service(tmp_path: Path) -> SubtitleService:
    settings = Settings(
        media_roots=str(tmp_path),
        data_dir=tmp_path / "data",
        provider_order="custom",
    )
    return SubtitleService(settings, client=httpx.AsyncClient())


def test_parse_movie_path_uses_tmdb_and_year(tmp_path: Path) -> None:
    root = tmp_path / "电影" / "外语电影"
    video = root / "The Matrix (1999) {tmdbid=603}" / "The.Matrix.1999.mkv"
    media = parse_media_path(video, root)
    assert media.title == "The Matrix"
    assert media.year == 1999
    assert media.tmdb_id == 603
    assert media.media_type is MediaType.MOVIE


def test_parse_tv_path_extracts_season_and_episode(tmp_path: Path) -> None:
    root = tmp_path / "电视剧" / "欧美剧"
    video = (
        root
        / "Some Show (2024) {tmdbid=123}"
        / "Season 2"
        / "Some.Show.S02E03.1080p.mkv"
    )
    media = parse_media_path(video, root)
    assert media.title == "Some Show"
    assert media.year == 2024
    assert media.tmdb_id == 123
    assert media.media_type is MediaType.TV
    assert media.season == 2
    assert media.episode == 3


def test_parse_path_reads_imdb_from_kodi_nfo(tmp_path: Path) -> None:
    root = tmp_path / "电影" / "外语电影"
    video = root / "中文译名 (2024)" / "Movie.2024.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    (video.parent / "movie.nfo").write_text(
        "<movie><imdbid>tt1234567</imdbid><tmdbid>7654321</tmdbid></movie>",
        encoding="utf-8",
    )
    media = parse_media_path(video, root)
    assert media.imdb_id == "tt1234567"
    assert media.tmdb_id == 7654321


@pytest.mark.asyncio
async def test_library_scan_downloads_only_missing_subtitles(tmp_path: Path) -> None:
    root = tmp_path / "电影" / "外语电影"
    missing = root / "Movie (2024) {tmdbid=1}" / "Movie.2024.mkv"
    existing = root / "Existing (2023) {tmdbid=2}" / "Existing.2023.mkv"
    missing.parent.mkdir(parents=True)
    existing.parent.mkdir(parents=True)
    missing.write_bytes(b"video")
    existing.write_bytes(b"video")
    (existing.parent / "Existing.2023.zh-cn.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n简体中文字幕\n",
        encoding="utf-8",
    )
    service = make_service(tmp_path)
    provider = ScanProvider()
    service.providers["custom"] = provider
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        result = await scanner.scan(
            ScanRequest(
                paths=[str(root)],
                languages=[SubtitleLanguage.SIMPLIFIED],
                max_files=10,
                dry_run=False,
            )
        )
    finally:
        await service.close()
    assert result.discovered == 2
    assert result.checked == 2
    assert result.existing == 1
    assert result.queued == 1
    assert result.downloaded == 1
    assert result.queue_remaining == 0
    assert provider.search_calls == 1
    assert provider.download_calls == 1
    assert (missing.parent / "Movie.2024.chi.zh-cn.srt").exists()
    assert not any(
        item.status == "completed" for item in result.items if "Existing" in item.title
    )


@pytest.mark.asyncio
async def test_full_scan_queues_all_then_downloads_under_daily_limit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "电影" / "外语电影"
    for index in range(1, 13):
        video = root / f"Movie {index} (2024)" / f"Movie.{index}.2024.mkv"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")

    service = make_service(tmp_path)
    provider = ScanProvider()
    service.providers["custom"] = provider
    try:
        first_scanner = LibraryScanner(service, service.settings.data_dir)
        first = await first_scanner.scan(
            ScanRequest(
                paths=[str(root)],
                languages=[SubtitleLanguage.SIMPLIFIED],
                max_files=2,
                daily_download_limit=10,
                dry_run=False,
            )
        )
        assert first.discovered == 12
        assert first.checked == 12
        assert first.existing == 0
        assert first.queued == 12
        assert first.downloaded == 2
        assert first.download_limit == 2
        assert first.downloads_today == 2
        assert first.queue_remaining == 10
        assert provider.search_calls == 2

        rebuilt_scanner = LibraryScanner(service, service.settings.data_dir)
        second = await rebuilt_scanner.scan(
            ScanRequest(
                paths=[str(root)],
                languages=[SubtitleLanguage.SIMPLIFIED],
                max_files=2,
                daily_download_limit=2,
                dry_run=False,
            )
        )
    finally:
        await service.close()

    assert second.checked == 12
    assert second.existing == 2
    assert second.queued == 10
    assert second.downloaded == 0
    assert second.download_limit == 0
    assert second.downloads_today == 2
    assert second.queue_remaining == 10
    assert provider.search_calls == 2


@pytest.mark.asyncio
async def test_library_scan_honors_exclusions_and_dry_run(tmp_path: Path) -> None:
    foreign = tmp_path / "电影" / "外语电影" / "Movie (2024)" / "Movie.mkv"
    chinese = (
        tmp_path / "电影" / "华语电影" / "本地电影 (2024)" / "本地电影.mkv"
    )
    foreign.parent.mkdir(parents=True)
    chinese.parent.mkdir(parents=True)
    foreign.write_bytes(b"video")
    chinese.write_bytes(b"video")
    service = make_service(tmp_path)
    provider = ScanProvider()
    service.providers["custom"] = provider
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        result = await scanner.scan(
            ScanRequest(
                paths=[str(tmp_path / "电影")],
                exclude_paths=[str(tmp_path / "电影" / "华语电影")],
                languages=[SubtitleLanguage.SIMPLIFIED],
                dry_run=True,
            )
        )
    finally:
        await service.close()
    assert result.discovered == 1
    assert result.downloaded == 0
    assert result.items[0].status == "dry_run"
    assert provider.search_calls == 0
    assert provider.download_calls == 0
    assert scanner.queue_status()["total"] == 0
    assert not (foreign.parent / "Movie.chi.zh-cn.srt").exists()


@pytest.mark.asyncio
async def test_failed_queue_item_waits_for_retry_cooldown(tmp_path: Path) -> None:
    root = tmp_path / "电影" / "外语电影"
    video = root / "Movie (2024)" / "Movie.2024.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    service = make_service(tmp_path)
    provider = NoResultProvider()
    service.providers["custom"] = provider
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        first = await scanner.scan(
            ScanRequest(
                paths=[str(root)],
                languages=[SubtitleLanguage.SIMPLIFIED],
                max_files=1,
                daily_download_limit=5,
                dry_run=False,
            )
        )
        queue_after_first = scanner.queue_status()
        second = await scanner.scan(
            ScanRequest(
                paths=[str(root)],
                languages=[SubtitleLanguage.SIMPLIFIED],
                max_files=1,
                daily_download_limit=5,
                dry_run=False,
            )
        )
    finally:
        await service.close()

    assert first.downloaded == 0
    assert first.queue_remaining == 1
    assert queue_after_first == {"total": 1, "due": 0, "deferred": 1}
    assert second.downloaded == 0
    assert second.queue_remaining == 1
    assert provider.search_calls == 1


@pytest.mark.asyncio
async def test_concurrent_scans_are_serialized_by_daily_quota(tmp_path: Path) -> None:
    root = tmp_path / "电影" / "外语电影"
    video = root / "Movie (2024)" / "Movie.2024.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    service = make_service(tmp_path)
    provider = SlowScanProvider()
    service.providers["custom"] = provider
    scanner = LibraryScanner(service, service.settings.data_dir)
    request = ScanRequest(
        paths=[str(root)],
        languages=[SubtitleLanguage.SIMPLIFIED],
        max_files=1,
        daily_download_limit=1,
        dry_run=False,
    )
    try:
        first, second = await asyncio.gather(
            scanner.scan(request),
            scanner.scan(request),
        )
    finally:
        await service.close()

    assert first.downloaded + second.downloaded == 1
    assert max(first.downloads_today, second.downloads_today) == 1
    assert provider.search_calls == 1
