from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.plugins.watchedcleaner as plugin_module
from app.plugins.watchedcleaner import WatchedCleaner
from app.plugins.watchedcleaner import adapters as adapter_module
from app.plugins.watchedcleaner.adapters import (
    EmbyFamilyAdapter,
    PlexAdapter,
    _collect_candidates,
)
from app.plugins.watchedcleaner.policy import (
    candidate_key,
    normalize_record,
    normalize_tv_mode,
    path_allowed,
    record_block_reason,
)


OLD = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
YEAR_OLD = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()


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
        "notify_enabled": False,
        "notify_user": "",
        "notify_scan_results": True,
        "notify_delete_results": True,
        "age_reminder_enabled": False,
        "age_reminder_days": 365,
        "age_reminder_interval_hours": 168,
        "age_reminder_limit": 10,
        "age_reminder_cooldown_days": 30,
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


def test_season_mode_groups_watched_episodes_into_one_season_candidate():
    episodes = [
        _movie_record(
            kind="episode",
            item_id="ep-1",
            series_id="show-1",
            season_id="season-1",
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
            season_id="season-1",
            title="第二集",
            series_title="测试剧",
            season=1,
            episode=2,
            paths=["/media/TV/Test/S01E02.mkv"],
        ),
        _movie_record(
            kind="episode",
            item_id="ep-3",
            series_id="show-1",
            season_id="season-2",
            title="第三集",
            series_title="测试剧",
            season=2,
            episode=1,
            paths=["/media/TV/Test/S02E01.mkv"],
            played=False,
            percentage=50,
        ),
    ]
    options = {
        "categories": ["tv"],
        "tv_mode": "season",
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

    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "season"
    assert candidate["item_id"] == "season-1"
    assert candidate["series_id"] == "show-1"
    assert candidate["season"] == 1
    assert candidate["episode_count"] == 2
    assert candidate["paths"] == [
        "/media/TV/Test/S01E01.mkv",
        "/media/TV/Test/S01E02.mkv",
    ]
    assert result["skipped"]["仍有 1 集未达到观看阈值"] == 1


def test_tv_mode_defaults_to_season_and_skips_specials():
    episode = _movie_record(
        kind="episode",
        item_id="special-1",
        series_id="show-1",
        season_id="season-0",
        title="特别篇",
        series_title="测试剧",
        season=0,
        episode=1,
    )

    assert normalize_tv_mode("") == "season"
    result = _collect_candidates(
        records=[episode],
        options={
            "categories": ["tv"],
            "tv_mode": "",
            "watched_threshold": 90,
            "min_days_since_played": 7,
            "min_days_since_added": 7,
            "protect_favorites": True,
            "allowed_roots": ["/media"],
            "anime_keywords": [],
            "max_candidates": 100,
        },
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
    assert result["skipped"]["特别篇（第0季）不参与按季清理"] == 1


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
            "SeasonId": "season-1",
            "SeriesName": "测试剧",
            "SeriesPrimaryImageTag": "series-tag",
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
    assert record["season_id"] == "season-1"
    assert record["percentage"] == 95
    assert record["favorite"] is True
    assert record["paths"] == ["/media/TV/Test/S01E02.mkv"]
    assert record["image"] == (
        "http://emby.local:8096/emby/Items/series-1/Images/Primary"
        "?api_key=secret&tag=series-tag"
    )


def test_emby_episode_uses_own_poster_when_series_tag_is_missing():
    service = SimpleNamespace(
        type="jellyfin",
        config=SimpleNamespace(
            config={"host": "http://jellyfin.local:8096", "apikey": "secret"}
        ),
        instance=None,
    )
    adapter = EmbyFamilyAdapter("客厅 Jellyfin", service, "jellyfin")
    record = adapter._record(
        {
            "Id": "episode-1",
            "Type": "Episode",
            "Name": "第一集",
            "SeriesId": "series-1",
            "SeasonId": "season-1",
            "SeriesName": "测试剧",
            "ImageTags": {"Primary": "episode-tag"},
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "Path": "/media/TV/Test/S01E01.mkv",
            "UserData": {"Played": True, "LastPlayedDate": OLD},
        }
    )

    assert record["image"] == (
        "http://jellyfin.local:8096/Items/episode-1/Images/Primary"
        "?api_key=secret&tag=episode-tag"
    )


def test_episode_mode_inherits_series_poster():
    episode = _movie_record(
        kind="episode",
        item_id="ep-1",
        series_id="show-1",
        season_id="season-1",
        title="第一集",
        series_title="测试剧",
        season=1,
        episode=1,
    )
    series = _movie_record(
        kind="series",
        item_id="show-1",
        title="测试剧",
        image="http://emby.local:8096/emby/Items/show-1/Images/Primary",
    )

    result = _collect_candidates(
        records=[series, episode],
        options={
            "categories": ["tv"],
            "tv_mode": "episode",
            "watched_threshold": 90,
            "min_days_since_played": 7,
            "min_days_since_added": 7,
            "protect_favorites": True,
            "allowed_roots": ["/media"],
            "anime_keywords": [],
            "max_candidates": 100,
        },
        service_name="Emby",
        service_type="emby",
        user_name="admin",
        user_id="user-1",
        library_name="电视剧",
        base_category="tv",
        scan_id="scan",
        limit=100,
    )

    assert result["candidates"][0]["image"] == series["image"]


def test_emby_season_refresh_reads_season_children(monkeypatch):
    service = SimpleNamespace(
        type="emby",
        config=SimpleNamespace(
            config={"host": "http://emby.local:8096", "apikey": "secret"}
        ),
        instance=None,
    )
    adapter = EmbyFamilyAdapter("客厅 Emby", service, "emby")
    season_item = {
        "Id": "season-1",
        "Type": "Season",
        "Name": "第1季",
        "SeriesId": "series-1",
        "SeriesName": "测试剧",
        "IndexNumber": 1,
    }
    episode_item = {
        "Id": "episode-1",
        "Type": "Episode",
        "Name": "第一集",
        "SeriesId": "series-1",
        "SeasonId": "season-1",
        "SeriesName": "测试剧",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "Path": "/media/TV/Test/S01E01.mkv",
        "DateCreated": OLD,
        "UserData": {"Played": True, "LastPlayedDate": OLD},
    }
    monkeypatch.setattr(
        adapter,
        "_get_item",
        lambda user_id, item_id: season_item if item_id == "season-1" else {
            "Id": "series-1",
            "Type": "Series",
            "Name": "测试剧",
        },
    )
    monkeypatch.setattr(
        adapter,
        "_list_items",
        lambda user_id, **kwargs: [episode_item],
    )

    record = adapter.refresh(
        {
            "kind": "season",
            "item_id": "season-1",
            "user_id": "user-1",
        }
    )

    assert record["kind"] == "season"
    assert record["item_id"] == "season-1"
    assert record["series_id"] == "series-1"
    assert record["season"] == 1
    assert len(record["episodes"]) == 1


def test_plex_episode_parser_preserves_season_id():
    service = SimpleNamespace(
        type="plex",
        config=SimpleNamespace(
            config={"host": "http://plex.local:32400", "token": "secret"}
        ),
        instance=None,
    )
    adapter = PlexAdapter("客厅 Plex", service)
    record = adapter._record(
        {
            "type": "episode",
            "ratingKey": "episode-1",
            "grandparentRatingKey": "series-1",
            "grandparentTitle": "测试剧",
            "grandparentThumb": "/library/metadata/series-1/thumb/1",
            "parentRatingKey": "season-1",
            "parentIndex": 1,
            "index": 2,
            "title": "第二集",
            "viewCount": 1,
            "addedAt": 1,
            "lastViewedAt": 2,
            "Media": [{"Part": [{"file": "/media/TV/Test/S01E02.mkv"}]}],
        }
    )

    assert record["kind"] == "episode"
    assert record["series_id"] == "series-1"
    assert record["season_id"] == "season-1"
    assert record["season"] == 1
    assert record["image"] == (
        "http://plex.local:32400/library/metadata/series-1/thumb/1"
        "?X-Plex-Token=secret"
    )


def test_plex_season_refresh_reads_season_children(monkeypatch):
    service = SimpleNamespace(
        type="plex",
        config=SimpleNamespace(
            config={"host": "http://plex.local:32400", "token": "secret"}
        ),
        instance=None,
    )
    adapter = PlexAdapter("客厅 Plex", service)
    requested = []
    season = {
        "type": "season",
        "ratingKey": "season-1",
        "parentRatingKey": "series-1",
        "parentTitle": "测试剧",
        "title": "第1季",
        "index": 1,
    }
    episode = {
        "type": "episode",
        "ratingKey": "episode-1",
        "grandparentRatingKey": "series-1",
        "grandparentTitle": "测试剧",
        "parentRatingKey": "season-1",
        "parentIndex": 1,
        "index": 1,
        "title": "第一集",
        "viewCount": 1,
        "Media": [{"Part": [{"file": "/media/TV/Test/S01E01.mkv"}]}],
    }
    show = {
        "type": "show",
        "ratingKey": "series-1",
        "title": "测试剧",
    }

    def fake_get_json(path, params=None):
        requested.append(path)
        payloads = {
            "/library/metadata/season-1/children": [episode],
            "/library/metadata/season-1": [season],
            "/library/metadata/series-1": [show],
        }
        return {"MediaContainer": {"Metadata": payloads.get(path, [])}}

    monkeypatch.setattr(adapter, "_get_json", fake_get_json)
    record = adapter.refresh(
        {
            "kind": "season",
            "item_id": "season-1",
        }
    )

    assert "/library/metadata/season-1/children" in requested
    assert all(not path.endswith("/allLeaves") for path in requested)
    assert record["kind"] == "season"
    assert record["series_id"] == "series-1"
    assert record["season"] == 1
    assert len(record["episodes"]) == 1


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


def test_age_reminder_service_requires_telegram_user_and_enabled(monkeypatch):
    plugin, _ = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="",
        age_reminder_enabled=True,
    )
    assert plugin.get_service() == []

    plugin, _ = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
        age_reminder_enabled=True,
        age_reminder_interval_hours=24,
    )
    services = plugin.get_service()

    assert len(services) == 1
    assert services[0]["trigger"] == "interval"
    assert services[0]["kwargs"] == {"hours": 24}
    assert services[0]["func"] == plugin.scan_age_reminders


def test_age_reminder_filters_old_candidates_and_sends_buttons(monkeypatch):
    plugin, data = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
        age_reminder_enabled=True,
        age_reminder_days=365,
        age_reminder_cooldown_days=30,
    )
    old = {
        "key": candidate_key("Emby", "movie", "old-movie"),
        "scan_id": "scan-age",
        "server": "Emby",
        "server_type": "emby",
        "user": "admin",
        "category": "movie",
        "kind": "movie",
        "item_id": "old-movie",
        "title": "一年前的电影",
        "path": "/media/Movies/Old.mkv",
        "paths": ["/media/Movies/Old.mkv"],
        "percentage": 100,
        "last_played": YEAR_OLD,
        "added_at": YEAR_OLD,
    }
    recent = {
        **old,
        "key": candidate_key("Emby", "movie", "recent-movie"),
        "item_id": "recent-movie",
        "title": "新电影",
        "added_at": OLD,
    }
    plugin._store_scan(
        {
            "scan_id": "scan-age",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scanned": 2,
            "candidates": [old, recent],
            "errors": [],
        }
    )
    plugin.api_scan = lambda _payload: SimpleNamespace(
        success=True,
        message="扫描完成",
    )
    sent = []
    plugin.post_message = lambda **kwargs: sent.append(kwargs)

    result = plugin._run_age_reminders()

    assert result["success"] is True
    assert result["age_candidates"] == 1
    assert result["sent"] == 1
    assert len(sent) == 1
    assert sent[0]["channel"] == plugin_module.NotificationChannel.Telegram
    assert sent[0]["userid"] == "123456789"
    callbacks = [
        button["callback_data"]
        for row in sent[0]["buttons"]
        for button in row
    ]
    assert callbacks[0] == f"[PLUGIN]WatchedCleaner|d:{old['key']}"
    assert callbacks[1] == f"[PLUGIN]WatchedCleaner|k:{old['key']}"
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    assert data["age_reminders"]["items"][old["key"]]["status"] == "pending"


def test_age_reminder_cooldown_prevents_duplicate(monkeypatch):
    plugin, _ = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
        age_reminder_enabled=True,
    )
    key = candidate_key("Emby", "movie", "old-movie")
    candidate = {
        "key": key,
        "added_at": YEAR_OLD,
        "category": "movie",
        "kind": "movie",
        "title": "一年前的电影",
    }
    state = {
        "items": {
            key: {
                "status": "pending",
                "last_notified_at": datetime.now(timezone.utc).isoformat(),
            }
        }
    }

    assert (
        plugin._eligible_age_reminder_candidates([candidate], state) == []
    )


def test_age_reminder_uses_season_scope_for_season_candidate(monkeypatch):
    plugin, _ = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
        age_reminder_enabled=True,
    )
    key = candidate_key("Emby", "season", "season-1")
    candidate = {
        "key": key,
        "server": "Emby",
        "category": "tv",
        "kind": "season",
        "item_id": "season-1",
        "season_id": "season-1",
        "title": "测试剧",
        "series_title": "测试剧",
        "season": 1,
        "episode_count": 12,
        "percentage": 100,
        "added_at": YEAR_OLD,
        "path": "/media/TV/Test/Season 1",
        "age_days": 400,
        "image": "http://emby.local:8096/emby/Items/season-1/Images/Primary",
    }
    sent = []
    plugin.post_message = lambda **kwargs: sent.append(kwargs)

    assert plugin._send_age_reminder(candidate) is True
    assert "测试剧 第1季" in sent[0]["text"]
    assert sent[0]["buttons"][0][0]["text"] == "删除这一季"
    assert sent[0]["image"] == candidate["image"]


def test_candidate_table_and_scan_push_send_one_poster_item_per_candidate(
    monkeypatch,
):
    plugin, _ = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
    )
    first = {
        "key": candidate_key("Emby", "movie", "movie-1"),
        "server": "Emby",
        "user": "admin",
        "category": "movie",
        "kind": "movie",
        "title": "测试电影",
        "image": "http://emby.local:8096/emby/Items/movie-1/Images/Primary",
        "percentage": 100,
        "last_played": OLD,
        "path": "/media/Movies/Test.mkv",
    }
    second = {
        **first,
        "key": candidate_key("Emby", "movie", "movie-2"),
        "item_id": "movie-2",
        "title": "第二部电影",
        "image": "http://emby.local:8096/emby/Items/movie-2/Images/Primary",
        "path": "/media/Movies/Test2.mkv",
    }

    table = plugin._candidate_table([first, second])
    row = table["content"][1]["content"][0]
    poster = row["content"][1]["content"][0]
    assert poster["component"] == "VImg"
    assert poster["props"]["src"] == first["image"]

    scheduled = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            scheduled.append(self.kwargs)

    monkeypatch.setattr(plugin_module, "Thread", FakeThread)
    sent = []
    plugin.post_message = lambda **kwargs: sent.append(kwargs)
    plugin._notify_scan_completed(
        {
            "scanned": 2,
            "candidates": [first, second],
            "created_at": OLD,
            "errors": [],
        },
        "扫描完成",
    )

    assert sent[0]["image"] is None
    assert "逐条推送：2 条" in sent[0]["text"]
    assert scheduled[0]["kwargs"]["title_prefix"] == "扫描候选"
    items = scheduled[0]["args"][0]
    assert [item["image"] for item in items] == [
        first["image"],
        second["image"],
    ]

    item_messages = []
    plugin.post_message = lambda **kwargs: item_messages.append(kwargs)
    result = plugin._send_notification_items(
        items,
        title_prefix="扫描候选",
        delay_seconds=0,
    )

    assert result == {"sent": 2, "failed": 0, "total": 2}
    assert [message["image"] for message in item_messages] == [
        first["image"],
        second["image"],
    ]
    assert item_messages[0]["title"].startswith("扫描候选 1/2")
    assert item_messages[1]["title"].startswith("扫描候选 2/2")


def test_delete_push_sends_each_result_with_its_poster(monkeypatch):
    plugin, _ = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
    )
    scheduled = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            scheduled.append(self.kwargs)

    monkeypatch.setattr(plugin_module, "Thread", FakeThread)
    sent = []
    plugin.post_message = lambda **kwargs: sent.append(kwargs)
    results = [
        {
            "key": candidate_key("Emby", "movie", "movie-1"),
            "category": "movie",
            "kind": "movie",
            "title": "测试电影",
            "image": "http://emby.local:8096/emby/Items/movie-1/Images/Primary",
            "status": "deleted",
            "message": "完成",
        },
        {
            "key": candidate_key("Emby", "movie", "movie-2"),
            "category": "movie",
            "kind": "movie",
            "title": "第二部电影",
            "image": "http://emby.local:8096/emby/Items/movie-2/Images/Primary",
            "status": "skipped",
            "message": "收藏保护",
        },
    ]

    plugin._notify_delete_completed(
        {"deleted": 1, "dry_run": 0, "skipped": 1, "failed": 0},
        results,
    )

    assert sent[0]["image"] is None
    assert "逐条推送：2 条" in sent[0]["text"]
    assert scheduled[0]["kwargs"]["title_prefix"] == "删除预演"
    items = scheduled[0]["args"][0]
    assert [item["image"] for item in items] == [
        results[0]["image"],
        results[1]["image"],
    ]
    assert "已删除" in items[0]["text"]
    assert "已跳过" in items[1]["text"]


def test_telegram_keep_callback_marks_kept_without_delete(monkeypatch):
    plugin, data = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
    )
    key = candidate_key("Emby", "movie", "movie-1")
    data["age_reminders"] = {
        "version": 1,
        "items": {
            key: {
                "status": "pending",
                "recipient": "123456789",
                "candidate": {
                    "key": key,
                    "server": "Emby",
                    "category": "movie",
                    "kind": "movie",
                    "title": "测试电影",
                },
            }
        },
    }
    replies = []
    plugin.post_message = lambda **kwargs: replies.append(kwargs)
    monkeypatch.setattr(
        plugin_module,
        "build_adapter",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应删除")),
    )

    plugin.on_message_action(
        SimpleNamespace(
            event_data={
                "plugin_id": "WatchedCleaner",
                "text": f"k:{key}",
                "userid": 123456789,
                "channel": plugin_module.NotificationChannel.Telegram,
                "source": "bot",
            }
        )
    )

    assert data["age_reminders"]["items"][key]["status"] == "kept"
    assert replies[-1]["title"] == "已保留"


def test_telegram_delete_callback_still_requires_actual_delete_mode(monkeypatch):
    plugin, data = _plugin(
        monkeypatch,
        dry_run=True,
        allow_delete=True,
        allowed_roots="/media",
        notify_enabled=True,
        notify_user="123456789",
    )
    key = candidate_key("Emby", "movie", "movie-1")
    data["age_reminders"] = {
        "version": 1,
        "items": {
            key: {
                "status": "pending",
                "recipient": "123456789",
                "candidate": {
                    "key": key,
                    "server": "Emby",
                    "category": "movie",
                    "kind": "movie",
                    "title": "测试电影",
                },
            }
        },
    }
    replies = []
    plugin.post_message = lambda **kwargs: replies.append(kwargs)
    monkeypatch.setattr(
        plugin_module,
        "build_adapter",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应删除")),
    )

    plugin.on_message_action(
        SimpleNamespace(
            event_data={
                "plugin_id": "WatchedCleaner",
                "text": f"d:{key}",
                "userid": 123456789,
                "channel": plugin_module.NotificationChannel.Telegram,
                "source": "bot",
            }
        )
    )

    assert data["age_reminders"]["items"][key]["status"] == "pending"
    assert replies[-1]["title"] == "当前仍为预演模式"


def test_telegram_delete_callback_revalidates_and_deletes(monkeypatch):
    plugin, data = _plugin(
        monkeypatch,
        dry_run=False,
        allow_delete=True,
        allowed_roots="/media",
        notify_enabled=True,
        notify_user="123456789",
    )
    key = candidate_key("Emby", "movie", "movie-1")
    candidate = {
        "key": key,
        "scan_id": "scan-age",
        "server": "Emby",
        "server_type": "emby",
        "user": "admin",
        "category": "movie",
        "kind": "movie",
        "item_id": "movie-1",
        "title": "测试电影",
        "path": "/media/Movies/Test.mkv",
        "paths": ["/media/Movies/Test.mkv"],
        "percentage": 100,
        "last_played": YEAR_OLD,
        "added_at": YEAR_OLD,
    }
    data["age_reminders"] = {
        "version": 1,
        "items": {
            key: {
                "status": "pending",
                "recipient": "123456789",
                "scan_id": "scan-age",
                "candidate": candidate,
            }
        },
    }
    plugin._store_scan(
        {
            "scan_id": "scan-age",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            "candidates": [candidate],
        }
    )
    plugin._discover_services = lambda: ({"Emby": object()}, "")
    replies = []
    plugin.post_message = lambda **kwargs: replies.append(kwargs)

    class FakeAdapter:
        deleted = False

        def refresh(self, _candidate):
            return _movie_record(added_at=YEAR_OLD, last_played=YEAR_OLD)

        def delete(self, _candidate):
            self.deleted = True
            return "媒体服务器已接受删除请求"

    adapter = FakeAdapter()
    monkeypatch.setattr(plugin_module, "build_adapter", lambda *_: adapter)

    plugin.on_message_action(
        SimpleNamespace(
            event_data={
                "plugin_id": "WatchedCleaner",
                "text": f"d:{key}",
                "userid": 123456789,
                "channel": plugin_module.NotificationChannel.Telegram,
                "source": "bot",
            }
        )
    )

    assert adapter.deleted is True
    assert data["age_reminders"]["items"][key]["status"] == "deleted"
    assert data["last_scan"]["candidates"] == []
    assert data["history"][0]["action"] == "telegram_delete"
    assert replies[-1]["title"] == "删除完成"


def test_telegram_callback_rejects_other_user(monkeypatch):
    plugin, data = _plugin(
        monkeypatch,
        notify_enabled=True,
        notify_user="123456789",
    )
    key = candidate_key("Emby", "movie", "movie-1")
    data["age_reminders"] = {
        "version": 1,
        "items": {
            key: {
                "status": "pending",
                "recipient": "123456789",
                "candidate": {
                    "key": key,
                    "server": "Emby",
                    "category": "movie",
                    "kind": "movie",
                    "title": "测试电影",
                },
            }
        },
    }
    replies = []
    plugin.post_message = lambda **kwargs: replies.append(kwargs)
    monkeypatch.setattr(
        plugin_module,
        "build_adapter",
        lambda *_: (_ for _ in ()).throw(AssertionError("不应删除")),
    )

    plugin.on_message_action(
        SimpleNamespace(
            event_data={
                "plugin_id": "WatchedCleaner",
                "text": f"d:{key}",
                "userid": 987654321,
                "channel": plugin_module.NotificationChannel.Telegram,
                "source": "bot",
            }
        )
    )

    assert data["age_reminders"]["items"][key]["status"] == "pending"
    assert replies[-1]["title"] == "无权处理"
