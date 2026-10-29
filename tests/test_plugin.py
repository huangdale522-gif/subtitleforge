import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


def load_moviepilot_plugin():
    """Load the adapter with minimal MoviePilot framework stubs."""

    app_module = types.ModuleType("app")
    app_module.__path__ = []
    schemas_module = types.ModuleType("app.schemas")
    schemas_module.__path__ = []
    types_module = types.ModuleType("app.schemas.types")
    types_module.EventType = types.SimpleNamespace(
        PluginAction="PluginAction",
        TransferComplete="TransferComplete",
    )
    types_module.MessageType = types.SimpleNamespace(Plugin="Plugin")
    types_module.MediaType = types.SimpleNamespace(MOVIE="movie", TV="tv")
    events_module = types.ModuleType("app.sdk.events")

    class Event:
        pass

    class EventManager:
        @staticmethod
        def register(_event_type):
            def decorator(function):
                return function

            return decorator

    events_module.Event = Event
    events_module.eventmanager = EventManager()
    logging_module = types.ModuleType("app.sdk.logging")
    logging_module.logger = types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    plugin_module = types.ModuleType("app.sdk.plugin")

    class PluginBase:
        pass

    plugin_module._PluginBase = PluginBase
    modules = {
        "app": app_module,
        "app.schemas": schemas_module,
        "app.schemas.types": types_module,
        "app.sdk": types.ModuleType("app.sdk"),
        "app.sdk.events": events_module,
        "app.sdk.logging": logging_module,
        "app.sdk.plugin": plugin_module,
    }
    modules["app.sdk"].__path__ = []
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        source = (
            Path(__file__).resolve().parents[1]
            / "plugins.v2"
            / "csfreborn"
            / "__init__.py"
        )
        spec = importlib.util.spec_from_file_location(
            "subtitleforge_moviepilot_plugin", source
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_scan_notification_lists_actual_downloaded_files() -> None:
    plugin = load_moviepilot_plugin()
    text = plugin.CSFReborn._scan_notification_text(
        {
            "discovered": 651,
            "checked": 651,
            "existing": 649,
            "excluded_domestic_anime": 4,
            "queue_remaining": 2,
            "downloaded": 1,
            "downloads_today": 3,
            "daily_download_limit": 10,
            "items": [
                {
                    "title": "Some Show",
                    "season": 2,
                    "episode": 3,
                    "downloaded": [
                        {
                            "path": "/plex/电视剧/Some.Show.S02E03.chi.zh-cn.ass",
                            "language": "zh-Hans",
                            "provider": "assrt",
                        }
                    ],
                }
            ],
        }
    )

    assert "全库发现 651 个视频" in text
    assert "排除国产动漫 4 个" in text
    assert "今日额度 3/10" in text
    assert "Some Show S02E03" in text
    assert "Some.Show.S02E03.chi.zh-cn.ass" in text


def test_status_page_explains_quota_queue_and_provider_health() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._scan_dry_run = False
    plugin._scan_enabled = True
    plugin.get_data = lambda _key: None
    plugin._request_json = lambda *_args, **_kwargs: {
        "status": "ok",
        "version": "0.3.1",
        "quota": {"used": 3, "limit": 10, "remaining": 7},
        "queue": {"total": 8, "due": 5, "deferred": 3},
        "providers": {
            "order": ["xunlei", "assrt"],
            "items": [
                {
                    "name": "xunlei",
                    "configured": True,
                    "cooling_down": False,
                    "cooldown_until": None,
                },
                {
                    "name": "assrt",
                    "configured": True,
                    "cooling_down": True,
                    "cooldown_until": "2026-10-29T00:00:00+00:00",
                },
            ],
        },
    }

    rendered = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "今日扫描下载" in rendered
    assert "3 / 10" in rendered
    assert "到期 5 · 延后 3" in rendered
    assert "字幕源状态与额度边界" in rendered
    assert "所有字幕源合计" in rendered
    assert "整理完成后调用的实时补全接口" in rendered
    assert "短暂冷却 15 分钟" in rendered
    assert "冷却中" in rendered
    assert "ASSRT" in rendered


def test_queue_page_shows_posters_seasons_and_manual_actions() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._scan_dry_run = False
    plugin.get_data = lambda _key: None
    plugin._request_json = lambda *_args, **_kwargs: {
        "status": "ok",
        "quota": {"used": 0, "limit": 10, "remaining": 10},
        "queue": {"total": 2, "due": 2, "deferred": 0},
        "queue_groups": {
            "total": 2,
            "due": 2,
            "deferred": 0,
            "truncated": False,
            "groups": [
                {
                    "group_id": "season-1",
                    "title": "Some Show",
                    "year": 2024,
                    "media_type": "tv",
                    "category": "anime",
                    "season": 1,
                    "tmdb_id": 42,
                    "poster": "data:image/jpeg;base64,cG9zdGVy",
                    "missing_languages": ["zh-Hans"],
                    "item_count": 2,
                    "items_truncated": False,
                    "items": [
                        {
                            "video_path": "/plex/动漫/Some Show/Season 1/E01.mkv",
                            "filename": "E01.mkv",
                            "title": "Some Show",
                            "year": 2024,
                            "media_type": "tv",
                            "season": 1,
                            "episode": 1,
                            "languages": ["zh-Hans"],
                            "attempts": 0,
                            "deferred": False,
                        }
                    ],
                }
            ],
        },
    }

    rendered = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "VImg" in rendered
    assert "VExpansionPanels" in rendered
    assert "查看分集与手动选字幕" in rendered
    assert "待处理分集" in rendered
    assert "手动选字幕" in rendered
    assert "第 1 季" in rendered
    assert "Some Show" in rendered
    assert "manual/search" in rendered
    assert "manual/download" in rendered


def test_plugin_registers_queue_and_manual_api_routes() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()

    paths = {route["path"] for route in plugin.get_api()}

    assert {"/queue", "/manual/search", "/manual/download"} <= paths


def test_media_poster_url_does_not_use_landscape_backdrop() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    media = types.SimpleNamespace(
        poster_url=None,
        poster_path=None,
        poster=None,
        cover=None,
        backdrop_path="/landscape-backdrop.jpg",
        tmdb_info={"backdrop_path": "/another-backdrop.jpg"},
    )

    assert plugin._media_poster_url(media) is None


def test_media_poster_url_builds_tmdb_poster_url() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    media = types.SimpleNamespace(
        poster_url=None,
        poster_path="/poster.jpg",
        poster=None,
        cover=None,
        tmdb_info={},
    )

    assert plugin._media_poster_url(media) == (
        "/api/v1/system/img/1?"
        "imgurl=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw500%2Fposter.jpg"
    )


def test_tmdb_image_url_uses_moviepilot_v3_runtime_settings(
    monkeypatch,
) -> None:
    plugin_module = load_moviepilot_plugin()
    settings_module = types.ModuleType("app.runtime.config")
    settings_module.settings = types.SimpleNamespace(
        API_V1_STR="/api/v1",
        TMDB_IMAGE_URL=lambda path, size: (
            f"https://images.example/t/p/{size}/{str(path).lstrip('/')}"
        ),
    )
    monkeypatch.setitem(sys.modules, "app.runtime.config", settings_module)

    assert plugin_module.CSFReborn._tmdb_image_url("/poster.jpg") == (
        "https://images.example/t/p/w500/poster.jpg"
    )


def test_tmdb_image_url_proxies_default_domain_through_moviepilot(
    monkeypatch,
) -> None:
    plugin_module = load_moviepilot_plugin()
    settings_module = types.ModuleType("app.runtime.config")
    settings_module.settings = types.SimpleNamespace(
        API_V1_STR="/api/v1",
        TMDB_IMAGE_DOMAIN="image.tmdb.org",
        TMDB_IMAGE_URL=lambda path, size: (
            f"https://image.tmdb.org/t/p/{size}/{str(path).lstrip('/')}"
        ),
    )
    monkeypatch.setitem(sys.modules, "app.runtime.config", settings_module)

    assert plugin_module.CSFReborn._tmdb_image_url("/poster.jpg") == (
        "/api/v1/system/img/1?"
        "imgurl=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw500%2Fposter.jpg"
    )


def test_domestic_anime_filter_uses_moviepilot_metadata() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._domestic_anime_regions = "CN,TW,HK"
    plugin._domestic_anime_path_keywords = "国漫,国产动漫"

    domestic = types.SimpleNamespace(
        genre_ids=[16],
        origin_country=["CN"],
        production_countries=[],
        original_language="zh",
        tmdb_info={},
    )
    foreign = types.SimpleNamespace(
        genre_ids=[16],
        origin_country=["JP"],
        production_countries=[],
        original_language="ja",
        tmdb_info={},
    )
    unknown = types.SimpleNamespace(
        genre_ids=[16],
        origin_country=[],
        production_countries=[],
        original_language=None,
        tmdb_info={},
    )

    assert plugin._is_domestic_anime(
        domestic, ["/plex/动漫/某部动画/Season 1/01.mkv"]
    )
    assert not plugin._is_domestic_anime(
        foreign, ["/plex/动漫/Foreign/Season 1/01.mkv"]
    )
    assert plugin._is_domestic_anime(
        unknown, ["/plex/动漫/国漫/某部动画/01.mkv"]
    )
    assert not plugin._is_domestic_anime(
        unknown, ["/plex/电影/普通电影/普通电影.mkv"]
    )


def test_domestic_anime_filter_reads_object_tmdb_info() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._domestic_anime_regions = "CN,TW,HK"
    plugin._domestic_anime_path_keywords = "国漫,国产动漫"
    media = types.SimpleNamespace(
        genre_ids=[],
        origin_country=[],
        production_countries=[],
        original_language=None,
        tmdb_info=types.SimpleNamespace(
            genre_ids=[16],
            origin_country=["CN"],
            production_countries=[],
            original_language="zh",
        ),
    )

    assert plugin._is_domestic_anime(
        media,
        ["/plex/动漫/某部动画/Season 1/01.mkv"],
    )


def test_scan_library_sends_domestic_anime_filter_settings() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._scan_paths = "/plex/动漫"
    plugin._scan_exclude_paths = ""
    plugin._exclude_domestic_anime = True
    plugin._domestic_anime_regions = "CN,TW,HK"
    plugin._domestic_anime_path_keywords = "国漫,国产动画"
    plugin._notify = False
    captured: dict = {}

    def request_json(method, path, payload, timeout=None):
        captured.update(payload)
        return {"status": "ok", "items": []}

    plugin._request_json = request_json
    plugin.save_data = lambda *_args, **_kwargs: None

    result = asyncio.run(plugin.scan_library())

    assert result["status"] == "ok"
    assert captured["exclude_domestic_anime"] is True
    assert captured["domestic_anime_regions"] == ["CN", "TW", "HK"]
    assert captured["domestic_anime_path_keywords"] == ["国漫", "国产动画"]


def test_scan_library_sends_moviepilot_tmdb_metadata(tmp_path: Path) -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    show = tmp_path / "牧神记 (2024) {tmdbid=236534}" / "Season 1"
    show.mkdir(parents=True)
    video = show / "牧神记 S01E08 2160p.OurTV.mp4"
    video.write_bytes(b"video")
    plugin._enabled = True
    plugin._scan_paths = str(tmp_path)
    plugin._scan_exclude_paths = ""
    plugin._exclude_domestic_anime = True
    plugin._domestic_anime_regions = "CN,TW,HK"
    plugin._domestic_anime_path_keywords = "国漫,国产动画"
    plugin._notify = False
    plugin.chain = types.SimpleNamespace(
        tmdb_info=lambda **_kwargs: {
            "name": "牧神记",
            "original_name": "牧神记",
            "first_air_date": "2024-10-27",
            "genre_ids": [16],
            "origin_country": ["CN"],
            "original_language": "zh",
            "poster_path": "/mushenji.jpg",
        }
    )
    captured: dict = {}

    def request_json(method, path, payload, timeout=None):
        captured.update(payload)
        return {"status": "ok", "items": []}

    plugin._request_json = request_json
    plugin.save_data = lambda *_args, **_kwargs: None

    result = asyncio.run(plugin.scan_library())

    assert result["metadata_enriched"] == 1
    metadata = captured["media_metadata"][0]
    assert metadata["tmdb_id"] == 236534
    assert metadata["media_type"] == "tv"
    assert metadata["title"] == "牧神记"
    assert metadata["original_title"] == "牧神记"
    assert metadata["year"] == 2024
    assert metadata["origin_countries"] == ["CN"]
    assert metadata["genres"] == ["16"]
    assert "mushenji.jpg" in metadata["poster_url"]
    assert str(show.parent) in metadata["path_prefixes"]


def test_queue_enrichment_hides_domestic_anime_and_adds_posters() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._exclude_domestic_anime = True
    plugin._domestic_anime_regions = "CN,TW,HK"
    plugin._domestic_anime_path_keywords = "国漫,国产动画"
    plugin.chain = types.SimpleNamespace(
        tmdb_info=lambda tmdbid, **_kwargs: (
            {
                "name": "牧神记",
                "genre_ids": [16],
                "origin_country": ["CN"],
                "original_language": "zh",
                "poster_path": "/mushenji.jpg",
            }
            if int(tmdbid) == 236534
            else {
                "name": "葬送的芙莉莲",
                "genre_ids": [16],
                "origin_country": ["JP"],
                "original_language": "ja",
                "poster_path": "/frieren.jpg",
            }
        )
    )
    plugin._request_json = lambda *_args, **_kwargs: {
        "status": "ok",
        "queue": {"total": 2, "due": 2, "deferred": 0},
        "queue_groups": {
            "total": 2,
            "due": 2,
            "deferred": 0,
            "groups": [
                {
                    "group_id": "domestic",
                    "title": "牧神记",
                    "media_type": "tv",
                    "category": "anime",
                    "season": 1,
                    "tmdb_id": 236534,
                    "poster": None,
                    "item_count": 1,
                    "items": [
                        {
                            "video_path": "/plex/动漫/牧神记/Season 1/E08.mp4",
                            "deferred": False,
                        }
                    ],
                },
                {
                    "group_id": "foreign",
                    "title": "葬送的芙莉莲",
                    "media_type": "tv",
                    "category": "anime",
                    "season": 1,
                    "tmdb_id": 209867,
                    "poster": "https://image.tmdb.org/t/p/w500/stale.jpg",
                    "item_count": 1,
                    "items": [
                        {
                            "video_path": "/plex/动漫/芙莉莲/Season 1/E08.mkv",
                            "deferred": False,
                        }
                    ],
                },
            ],
        },
    }

    result = asyncio.run(plugin.queue())

    assert result["excluded_in_queue"] == 1
    assert result["queue"]["total"] == 1
    assert result["queue_groups"]["total"] == 1
    groups = result["queue_groups"]["groups"]
    assert [group["group_id"] for group in groups] == ["foreign"]
    assert "frieren.jpg" in groups[0]["poster"]
    assert "stale.jpg" not in groups[0]["poster"]
