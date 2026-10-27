from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.plugins.watchedcleaner as plugin_module
from app.plugins.watchedcleaner import WatchedCleaner
from app.plugins.watchedcleaner import adapters as adapter_module
from app.plugins.watchedcleaner.adapters import EmbyFamilyAdapter, _collect_candidates
from app.plugins.watchedcleaner.policy import (
    candidate_key,
    normalize_record,
    path_allowed,
    record_block_reason,
)


OLD = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()


def _plugin(monkeypatch, **overrides):
    plugin = object.__new__(WatchedCleaner)
    data = {}
    plugin.get_data = lambda key=None: data.get(key)
    plugin.save_data = lambda key, value: data.__setitem__(key, value)
    config = {
        "enabled": True,
        "dry_run": True,
        "allow_delete": False,
        "allowed_roots": "",
        "categories": ["movie", "tv", "anime"],
        "tv_mode": "episode",
        "watched_threshold": 90,
        "min_days_since_played": 7,
        "min_days_since_added": 7,
        "protect_favorites": True,
        **overrides,
    }
    plugin.init_plugin(config)
    return plugin, data


def _movie_record(**overrides):
    record = {
        "kind": "movie",
        "item_id": "movie-1",
        "title": "测试电影",
        "paths": ["/media/Movies/Test.mkv"],
        "played": True,
        "percentage": 100,
        "last_played": OLD,
        "added_at": OLD,
        "favorite": False,
        "genres": [],
    }
    record.update(overrides)
    return normalize_record(record)


def test_path_guard_rejects_prefix_collision_and_parent_escape():
    allowed, _ = path_allowed("/media2/Movie/file.mkv", ["/media"])
    assert allowed is False

    allowed, _ = path_allowed("/media/Movie/../Movie/file.mkv", ["/media"])
    assert allowed is True

    allowed, _ = path_allowed("/media/../etc/passwd", ["/media"])
    assert allowed is False

    allowed, _ = path_allowed("/media/Movie/file.mkv", ["/"])
    assert allowed is False


def test_series_mode_requires_every_episode_to_be_watched():
    episodes = [
        _movie_record(
            kind="episode",
            item_id="ep-1",
            series_id="show-1",
            title="第一集",
            series_title="测试剧",
            season=1,
            episode=1,
            paths=["/media/TV/Test/S01E01.mkv"],
        ),
        _movie_record(
            kind="episode",
            item_id="ep-2",
            series_id="show-1",
            title="第二集",
            series_title="测试剧",
            season=1,
            episode=2,
            paths=["/media/TV/Test/S01E02.mkv"],
            played=False,
            percentage=40,
        ),
    ]
    options = {
        "categories": ["tv"],
        "tv_mode": "series",
        "watched_threshold": 90,
        "min_days_since_played": 7,
        "min_days_since_added": 7,
        "protect_favorites": True,
        "allowed_roots": ["/media"],
        "anime_keywords": ["anime"],
        "max_candidates": 100,
    }

    result = _collect_candidates(
        records=episodes,
        options=options,
        service_name="Emby",
        service_type="emby",
        user_name="admin",
        user_id="user-1",
        library_name="电视剧",
        base_category="tv",
        scan_id="scan",
        limit=100,
    )

    assert result["candidates"] == []
    assert result["skipped"]["仍有 1 集未达到观看阈值"] == 1


def test_anime_library_is_classified_without_losing_delete_granularity():
    episode = _movie_record(
        kind="episode",
        item_id="ep-1",
        series_id="anime-1",
        title="第一话",
        series_title="测试番",
        season=1,
        episode=1,
        paths=["/media/Anime/Test/S01E01.mkv"],
    )
    options = {
        "categories": ["anime"],
        "tv_mode": "episode",
        "watched_threshold": 90,
        "min_days_since_played": 7,
        "min_days_since_added": 7,
        "protect_favorites": True,
        "allowed_roots": ["/media"],
        "anime_keywords": ["anime"],
        "max_candidates": 100,
    }

    result = _collect_candidates(
        records=[episode],
        options=options,
        service_name="Jellyfin",
        service_type="jellyfin",
        user_name="admin",
        user_id="user-1",
        library_name="Anime",
        base_category="tv",
        scan_id="scan",
        limit=100,
    )

    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["category"] == "anime"
    assert candidate["kind"] == "episode"


def test_recent_play_and_favorite_are_protected():
    recent = _movie_record(
        last_played=datetime.now(timezone.utc).isoformat(),
        favorite=True,
    )
    options = {
        "watched_threshold": 90,
        "min_days_since_played": 7,
        "min_days_since_added": 7,
        "protect_favorites": True,
        "allowed_roots": ["/media"],
    }

    assert record_block_reason(recent, options) == "收藏条目受保护"
    recent["favorite"] = False
    assert "播放过" in record_block_reason(recent, options)


def test_emby_parser_uses_user_state_and_file_path():
    service = SimpleNamespace(
        type="emby",
        config=SimpleNamespace(
            config={"host": "http://emby.local:8096", "apikey": "secret"}
        ),
        instance=None,
    )
    adapter = EmbyFamilyAdapter("客厅 Emby", service, "emby")
    record = adapter._record(
        {
            "Id": "episode-1",
            "Type": "Episode",
            "Name": "第一集",
            "SeriesId": "series-1",
            "SeriesName": "测试剧",
            "ParentIndexNumber": 1,
            "IndexNumber": 2,
            "Path": "/media/TV/Test/S01E02.mkv",
            "DateCreated": OLD,
            "RunTimeTicks": 1000,
            "UserData": {
                "Played": False,
                "PlaybackPositionTicks": 950,
                "IsFavorite": True,
                "LastPlayedDate": OLD,
            },
        }
    )

    assert record["kind"] == "episode"
    assert record["percentage"] == 95
    assert record["favorite"] is True
    assert record["paths"] == ["/media/TV/Test/S01E02.mkv"]


def test_emby_delete_uses_expected_endpoint_and_token(monkeypatch):
    service = SimpleNamespace(
        type="emby",
        config=SimpleNamespace(
            config={"host": "http://emby.local:8096", "apikey": "secret"}
        ),
        instance=None,
    )
    adapter = EmbyFamilyAdapter("客厅 Emby", service, "emby")
    captured = {}

    class FakeResponse:
        status_code = 204

        @staticmethod
        def close():
            return None

    class FakeRequestUtils:
        def __init__(self, headers=None):
            captured["headers"] = headers

        def delete_res(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(adapter_module, "RequestUtils", FakeRequestUtils)

    message = adapter.delete({"item_id": "item-1"})

    assert "接受删除" in message
    assert captured["url"] == "http://emby.local:8096/emby/Items/item-1"
    assert captured["params"] == {"api_key": "secret"}
    assert captured["headers"]["X-Emby-Token"] == "secret"


def test_delete_api_requires_confirmation_even_in_dry_run(monkeypatch):
    plugin, _ = _plugin(monkeypatch)

    response = plugin.api_delete(
        {
            "candidate_keys": [candidate_key("Emby", "movie", "movie-1")],
            "confirmation": "",
        }
    )

    assert response.success is False
    assert "confirmation" in response.message


def test_scan_only_stores_candidates_and_never_deletes(monkeypatch):
    plugin, _ = _plugin(monkeypatch, dry_run=True)
    plugin._discover_services = lambda: ({"Emby": object()}, "")
    key = candidate_key("Emby", "movie", "movie-1")

    class FakeAdapter:
        service_type = "emby"

        def scan(self, _options, scan_id):
            return {
                "server": "Emby",
                "server_type": "emby",
                "user": "admin",
                "user_id": "user-1",
                "scanned": 1,
                "candidates": [
                    {
                        "key": key,
                        "scan_id": scan_id,
                        "server": "Emby",
                        "server_type": "emby",
                        "user": "admin",
                        "user_id": "user-1",
                        "category": "movie",
                        "kind": "movie",
                        "item_id": "movie-1",
                        "title": "测试电影",
                        "path": "/media/Movies/Test.mkv",
                    }
                ],
                "skipped": {},
                "truncated": False,
                "errors": [],
            }

    monkeypatch.setattr(plugin_module, "build_adapter", lambda *_: FakeAdapter())

    response = plugin.api_scan({})

    assert response.success is True
    assert response.data["candidates"][0]["key"] == key
    assert plugin._load_scan()["scan_id"] == response.data["scan_id"]


def test_dry_run_revalidates_and_never_calls_adapter_delete(monkeypatch):
    plugin, _ = _plugin(
        monkeypatch,
        dry_run=True,
        allowed_roots="/media",
    )
    candidate = {
        "key": candidate_key("Emby", "movie", "movie-1"),
        "scan_id": "scan-1",
        "server": "Emby",
        "server_type": "emby",
        "user": "admin",
        "user_id": "user-1",
        "category": "movie",
        "kind": "movie",
        "item_id": "movie-1",
        "title": "测试电影",
        "path": "/media/Movies/Test.mkv",
        "paths": ["/media/Movies/Test.mkv"],
        "percentage": 100,
        "last_played": OLD,
        "added_at": OLD,
    }
    plugin._store_scan(
        {
            "scan_id": "scan-1",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            "candidates": [candidate],
        }
    )
    plugin._discover_services = lambda: ({"Emby": object()}, "")

    class FakeAdapter:
        deleted = False

        def refresh(self, _candidate):
            return _movie_record()

        def delete(self, _candidate):
            self.deleted = True
            return "deleted"

    adapter = FakeAdapter()
    monkeypatch.setattr(plugin_module, "build_adapter", lambda *_: adapter)

    response = plugin.api_delete(
        {
            "candidate_keys": [candidate["key"]],
            "confirmation": "DRY_RUN",
        }
    )

    assert response.success is True
    assert response.data["counts"]["dry_run"] == 1
    assert adapter.deleted is False


def test_actual_delete_requires_all_three_safety_switches(monkeypatch):
    plugin, _ = _plugin(
        monkeypatch,
        dry_run=False,
        allow_delete=False,
        allowed_roots="/media",
    )
    response = plugin.api_delete(
        {
            "candidate_keys": [candidate_key("Emby", "movie", "movie-1")],
            "confirmation": "DELETE",
        }
    )

    assert response.success is False
    assert "允许实际删除" in response.message
