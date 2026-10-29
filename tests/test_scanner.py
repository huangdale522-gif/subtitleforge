import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from subtitleforge.config import Settings
from subtitleforge.models import (
    DownloadedAsset,
    ManualDownloadRequest,
    ManualSearchRequest,
    MediaType,
    ScanMediaMetadata,
    ScanRequest,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from subtitleforge.providers.base import SubtitleProvider
from subtitleforge.scanner import LibraryScanner, ScanStore, parse_media_path
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


class ManualSelectionProvider(ScanProvider):
    def __init__(self) -> None:
        super().__init__()
        self.requested_video_paths: list[str] = []

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        self.search_calls += 1
        self.requested_video_paths.append(
            str(getattr(request, "video_path", "") or "")
        )
        return [
            SubtitleCandidate(
                provider=self.name,
                candidate_id="manual-test",
                title=request.title,
                release_name=f"{request.title}.manual.zh-cn.srt",
                language=SubtitleLanguage.SIMPLIFIED,
                format="srt",
                download_ref={"url": "https://private.example/subtitle"},
            )
        ]


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


def test_parse_movie_nfo_ignores_actor_thumb_and_uses_root_poster(
    tmp_path: Path,
) -> None:
    root = tmp_path / "电影" / "外语电影"
    video = root / "Movie (2024)" / "Movie.2024.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    (video.parent / "poster.jpg").write_bytes(b"correct-poster")
    (video.parent / "actor.jpg").write_bytes(b"actor-headshot")
    (video.parent / "movie.nfo").write_text(
        (
            "<movie>"
            "<poster>poster.jpg</poster>"
            "<actor><name>Actor</name><thumb>actor.jpg</thumb></actor>"
            "</movie>"
        ),
        encoding="utf-8",
    )

    media = parse_media_path(video, root)

    assert media.poster_url == str(video.parent / "poster.jpg")


def test_parse_tv_nfo_never_uses_nested_actor_thumb_as_poster(
    tmp_path: Path,
) -> None:
    root = tmp_path / "电视剧"
    video = (
        root
        / "Some Show (2024)"
        / "Season 1"
        / "Some.Show.S01E01.mkv"
    )
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    (video.parent.parent / "tvshow.nfo").write_text(
        (
            "<tvshow>"
            "<actor><name>Actor</name><thumb>actor.jpg</thumb></actor>"
            "</tvshow>"
        ),
        encoding="utf-8",
    )

    media = parse_media_path(video, root)

    assert media.poster_url is None


def test_parse_path_reads_anime_metadata_from_nfo(tmp_path: Path) -> None:
    root = tmp_path / "动漫"
    video = (
        root
        / "某部动画 (2024) {tmdbid=123}"
        / "Season 1"
        / "某部动画 S01E01.mkv"
    )
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    (video.parent.parent / "tvshow.nfo").write_text(
        (
            "<tvshow>"
            "<genre>Animation</genre>"
            "<origin_country>CN</origin_country>"
            "<original_language>zh</original_language>"
            "</tvshow>"
        ),
        encoding="utf-8",
    )

    media = parse_media_path(video, root)

    assert media.genres == ["Animation"]
    assert media.origin_countries == ["CN"]
    assert media.original_language == "zh"


@pytest.mark.asyncio
async def test_scan_excludes_domestic_anime_but_keeps_foreign_anime(
    tmp_path: Path,
) -> None:
    root = tmp_path / "动漫"
    domestic = (
        root
        / "国产动画 (2024) {tmdbid=1}"
        / "Season 1"
        / "国产动画 S01E01.mkv"
    )
    foreign = (
        root
        / "Foreign Anime (2024) {tmdbid=2}"
        / "Season 1"
        / "Foreign.Anime.S01E01.mkv"
    )
    path_only = root / "国漫" / "无元数据 (2024)" / "无元数据.mkv"
    for video in (domestic, foreign, path_only):
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")
    (domestic.parent.parent / "tvshow.nfo").write_text(
        "<tvshow><genre>动画</genre><origin_country>CN</origin_country></tvshow>",
        encoding="utf-8",
    )
    (foreign.parent.parent / "tvshow.nfo").write_text(
        (
            "<tvshow><genre>Animation</genre>"
            "<origin_country>JP</origin_country>"
            "<original_language>ja</original_language></tvshow>"
        ),
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
                exclude_domestic_anime=True,
                domestic_anime_regions=["CN", "TW", "HK"],
                languages=[SubtitleLanguage.SIMPLIFIED],
                dry_run=True,
            )
        )
    finally:
        await service.close()

    assert result.discovered == 3
    assert result.checked == 1
    assert result.excluded_domestic_anime == 2
    assert result.items[0].title == "Foreign Anime"
    assert provider.search_calls == 0


@pytest.mark.asyncio
async def test_scan_uses_moviepilot_metadata_for_domestic_anime_and_posters(
    tmp_path: Path,
) -> None:
    root = tmp_path / "动漫"
    domestic = (
        root
        / "牧神记 (2024) {tmdbid=236534}"
        / "Season 1"
        / "牧神记 S01E08 2160p.OurTV.mp4"
    )
    foreign = (
        root
        / "葬送的芙莉莲 (2023) {tmdbid=209867}"
        / "Season 1"
        / "Frieren.S01E08.mkv"
    )
    for video in (domestic, foreign):
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")

    service = make_service(tmp_path)
    provider = NoResultProvider()
    service.providers["custom"] = provider
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        result = await scanner.scan(
            ScanRequest(
                paths=[str(root)],
                exclude_domestic_anime=True,
                domestic_anime_regions=["CN", "TW", "HK"],
                languages=[SubtitleLanguage.SIMPLIFIED],
                dry_run=False,
                media_metadata=[
                    ScanMediaMetadata(
                        tmdb_id=236534,
                        media_type=MediaType.TV,
                        title="牧神记",
                        year=2024,
                        genres=["16"],
                        origin_countries=["CN"],
                        original_language="zh",
                        poster_url="https://image.tmdb.org/t/p/w500/mushenji.jpg",
                    ),
                    ScanMediaMetadata(
                        media_type=MediaType.TV,
                        title="葬送的芙莉莲",
                        original_title="Sousou no Frieren",
                        year=2023,
                        genres=["16"],
                        origin_countries=["JP"],
                        original_language="ja",
                        poster_url="https://image.tmdb.org/t/p/w500/frieren.jpg",
                    ),
                ],
            )
        )
        queue = scanner.queue_view()
    finally:
        await service.close()

    assert result.discovered == 2
    assert result.metadata_applied == 2
    assert result.metadata_animation == 2
    assert result.excluded_domestic_anime == 1
    assert result.checked == 1
    assert provider.search_calls >= 1
    assert len(queue.groups) == 1
    assert queue.groups[0].title == "葬送的芙莉莲"
    assert queue.groups[0].poster == (
        "https://image.tmdb.org/t/p/w500/frieren.jpg"
    )


@pytest.mark.asyncio
async def test_scanner_status_reports_live_quota_and_queue(tmp_path: Path) -> None:
    video = tmp_path / "Movie (2024)" / "Movie.2024.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    service = make_service(tmp_path)
    store = ScanStore(service.settings.data_dir)
    store.record_downloads(3)
    store.upsert_queue_item(
        video,
        {
            "video_path": str(video),
            "title": "Movie",
            "media_type": "movie",
            "languages": ["zh-Hans"],
        },
    )
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        status = scanner.status(daily_download_limit=10)
    finally:
        await service.close()

    assert status["quota"] == {"used": 3, "limit": 10, "remaining": 7}
    assert status["queue"] == {"total": 1, "due": 1, "deferred": 0}


@pytest.mark.asyncio
async def test_queue_view_groups_tv_by_season_and_adds_local_poster(
    tmp_path: Path,
) -> None:
    show_root = tmp_path / "动漫" / "Some Show (2024) {tmdbid=42}"
    season_one = show_root / "Season 1"
    season_two = show_root / "Season 2"
    movie_root = tmp_path / "电影" / "Movie (2023)"
    season_one.mkdir(parents=True)
    season_two.mkdir(parents=True)
    movie_root.mkdir(parents=True)
    (show_root / "poster.jpg").write_bytes(b"fake-poster")
    videos = [
        season_one / "Some.Show.S01E01.mkv",
        season_one / "Some.Show.S01E02.mkv",
        season_two / "Some.Show.S02E01.mkv",
        movie_root / "Movie.2023.mkv",
    ]
    for video in videos:
        video.write_bytes(b"video")

    service = make_service(tmp_path)
    store = ScanStore(service.settings.data_dir)
    for video in videos[:3]:
        store.upsert_queue_item(
            video,
            {
                "video_path": str(video),
                "title": "Some Show",
                "year": 2024,
                "tmdb_id": 42,
                "media_type": "tv",
                "season": 1 if "S01" in video.name else 2,
                "episode": int(video.stem.rsplit("E", 1)[1]),
                "languages": ["zh-Hans"],
            },
        )
    store.upsert_queue_item(
        videos[3],
        {
            "video_path": str(videos[3]),
            "title": "Movie",
            "year": 2023,
            "media_type": "movie",
            "languages": ["zh-Hans"],
        },
    )
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        view = scanner.queue_view()
    finally:
        await service.close()

    assert view.total == 4
    assert len(view.groups) == 3
    tv_groups = [
        group for group in view.groups if group.media_type is MediaType.TV
    ]
    assert [group.season for group in tv_groups] == [1, 2]
    assert tv_groups[0].category == "anime"
    assert tv_groups[0].item_count == 2
    assert len(tv_groups[0].items) == 2
    assert tv_groups[0].poster is not None
    assert tv_groups[0].poster.startswith("data:image/jpeg;base64,")
    movie_group = next(
        group for group in view.groups if group.media_type is MediaType.MOVIE
    )
    assert movie_group.title == "Movie"
    assert movie_group.item_count == 1


@pytest.mark.asyncio
async def test_queue_view_ignores_legacy_actor_poster(
    tmp_path: Path,
) -> None:
    video = tmp_path / "电影" / "Movie (2024)" / "Movie.2024.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    (video.parent / "poster.jpg").write_bytes(b"correct-poster")
    (video.parent / "actor.jpg").write_bytes(b"actor-headshot")

    service = make_service(tmp_path)
    store = ScanStore(service.settings.data_dir)
    store.upsert_queue_item(
        video,
        {
            "video_path": str(video),
            "title": "Movie",
            "year": 2024,
            "media_type": "movie",
            "poster_url": str(video.parent / "actor.jpg"),
            "languages": ["zh-Hans"],
        },
    )
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        view = scanner.queue_view()
    finally:
        await service.close()

    poster = view.groups[0].poster or ""
    encoded = poster.split(",", 1)[1]
    assert base64.b64decode(encoded) == b"correct-poster"


@pytest.mark.asyncio
async def test_manual_search_keeps_video_path_and_downloads_selected_candidate(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Show (2024)" / "Season 1" / "Show.S01E01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    service = make_service(tmp_path)
    provider = ManualSelectionProvider()
    service.providers["custom"] = provider
    store = ScanStore(service.settings.data_dir)
    store.upsert_queue_item(
        video,
        {
            "video_path": str(video),
            "title": "Show",
            "year": 2024,
            "media_type": "tv",
            "season": 1,
            "episode": 1,
            "languages": ["zh-Hans"],
        },
    )
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        search = await scanner.manual_search(
            ManualSearchRequest(video_path=str(video))
        )
        public_payload = search.model_dump(mode="json")
        assert "download_ref" not in json.dumps(public_payload)
        assert provider.requested_video_paths == [str(video)]
        assert search.candidates

        result = await scanner.manual_download(
            ManualDownloadRequest(
                search_id=search.search_id,
                selection_id=search.candidates[0].selection_id,
                language=SubtitleLanguage.SIMPLIFIED,
                daily_download_limit=10,
            )
        )
        status = scanner.status(daily_download_limit=10)
    finally:
        await service.close()

    assert result.status == "completed"
    assert result.downloaded
    assert scanner.queue_status()["total"] == 0
    assert status["quota"]["used"] == 1


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


@pytest.mark.asyncio
async def test_bluray_segments_are_skipped_and_removed_from_queue(
    tmp_path: Path,
) -> None:
    root = tmp_path / "电影" / "外语电影"
    normal = root / "Movie (2024)" / "Movie.2024.mkv"
    segment = root / "Movie (2024)" / "BDMV" / "STREAM" / "00001.m2ts"
    normal.parent.mkdir(parents=True)
    segment.parent.mkdir(parents=True)
    normal.write_bytes(b"video")
    segment.write_bytes(b"video")

    service = make_service(tmp_path)
    service.providers["custom"] = ScanProvider()
    store = ScanStore(service.settings.data_dir)
    store.upsert_queue_item(
        segment,
        {
            "video_path": str(segment),
            "title": "STREAM",
            "media_type": "movie",
            "languages": ["zh-Hans"],
        },
    )
    scanner = LibraryScanner(service, service.settings.data_dir)
    try:
        result = await scanner.scan(
            ScanRequest(
                paths=[str(root)],
                languages=[SubtitleLanguage.SIMPLIFIED],
                max_files=1,
                daily_download_limit=1,
                dry_run=False,
            )
        )
    finally:
        await service.close()

    assert result.discovered == 1
    assert result.checked == 1
    assert segment not in {Path(item.video_path) for item in result.items}
    assert scanner.queue_status()["total"] == 0
