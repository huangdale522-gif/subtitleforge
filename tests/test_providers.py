from __future__ import annotations

import httpx
import pytest

from subtitleforge.models import ProcessRequest, SearchRequest, SubtitleLanguage
from subtitleforge.providers.assrt import AssrtProvider
from subtitleforge.providers.base import ProviderUnavailableError
from subtitleforge.providers.custom import CustomHttpProvider
from subtitleforge.providers.opensubtitles import OpenSubtitlesProvider
from subtitleforge.providers.shooter import ShooterProvider
from subtitleforge.providers.subtitle_best import SubtitleBestProvider
from subtitleforge.providers.xunlei import XunleiProvider


@pytest.mark.asyncio
async def test_opensubtitles_falls_back_to_title_and_downloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"token": "jwt"})
        if request.url.path.endswith("/subtitles"):
            if "imdb_id" in request.url.params:
                return httpx.Response(200, json={"data": [], "total_pages": 1})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "10",
                            "attributes": {
                                "release": "Movie.2024.1080p",
                                "language": "zh-cn",
                                "download_count": 20,
                                "feature_details": {"year": 2024},
                                "files": [
                                    {
                                        "file_id": 99,
                                        "file_name": "Movie.2024.1080p.srt",
                                    }
                                ],
                            },
                        }
                    ],
                    "total_pages": 1,
                },
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(
                200,
                json={
                    "link": "https://cdn.example/subtitle.srt",
                    "file_name": "Movie.srt",
                },
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"subtitle")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenSubtitlesProvider(client, "key", "user", "pass")
    request = SearchRequest(
        title="Movie",
        english_title="Movie",
        imdb_id="tt1234567",
        year=2024,
        languages=[SubtitleLanguage.SIMPLIFIED],
    )
    candidates = await provider.search(request)
    assert len(candidates) == 1
    assert candidates[0].language is SubtitleLanguage.SIMPLIFIED
    assert any("query" in item.url.params for item in requests)
    asset = await provider.download(candidates[0])
    assert asset.content == b"subtitle"
    assert any(item.headers.get("Authorization") == "Bearer jwt" for item in requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_assrt_normalizes_search_results() -> None:
    search_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        assert request.headers["Authorization"] == "Bearer token"
        assert request.url.path.endswith("/sub/search")
        search_calls += 1
        return httpx.Response(
            200,
            json={
                "status": 0,
                "sub": {
                    "subs": [
                        {
                            "id": 12,
                            "native_name": "Example.Show.S02E04.1080p",
                            "videoname": "Example.Show.S02E04.1080p",
                            "subtype": "srt",
                            "lang": {"desc": "简体", "langlist": {"langchinese": 1}},
                            "vote_score": 5,
                        }
                    ]
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AssrtProvider(client, "token")
    candidates = await provider.search(
        SearchRequest(title="Example Show", english_title="Example Show")
    )
    assert len(candidates) == 1
    assert search_calls == 1
    assert candidates[0].season == 2
    assert candidates[0].episode == 4
    assert candidates[0].language is SubtitleLanguage.SIMPLIFIED
    await client.aclose()


@pytest.mark.asyncio
async def test_custom_provider_does_not_forward_token_to_other_host() -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        if request.url.host == "manifest.example":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "1",
                            "title": "Movie",
                            "language": "zh-Hans",
                            "format": "srt",
                            "url": "https://cdn.example/movie.srt",
                        }
                    ]
                },
            )
        return httpx.Response(200, content=b"subtitle")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CustomHttpProvider(
        client,
        "https://manifest.example/search",
        token="private-token",
    )
    candidates = await provider.search(SearchRequest(title="Movie"))
    await provider.download(candidates[0])
    assert seen_headers[0]["Authorization"] == "Bearer private-token"
    assert "Authorization" not in seen_headers[1]
    await client.aclose()


@pytest.mark.asyncio
async def test_subtitle_best_searches_and_downloads_episode() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.subtitle.best":
            assert request.headers["Authorization"] == "Bearer client-token"
        if request.url.path.endswith("/search-tv-eps"):
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "subtitles": [
                        {
                            "sub_sha256": "abc123",
                            "title": "Example.S01E02.zh-Hans.srt",
                            "language": 1,
                            "ext": "srt",
                            "is_movie": False,
                            "season": 1,
                            "episode": 2,
                            "token": "item-token",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/get-dl-url"):
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "download_link": "https://cdn.example/episode.srt",
                },
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"subtitle")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SubtitleBestProvider(client, "account-key", "client-token")
    candidates = await provider.search(
        SearchRequest(
            title="Example",
            imdb_id="tt1234567",
            media_type="tv",
            season=1,
            episode=2,
        )
    )
    assert len(candidates) == 1
    assert candidates[0].language is SubtitleLanguage.SIMPLIFIED
    asset = await provider.download(candidates[0])
    assert asset.content == b"subtitle"
    assert not any(
        "Authorization" in request.headers
        for request in requests
        if request.url.host == "cdn.example"
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_subtitle_best_522_is_classified_as_temporary_outage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(522, text="origin timeout")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SubtitleBestProvider(client, "account-key", "client-token")
    with pytest.raises(ProviderUnavailableError, match="HTTP 522"):
        await provider.search(
            SearchRequest(
                title="Example",
                imdb_id="tt1234567",
                media_type="tv",
                season=1,
                episode=8,
            )
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_xunlei_matches_exact_file_and_downloads(tmp_path) -> None:
    video = tmp_path / "Movie.2024.mkv"
    video.write_bytes(b"x" * 0x10000)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "sub.xmp.sandai.net":
            return httpx.Response(
                200,
                json={
                    "sublist": [
                        {
                            "scid": "sub-1",
                            "sname": "Movie.2024.chs.ass",
                            "language": "简体&英文",
                            "rate": "5",
                            "surl": "http://cdn.example/movie.ass",
                            "svote": 120,
                            "roffset": 250,
                        }
                    ]
                },
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"subtitle")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = XunleiProvider(client)
    request = ProcessRequest(
        video_path=str(video),
        title="Movie",
        year=2024,
        languages=[SubtitleLanguage.SIMPLIFIED],
    )
    candidates = await provider.search(request)
    assert len(candidates) == 1
    assert candidates[0].language is SubtitleLanguage.SIMPLIFIED
    assert candidates[0].download_ref["exact_file_match"] is True
    asset = await provider.download(candidates[0])
    assert asset.content == b"subtitle"
    assert any(item.url.host == "sub.xmp.sandai.net" for item in requests)
    await client.aclose()


@pytest.mark.asyncio
async def test_shooter_matches_exact_file_and_downloads(tmp_path) -> None:
    video = tmp_path / "Movie.2024.mkv"
    video.write_bytes(b"x" * 0x10000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.shooter.cn":
            return httpx.Response(
                200,
                json=[
                    {
                        "desc": "Movie.2024.1080p",
                        "delay": 150,
                        "files": [
                            {
                                "ext": "srt",
                                "link": "https://cdn.example/movie.srt",
                            }
                        ],
                    }
                ],
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"subtitle")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ShooterProvider(client, enabled=True)
    request = ProcessRequest(
        video_path=str(video),
        title="Movie",
        year=2024,
        languages=[SubtitleLanguage.SIMPLIFIED],
    )
    candidates = await provider.search(request)
    assert len(candidates) == 1
    assert candidates[0].download_ref["exact_file_match"] is True
    asset = await provider.download(candidates[0])
    assert asset.content == b"subtitle"
    await client.aclose()
