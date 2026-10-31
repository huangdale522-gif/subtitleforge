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
            "excluded_configured_paths": 3,
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
    assert "配置排除目录中的视频 3 个" in text
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
    assert "自动顺序（使用全局字幕源顺序）" in rendered
    assert "provider_order" in rendered


def test_plugin_registers_queue_and_manual_api_routes() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()

    paths = {route["path"] for route in plugin.get_api()}

    assert {
        "/queue",
        "/manual/search",
        "/manual/download",
        "/cleanup/bluray",
    } <= paths


def test_transfer_paths_skip_bluray_and_non_video_files() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    movie_dir = "/plex/电影/外语电影/Evil Dead Burn (2026)"
    transferinfo = types.SimpleNamespace(
        target_diritem=types.SimpleNamespace(path=movie_dir),
        file_list_new=[
            "BDMV/STREAM/00000.m2ts",
            "BDMV/JAR/logger.m2ts",
            "BDMV/META/DL/loadingAnimation.m2ts",
            "poster.jpg",
            "Evil.Dead.Burn.2026.mkv",
        ],
        target_item=None,
    )

    assert plugin._extract_video_paths(transferinfo) == [
        f"{movie_dir}/Evil.Dead.Burn.2026.mkv"
    ]


def test_transfer_complete_ignores_bluray_only_payload() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._notify = True
    transferinfo = types.SimpleNamespace(
        target_diritem=types.SimpleNamespace(
            path="/plex/电影/外语电影/Evil Dead Burn (2026)"
        ),
        file_list_new=[
            "BDMV/STREAM/00000.m2ts",
            "BDMV/JAR/logger.m2ts",
        ],
        target_item=None,
    )
    called = False

    def request_json(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "ok"}

    plugin._request_json = request_json
    plugin.post_message = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("BDMV payload must not notify")
    )
    event = types.SimpleNamespace(
        event_data={"transferinfo": transferinfo, "mediainfo": None}
    )

    asyncio.run(plugin.on_transfer_complete(event))

    assert called is False


def test_cleanup_bluray_subtitles_only_removes_generated_siblings(
    tmp_path: Path,
) -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    stream = tmp_path / "Evil Dead Burn (2026)" / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    first_video = stream / "00000.m2ts"
    second_video = stream / "00001.m2ts"
    first_video.write_bytes(b"video")
    second_video.write_bytes(b"video")
    generated = [
        stream / "00000.chi.zh-cn.srt",
        stream / "00001.zh-tw.ass",
    ]
    for subtitle in generated:
        subtitle.write_text("subtitle", encoding="utf-8")
    manual = stream / "00000.manual.srt"
    manual.write_text("manual", encoding="utf-8")
    orphan = stream / "99999.chi.zh-cn.srt"
    orphan.write_text("orphan", encoding="utf-8")

    plugin._media_roots = str(tmp_path)
    history = [
        {
            "result": {
                "downloaded": [
                    {"path": str(path)} for path in generated
                ]
            }
        }
    ]
    plugin._load_history = lambda: history
    saved: dict = {}
    plugin.save_data = lambda key, value: saved.update({key: value})

    candidates, errors = plugin._bluray_cleanup_candidates(history)

    assert candidates == sorted(generated)
    assert errors == []

    result = plugin._cleanup_bluray_subtitles()

    assert result["status"] == "ok"
    assert result["candidates"] == 2
    assert result["deleted"] == 2
    assert result["paths"] == [str(path) for path in sorted(generated)]
    assert all(not path.exists() for path in generated)
    assert manual.exists()
    assert orphan.exists()
    assert history[0]["result"]["downloaded"] == []
    assert "download_history" in saved
    assert plugin._bluray_cleanup_candidates(history) == ([], [])


def test_bluray_cleanup_page_counts_existing_files_only(tmp_path: Path) -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    stream = tmp_path / "Movie (2026)" / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    video = stream / "00000.m2ts"
    video.write_bytes(b"video")
    generated = stream / "00000.chi.zh-cn.srt"
    generated.write_text("subtitle", encoding="utf-8")
    missing = stream / "00001.chi.zh-cn.srt"
    history = [
        {
            "result": {
                "downloaded": [
                    {"path": str(generated)},
                    {"path": str(missing)},
                ]
            }
        }
    ]
    plugin._media_roots = str(tmp_path)
    plugin._load_history = lambda: history
    plugin.get_data = lambda _key: None
    plugin._request_json = lambda *_args, **_kwargs: {}

    rendered = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "清理蓝光分片误生成字幕（1）" in rendered
    assert "检测到 1 个蓝光分片误生成字幕" in rendered
    assert str(generated) in rendered
    assert str(missing) not in rendered

    generated.unlink()
    rendered = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "清理蓝光分片误生成字幕（1）" not in rendered
    assert "清理蓝光分片误生成字幕（0）" not in rendered


def test_cleanup_bluray_api_requires_confirmation() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()

    result = asyncio.run(plugin.cleanup_bluray_subtitles({}))

    assert result["status"] == "error"
    assert "明确确认" in result["message"]


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


def test_media_poster_url_accepts_relative_moviepilot_proxy() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    media = types.SimpleNamespace(
        poster_url=(
            "api/v1/system/img/1?"
            "imgurl=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw500%2Fposter.jpg"
        ),
        poster_path=None,
        poster=None,
        cover=None,
        tmdb_info={},
    )

    assert plugin._media_poster_url(media) == (
        "/api/v1/system/img/1?"
        "imgurl=https%3A%2F%2Fimage.tmdb.org%2Ft%2Fp%2Fw500%2Fposter.jpg"
    )


def test_media_poster_url_preserves_absolute_local_poster() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    media = types.SimpleNamespace(
        poster_url="/config/cache/MediaCover/poster.jpg",
        poster_path=None,
        poster=None,
        cover=None,
        tmdb_info={},
    )

    assert plugin._media_poster_url(media) == (
        "/config/cache/MediaCover/poster.jpg"
    )


def test_media_poster_url_proxies_alternate_tmdb_cdn() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    media = types.SimpleNamespace(
        poster_url="https://media.themoviedb.org/t/p/w500/poster.jpg",
        poster_path=None,
        poster=None,
        cover=None,
        tmdb_info={},
    )

    assert plugin._media_poster_url(media) == (
        "/api/v1/system/img/1?"
        "imgurl=https%3A%2F%2Fmedia.themoviedb.org%2Ft%2Fp%2Fw500%2Fposter.jpg"
    )


def test_fetch_tmdb_info_merges_native_fallback_when_chain_has_no_poster() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin.chain = types.SimpleNamespace(
        tmdb_info=lambda **_kwargs: {
            "name": "葬送的芙莉莲",
            "genre_ids": [16],
            "origin_country": ["JP"],
            "original_language": "ja",
        }
    )
    plugin._tmdb_api_client = types.SimpleNamespace(
        get_info=lambda **_kwargs: {
            "name": "葬送的芙莉莲",
            "genre_ids": [16],
            "origin_country": ["JP"],
            "original_language": "ja",
            "poster_path": "/frieren.jpg",
        }
    )

    metadata = plugin._tmdb_metadata(209867, "tv")

    assert metadata is not None
    assert metadata["title"] == "葬送的芙莉莲"
    assert metadata["genres"] == ["16"]
    assert "frieren.jpg" in metadata["poster_url"]


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


def test_scan_library_forwards_selected_provider_sources() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._scan_paths = ""
    plugin._scan_exclude_paths = ""
    plugin._scan_provider_sources = ["assrt", "xunlei"]
    plugin._notify = False
    captured: dict = {}

    def request_json(method, path, payload, timeout=None):
        captured.update(payload)
        return {"status": "ok", "items": []}

    plugin._request_json = request_json
    plugin.save_data = lambda *_args, **_kwargs: None

    result = asyncio.run(plugin.scan_library())

    assert result["status"] == "ok"
    assert captured["provider_order"] == ["assrt", "xunlei"]


def test_scan_provider_order_accepts_legacy_comma_text() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()

    assert plugin._normalize_provider_order(
        "ASSRT, xunlei,assrt,unsupported"
    ) == ["assrt", "xunlei"]
    assert plugin._normalize_provider_order(
        ["subtitle_best", "custom"]
    ) == ["subtitle_best", "custom"]


def test_scan_provider_sources_migrates_legacy_config() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()

    assert plugin._scan_provider_sources_from_config(
        {"scan_provider_order": ""}
    ) == []
    assert plugin._scan_provider_sources_from_config(
        {"scan_provider_order": "ASSRT,xunlei"}
    ) == ["assrt", "xunlei"]
    assert plugin._scan_provider_sources_from_config(
        {
            "scan_provider_sources": ["subtitle_best"],
            "scan_provider_order": "assrt",
        }
    ) == ["subtitle_best"]


def test_form_uses_multi_select_for_automatic_scan_sources() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._scan_provider_sources = ["assrt"]

    form, defaults = plugin.get_form()
    rendered = json.dumps(form, ensure_ascii=False)

    assert '"model": "scan_provider_sources"' in rendered
    assert '"multiple": true' in rendered
    assert "自动扫描字幕源（可选）" in rendered
    assert "SubtitleBest" in rendered
    assert defaults["scan_provider_sources"] == ["assrt"]


def test_manual_source_prompt_accepts_number_id_and_label() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()

    handler = plugin._manual_onclick("/plex/movie.mkv")

    assert "可输入编号、ID 或名称" in handler
    assert "optionKey" in handler
    assert "labelKey" in handler
    assert "未识别该字幕源" in handler


def test_manual_search_forwards_selected_provider_source() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._provider_order = "xunlei,assrt"
    plugin._scan_provider_sources = ["subtitle_best"]
    captured: dict = {}

    def request_json(method, path, payload, timeout=None):
        captured.update(method=method, path=path, payload=payload)
        return {"status": "ok", "candidates": []}

    plugin._request_json = request_json
    result = asyncio.run(
        plugin.manual_search(
            {
                "video_path": "/plex/movie.mkv",
                "provider_order": ["assrt"],
            }
        )
    )

    assert result["status"] == "ok"
    assert captured["path"] == "/v1/manual/search"
    assert captured["payload"]["provider_order"] == ["assrt"]


def test_manual_search_auto_uses_global_order_not_scan_order() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._provider_order = "xunlei,assrt"
    plugin._scan_provider_sources = ["subtitle_best"]
    captured: dict = {}

    def request_json(method, path, payload, timeout=None):
        captured.update(payload)
        return {"status": "ok", "candidates": []}

    plugin._request_json = request_json
    result = asyncio.run(
        plugin.manual_search({"video_path": "/plex/movie.mkv"})
    )

    assert result["status"] == "ok"
    assert captured["provider_order"] == ["xunlei", "assrt"]


def test_manual_search_rejects_unknown_provider_source() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    called = False

    def request_json(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "ok"}

    plugin._request_json = request_json
    result = asyncio.run(
        plugin.manual_search(
            {
                "video_path": "/plex/movie.mkv",
                "provider_order": ["unsupported"],
            }
        )
    )

    assert result == {"status": "error", "message": "字幕源选择无效"}
    assert called is False


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


def test_queue_enrichment_adds_posters_to_each_group() -> None:
    plugin_module = load_moviepilot_plugin()
    plugin = plugin_module.CSFReborn()
    plugin._enabled = True
    plugin._exclude_domestic_anime = True
    plugin._domestic_anime_regions = "CN,TW,HK"
    plugin._domestic_anime_path_keywords = "国漫,国产动画"
    plugin.chain = types.SimpleNamespace(
        tmdb_info=lambda tmdbid, **_kwargs: {
            "name": f"Media {tmdbid}",
            "genre_ids": [16] if int(tmdbid) == 209867 else [18],
            "origin_country": ["JP"] if int(tmdbid) == 209867 else ["US"],
            "original_language": "ja" if int(tmdbid) == 209867 else "en",
            "poster_path": f"/{tmdbid}.jpg",
        }
    )
    plugin._request_json = lambda *_args, **_kwargs: {
        "status": "ok",
        "queue": {"total": 3, "due": 3, "deferred": 0},
        "queue_groups": {
            "total": 3,
            "due": 3,
            "deferred": 0,
            "groups": [
                {
                    "group_id": "movie",
                    "title": "Movie",
                    "media_type": "movie",
                    "category": "movie",
                    "tmdb_id": 1249289,
                    "poster": None,
                    "item_count": 1,
                    "items": [{"video_path": "/plex/电影/Movie/movie.mkv"}],
                },
                {
                    "group_id": "tv",
                    "title": "Show",
                    "media_type": "tv",
                    "category": "tv",
                    "tmdb_id": 1399,
                    "poster": None,
                    "item_count": 1,
                    "items": [{"video_path": "/plex/电视剧/Show/S01E01.mkv"}],
                },
                {
                    "group_id": "anime",
                    "title": "Frieren",
                    "media_type": "tv",
                    "category": "anime",
                    "tmdb_id": 209867,
                    "poster": None,
                    "item_count": 1,
                    "items": [{"video_path": "/plex/动漫/Frieren/S01E01.mkv"}],
                },
            ],
        },
    }

    result = asyncio.run(plugin.queue())

    groups = result["queue_groups"]["groups"]
    assert [group["group_id"] for group in groups] == ["movie", "tv", "anime"]
    assert all(group["poster"] for group in groups)
    assert "1249289.jpg" in groups[0]["poster"]
    assert "1399.jpg" in groups[1]["poster"]
    assert "209867.jpg" in groups[2]["poster"]
