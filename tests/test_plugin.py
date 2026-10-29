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
