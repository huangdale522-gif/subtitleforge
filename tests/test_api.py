from pathlib import Path

from fastapi.testclient import TestClient

from subtitleforge.api import create_app
from subtitleforge.config import Settings


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
    assert response.json()["version"] == "0.3.2"


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
    assert payload["version"] == "0.3.2"
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
