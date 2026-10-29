from pathlib import Path

from fastapi.testclient import TestClient

from subtitleforge.api import create_app
from subtitleforge.config import Settings
from subtitleforge.models import (
    DownloadedAsset,
    SearchRequest,
    SubtitleCandidate,
    SubtitleLanguage,
)
from subtitleforge.providers.base import SubtitleProvider
from subtitleforge.scanner import ScanStore


class ApiManualProvider(SubtitleProvider):
    name = "custom"

    @property
    def configured(self) -> bool:
        return True

    async def search(self, request: SearchRequest) -> list[SubtitleCandidate]:
        return [
            SubtitleCandidate(
                provider=self.name,
                candidate_id="api-manual",
                title=request.title,
                release_name="Manual.zh-cn.srt",
                language=SubtitleLanguage.SIMPLIFIED,
                format="srt",
                download_ref={"url": "https://private.example/secret"},
            )
        ]

    async def download(self, candidate: SubtitleCandidate) -> DownloadedAsset:
        return DownloadedAsset(
            content="1\n00:00:00,000 --> 00:00:01,000\n简体中文字幕\n".encode(),
            file_name="Manual.srt",
        )


def test_healthz_is_public(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            media_roots=str(tmp_path), data_dir=tmp_path / "data", api_token="secret"
        )
    )
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == "0.4.1"


def test_runtime_status_reports_quota_queue_and_providers(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            media_roots=str(tmp_path),
            data_dir=tmp_path / "data",
            api_token="secret",
            provider_order="xunlei,assrt",
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/status?daily_download_limit=7",
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == "0.4.1"
    assert payload["quota"] == {"used": 0, "limit": 7, "remaining": 7}
    assert payload["queue"] == {"total": 0, "due": 0, "deferred": 0}
    assert payload["providers"]["order"] == ["xunlei", "assrt"]
    assert payload["providers"]["temporary_cooldown_minutes"] == 15
    assert [item["name"] for item in payload["providers"]["items"]] == [
        "xunlei",
        "assrt",
    ]


def test_process_requires_token(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            media_roots=str(tmp_path), data_dir=tmp_path / "data", api_token="secret"
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/process", json={"video_path": "/nope", "title": "x"}
        )
    assert response.status_code == 401


def test_queue_and_manual_search_are_grouped_and_hide_provider_refs(
    tmp_path: Path,
) -> None:
    video = tmp_path / "Show (2024)" / "Season 1" / "Show.S01E01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    settings = Settings(
        media_roots=str(tmp_path),
        data_dir=tmp_path / "data",
        api_token="secret",
        provider_order="custom",
    )
    ScanStore(settings.data_dir).upsert_queue_item(
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
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.service.providers["custom"] = ApiManualProvider()
        queue_response = client.get(
            "/v1/queue",
            headers={"Authorization": "Bearer secret"},
        )
        search_response = client.post(
            "/v1/manual/search",
            headers={"Authorization": "Bearer secret"},
            json={"video_path": str(video)},
        )

    assert queue_response.status_code == 200
    queue_payload = queue_response.json()
    assert queue_payload["groups"][0]["season"] == 1
    assert queue_payload["groups"][0]["items"][0]["video_path"] == str(video)
    assert search_response.status_code == 200
    search_payload = search_response.json()
    assert search_payload["candidates"]
    assert "download_ref" not in str(search_payload)
