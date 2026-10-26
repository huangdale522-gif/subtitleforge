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
