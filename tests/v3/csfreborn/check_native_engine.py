"""
CSFReborn 内置字幕引擎自测。

用假的 app.* 宿主模块加载插件包，验证：
1. 语言判定 / 标题匹配 / 文件指纹 / 迅雷坏字节容错
2. SubtitleBest、ASSRT 响应解析
3. 引擎完整链路：检索 → 下载 → 解压 → GBK 转码 → 命名落盘 → 去重
4. 插件层编排：内置源与服务结果的合并、扫描摘要结构

直接运行：python3 tests/v3/csfreborn/check_native_engine.py
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import zipfile
from enum import Enum
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / "csfreborn"
INIT_PATH = PLUGIN_DIR / "__init__.py"

PASS = 0
FAIL = 0


# ---------------------------------------------------------------------------
# 伪造宿主模块
# ---------------------------------------------------------------------------


class MediaType(Enum):
    MOVIE = "电影"
    TV = "电视剧"


class EventType(Enum):
    TransferComplete = "transfer.complete"
    PluginAction = "plugin.action"


class MessageType(Enum):
    Plugin = "插件"


class _EventManager:
    def register(self, etype):
        def decorator(func):
            return func

        return decorator


class _Event:
    def __init__(self, event_type=None, event_data=None):
        self.event_type = event_type
        self.event_data = event_data or {}


class _Logger:
    def info(self, msg, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        print(f"  [WARN] {msg}")

    def error(self, msg, *a, **k):
        print(f"  [ERROR] {msg}")

    def debug(self, msg, *a, **k):
        pass


class _Response:
    def __init__(self, status_code: int, content: bytes, headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def json(self):
        return json.loads(self.content)


class AsyncRequestUtils:
    """极简异步 HTTP 客户端（urllib 实现），供测试注入固定响应。"""

    routes: dict[str, object] = {}

    def __init__(self, headers=None, ua=None, cookies=None, proxies=None, timeout=None, **kwargs):
        self.headers = dict(headers or {})
        self.timeout = timeout or 20
        self.calls: list[dict] = []

    async def _request(self, method, url, params=None, data=None, json_body=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json_body})
        import urllib.parse
        import urllib.request

        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = None
        merged = dict(self.headers)
        merged.update(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            merged.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()

        def _do():
            req = urllib.request.Request(url, data=body, headers=merged, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return _Response(resp.status, resp.read(), dict(resp.headers))
            except Exception as err:  # noqa: BLE001
                return _Response(getattr(err, "code", 0) or 0, b"")

        return await asyncio.to_thread(_do)

    async def get_res(self, url, params=None, **kwargs):
        return await self._request("GET", url, params=params, headers=kwargs.get("headers"))

    async def post_res(self, url, data=None, json=None, params=None, headers=None, **kwargs):
        return await self._request(
            "POST", url, params=params, data=data, json_body=json, headers=headers
        )


class _MetaInfo:
    def __init__(self, title: str = "", **kwargs):
        self.cn_name = title
        self.name = title
        self.en_name = ""
        self.year = ""
        self.begin_season = None
        self.begin_episode = None


class _PluginBase:
    def __init__(self):
        self._data: dict = {}

    def get_data(self, key=None, plugin_id=None):
        return self._data.get(key)

    def save_data(self, key, value, plugin_id=None):
        self._data[key] = value

    def update_config(self, config, plugin_id=None):
        return True

    def post_message(self, **kwargs):
        pass

    def get_data_path(self, plugin_id=None):
        return Path(tempfile.gettempdir()) / "csfreborn-plugin-data"


def install_fake_host() -> None:
    app = types.ModuleType("app")
    app.__path__ = []
    sys.modules["app"] = app

    def make(name: str, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    make("app.schemas")
    make(
        "app.schemas.types",
        EventType=EventType,
        MediaType=MediaType,
        MessageType=MessageType,
    )
    make("app.sdk")
    make(
        "app.sdk.config",
        settings=types.SimpleNamespace(
            RMT_SUBEXT=[".srt", ".ass", ".ssa", ".sup"],
            USER_AGENT="csfreborn-test",
            PROXY=None,
        ),
    )
    make("app.sdk.events", eventmanager=_EventManager(), Event=_Event)
    make("app.sdk.logging", logger=_Logger())
    make("app.sdk.media", MetaInfo=_MetaInfo)
    make("app.sdk.network", AsyncRequestUtils=AsyncRequestUtils)
    make("app.sdk.plugin", _PluginBase=_PluginBase)


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "csfreborn", INIT_PATH, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["csfreborn"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


class FakePlugin:
    """只提供内置引擎需要的配置属性。"""

    def __init__(self, tmp: Path, **overrides):
        self._subtitle_best_api_key = ""
        self._subtitle_best_header_token = ""
        self._assrt_token = ""
        self._native_xunlei = True
        self._native_shooter = False
        self._native_tag_style = "iso"
        self._native_auto_sync = False
        self._native_encoding = True
        self._native_max_subs = 2
        self._native_request_delay = 0.0
        self._dir = tmp
        for key, value in overrides.items():
            setattr(self, key, value)

    def get_data_path(self, plugin_id=None) -> Path:
        return self._dir


def test_helpers(native, tmp: Path) -> None:
    print("\n[1] 语言判定 / 标题 / 指纹")

    chs = native.detect_language("你好，世界。这是一个中文字幕测试。")
    check("简体判定", chs.chinese and chs.variant == native.LANG_CHS, str(chs))

    cht = native.detect_language("你好，世界。這是一個測試字幕，我們來說明這個問題。")
    check("繁体判定", cht.chinese and cht.variant == native.LANG_CHT, str(cht))

    lines = []
    for i in range(20):
        lines.append(f"第{i}行中文字幕内容")
        lines.append(f"English subtitle line number {i}")
    bi = native.detect_language("\n".join(lines))
    check("中英双语判定", bi.chinese and bi.bilingual, str(bi))

    check("站点标签 chs", native.lang_from_label("简体&英文").variant == native.LANG_CHS)
    check("站点标签 cht", native.lang_from_label("繁體中文").variant == native.LANG_CHT)
    check("语言代码映射", native.required_langs(["zh-Hans", "zh-Hant"]) == [native.LANG_CHS, native.LANG_CHT])
    check("未知语言代码忽略", native.required_langs(["eng", "zh-CN"]) == [native.LANG_CHS])

    video = tmp / "video.mkv"
    payload = os.urandom(200 * 1024)
    video.write_bytes(payload)
    size = len(payload)
    expect_md5 = ";".join(
        hashlib.md5(payload[pos : pos + 4096]).hexdigest()
        for pos in (4 * 1024, int(size / 3 * 2), int(size / 3), size - 8 * 1024)
    )
    check("Shooter 指纹", native.md5_chunks_hash(video) == expect_md5)

    ctx = hashlib.sha1()
    for pos in (0, int(size / 3), size - 0x5000):
        ctx.update(payload[pos : pos + 0x5000])
    check("迅雷 cid 指纹", native.sha1_cid(video) == ctx.hexdigest().upper())

    check("标题命中", native.title_matches(["The Matrix"], "The.Matrix.1999.1080p", "1999"))
    check("年份否决", not native.title_matches(["The Matrix"], "The.Matrix.2003.1080p", "1999"))
    check("无关片名否决", not native.title_matches(["The Matrix"], "Inception.2010.1080p", "1999"))


def test_name_mismatch(native) -> None:
    print("\n[2] 字幕名与目标校验")

    movie = native.SubtitleRequest(
        video_path=Path("/tmp/m.mkv"),
        media_type="movie",
        title="流浪地球2",
        en_title="The Wandering Earth II",
        year="2023",
    )
    tv = native.SubtitleRequest(
        video_path=Path("/tmp/t.mkv"),
        media_type="tv",
        title="黑镜",
        en_title="Black Mirror",
        season=6,
        episode=1,
    )
    check("电影年份不符", native.subtitle_name_mismatch("Other.Movie.2019.chs.srt", movie))
    check("电影年份相符", not native.subtitle_name_mismatch("The.Wandering.Earth.II.2023.chs.srt", movie))
    check("剧集集数不符", native.subtitle_name_mismatch("Black.Mirror.S06E02.chs.srt", tv))
    check("剧集集数相符", not native.subtitle_name_mismatch("Black.Mirror.S06E01.chs.srt", tv))
    check("strict 无交集否决", native.subtitle_name_mismatch("SNIS-497.1080p.srt", tv, strict=True))
    check("strict 有交集通过", not native.subtitle_name_mismatch("Black.Mirror.第01集.chs.srt", tv, strict=True))


def test_sources(native, tmp: Path) -> None:
    print("\n[3] 来源解析")

    config = native.SourceConfig(subtitle_best_api_key="key", assrt_token="token")

    # SubtitleBest
    sb = native.SubtitleBestSource(config)
    original_post = AsyncRequestUtils.post_res

    async def fake_post(self, url, data=None, params=None, headers=None, **kwargs):
        if url.endswith("/search-movie"):
            assert headers and headers["Authorization"].startswith("Bearer ")
            body = {
                "status": 0,
                "subtitles": [
                    {"sub_sha256": "a", "title": "Movie", "language": 1, "ext": "srt",
                     "is_movie": True, "season": 0, "episode": 0, "token": "t1"},
                    {"sub_sha256": "b", "title": "Movie", "language": 2, "ext": "ass",
                     "is_movie": True, "season": 0, "episode": 0, "token": "t2"},
                ],
            }
            return _Response(200, json.dumps(body).encode())
        if url.endswith("/get-dl-url"):
            return _Response(200, json.dumps({"status": 0, "download_link": "http://x/sub.zip"}).encode())
        return _Response(404, b"")

    AsyncRequestUtils.post_res = fake_post
    try:
        req = native.SubtitleRequest(
            video_path=Path("/tmp/x.mkv"), media_type="movie", title="Movie", imdb_id="tt1234567"
        )
        cands = asyncio.run(sb.search(req))
        check("SubtitleBest 解析", len(cands) == 2 and cands[0].language == native.LANG_CHS)
        check("SubtitleBest 扩展名", cands[1].ext == ".ass", cands[1].ext)
        check("SubtitleBest 取直链", asyncio.run(sb.resolve(cands[0])) == "http://x/sub.zip")
    finally:
        AsyncRequestUtils.post_res = original_post

    # ASSRT
    assrt = native.AssrtSource(config)
    original_get = AsyncRequestUtils.get_res

    async def fake_get(self, url, params=None, **kwargs):
        if url.endswith("/sub/search"):
            body = {
                "status": 0,
                "sub": {
                    "subs": [
                        {"id": 1, "native_name": "黑镜 第六季", "videoname": "Black.Mirror.S06.1080p",
                         "vote_score": 12, "lang": {"langlist": {"langchs": True, "langeng": True}}},
                        {"id": 2, "native_name": "别的片子", "videoname": "Other.Show.2020",
                         "lang": {"langlist": {"langcht": True}}},
                    ]
                },
            }
            return _Response(200, json.dumps(body).encode())
        if url.endswith("/sub/detail"):
            return _Response(200, json.dumps({"status": 0, "sub": {"subs": [{"url": "http://a/1.zip"}]}}).encode())
        return _Response(404, b"")

    AsyncRequestUtils.get_res = fake_get
    try:
        req = native.SubtitleRequest(
            video_path=Path("/tmp/x.mkv"), media_type="tv", title="黑镜",
            en_title="Black Mirror", year="2023", season=6, episode=1,
        )
        cands = asyncio.run(assrt.search(req))
        check("ASSRT 过滤无关片名", len(cands) == 1, str(len(cands)))
        check("ASSRT 语言判定", cands[0].lang_info().variant == native.LANG_CHS)
        check("ASSRT 取详情链接", asyncio.run(assrt.resolve(cands[0])) == "http://a/1.zip")
    finally:
        AsyncRequestUtils.get_res = original_get

    # 迅雷：坏字节 + 无关字幕过滤
    corrupt = (
        b'{"sublist":[{"scid":"A","sname":"Black.Mirror.S06E01.chs.srt","language":"\xe7\xb9?\xe4\xbd\x93",'
        b'"rate":"5","surl":"http://x/a.srt","svote":9},'
        b'{"scid":"B","sname":"SNIS-497.1080p.srt","language":"\xe7\xae\x80\xe4\xbd\x93","surl":"http://x/b.srt","svote":50},'
        b"{}]}"
    )
    data = native.BaseSource._json(_Response(200, corrupt))
    check("坏字节 JSON 可解析", isinstance(data, dict) and len(data["sublist"]) == 3)

    xunlei = native.XunleiSource(config)
    video = tmp / "Black.Mirror.S06E01.mkv"
    video.write_bytes(os.urandom(80 * 1024))
    original_get = AsyncRequestUtils.get_res

    async def fake_xunlei(self, url, params=None, **kwargs):
        return _Response(200, corrupt)

    AsyncRequestUtils.get_res = fake_xunlei
    try:
        req = native.SubtitleRequest(
            video_path=video, media_type="tv", title="黑镜",
            en_title="Black Mirror", season=6, episode=1,
        )
        cands = asyncio.run(xunlei.search(req))
        check("迅雷过滤无关字幕", len(cands) == 1 and cands[0].url == "http://x/a.srt", str(len(cands)))
        check("迅雷按文件名识别语言", cands[0].lang_info().variant == native.LANG_CHS)
    finally:
        AsyncRequestUtils.get_res = original_get


def test_engine(native, tmp: Path) -> None:
    print("\n[4] 引擎完整链路")

    media = tmp / "media"
    media.mkdir()
    video = media / "Black.Mirror.S06E01.1080p.mkv"
    video.write_bytes(os.urandom(120 * 1024))

    payload_dir = tmp / "payload"
    payload_dir.mkdir()
    srt_text = "1\n00:00:01,000 --> 00:00:03,000\n这是一个中文字幕测试。\n"
    (payload_dir / "Black.Mirror.S06E01.chs.srt").write_bytes(srt_text.encode("gb18030"))
    with zipfile.ZipFile(payload_dir / "sub.zip", "w") as zf:
        zf.write(payload_dir / "Black.Mirror.S06E01.chs.srt", "Black.Mirror.S06E01.chs.srt")

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8898", "--directory", str(payload_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)
    try:
        plugin = FakePlugin(tmp / "plugindata")
        (tmp / "plugindata").mkdir(exist_ok=True)
        engine = native.NativeSubtitleEngine(plugin)
        check("无凭据时仅迅雷可用", engine.has_source() and len(engine.active_sources()) == 1)

        async def fake_search(req):
            return [
                native.SubCandidate(
                    source="xunlei",
                    title="Black Mirror S06E01",
                    language=native.LANG_CHS,
                    url="http://127.0.0.1:8898/sub.zip",
                )
            ]

        engine.search = fake_search  # type: ignore[method-assign]

        payload = {
            "video_path": str(video),
            "title": "Black Mirror",
            "media_type": "tv",
            "season": 6,
            "episode": 1,
            "languages": ["zh-Hans"],
        }
        result = asyncio.run(engine.process(payload))
        placed = media / "Black.Mirror.S06E01.1080p.zh-CN.srt"
        check("process 状态", result["status"] == "downloaded", str(result))
        check("字幕按 zh-CN 落盘", placed.exists(), str(list(media.iterdir())))
        check("结果结构含 path/language/provider", bool(result["downloaded"])
              and set(result["downloaded"][0]) == {"path", "language", "provider"})
        if placed.exists():
            check("GBK 转 UTF-8", "这是一个中文字幕测试" in placed.read_text(encoding="utf-8"))

        again = asyncio.run(engine.process(payload))
        check("已有字幕时跳过", again["status"] == "ok" and not again["downloaded"], str(again))

        # 语言不符：目标繁体，只有简体资源
        cht_video = media / "Other.Movie.2023.mkv"
        cht_video.write_bytes(os.urandom(120 * 1024))
        cht_payload = {
            "video_path": str(cht_video),
            "title": "Other",
            "media_type": "movie",
            "year": 2023,
            "languages": ["zh-Hant"],
        }
        bad = asyncio.run(engine.process(cht_payload))
        check("语言不符不落盘", bad["status"] == "not_found" and not bad["downloaded"], str(bad))
        check("语言不符报告缺失", bad["missing"] == [native.LANG_CHT], str(bad.get("missing")))

        # dry_run
        dry = asyncio.run(engine.process({**cht_payload, "dry_run": True}))
        check("dry_run 不落盘", dry["status"] == "dry_run" and not dry["downloaded"])

        # 扫描摘要
        scan = asyncio.run(
            engine.scan(
                {
                    "paths": [str(media)],
                    "languages": ["zh-Hans"],
                    "max_files": 5,
                    "daily_download_limit": 5,
                    "dry_run": False,
                }
            )
        )
        for key in ("status", "discovered", "checked", "existing", "queued", "downloaded", "items"):
            check(f"扫描摘要含 {key}", key in scan)
        check("扫描统计正确", scan["discovered"] == 2 and scan["existing"] == 1, str(scan))
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_orchestration(module, native, tmp: Path) -> None:
    print("\n[5] 插件层编排")

    plugin = module.CSFReborn()
    plugin.init_plugin(
        {
            "enabled": True,
            "languages": ["zh-Hans"],
            "subtitle_best_api_key": "key",
            "scan_paths": str(tmp),
        }
    )
    check("引擎已实例化", plugin._native_engine is not None)
    check("默认模式为自动", plugin._native_mode == "auto")
    check("内置源可用", plugin._native_ready is True)
    check("来源描述完整", len(plugin._native_engine.describe()) == 4)
    check("表单可序列化", bool(json.dumps(plugin.get_form())))
    check("详情页可序列化", bool(json.dumps(plugin.get_page())))

    merged = plugin._merge_process_results(
        {"status": "not_found", "downloaded": [], "errors": [], "missing": ["繁體中文"]},
        {"status": "downloaded", "downloaded": [{"path": "/a.srt", "language": "zh-Hant", "provider": "x"}], "errors": []},
    )
    check("合并结果取并集", merged["status"] == "downloaded" and len(merged["downloaded"]) == 1, str(merged))

    empty = plugin._merge_process_results(None, None)
    check("无来源时报错", empty["status"] == "error")

    scan_merged = plugin._merge_scan_results(
        {"status": "ok", "discovered": 10, "checked": 10, "downloaded": 2, "items": [{"title": "a"}]},
        {"status": "ok", "discovered": 8, "checked": 8, "downloaded": 1, "items": [{"title": "b"}]},
    )
    check("扫描合并不重复计数", scan_merged["discovered"] == 10 and scan_merged["downloaded"] == 3, str(scan_merged))

    # 仅内置源模式：服务不应被调用
    plugin._native_mode = "native_only"
    calls: list[str] = []

    class _Engine:
        def has_source(self):
            return True

        async def process(self, payload):
            calls.append("native")
            return {"status": "downloaded", "downloaded": [{"path": "/x.srt", "language": "zh-Hans", "provider": "x"}], "errors": [], "missing": []}

        async def scan(self, payload):
            calls.append("native-scan")
            return {"status": "ok", "discovered": 1, "checked": 1, "downloaded": 1, "items": []}

        def describe(self):
            return []

    plugin._native_engine = _Engine()
    result = asyncio.run(plugin._process_payload({"video_path": "/tmp/x.mkv", "title": "X"}))
    check("仅内置源时调用内置", calls == ["native"] and result["status"] == "downloaded", str(calls))

    # 自动模式：内置源仍缺语言时补调服务
    plugin._native_mode = "auto"
    service_calls: list[str] = []

    class _Engine2(_Engine):
        async def process(self, payload):
            return {"status": "not_found", "downloaded": [], "errors": [], "missing": ["繁體中文"]}

    async def fake_service(payload):
        service_calls.append("service")
        return {"status": "downloaded", "downloaded": [{"path": "/y.srt", "language": "zh-Hant", "provider": "s"}], "errors": []}

    plugin._native_engine = _Engine2()
    plugin._call_service_process = fake_service  # type: ignore[method-assign]
    result = asyncio.run(plugin._process_payload({"video_path": "/tmp/x.mkv", "title": "X"}))
    check("自动模式不足时调服务", service_calls == ["service"] and result["status"] == "downloaded", str(result))

    # 自动模式：内置源已满足则不调服务
    service_calls.clear()

    class _Engine3(_Engine):
        pass

    plugin._native_engine = _Engine3()
    asyncio.run(plugin._process_payload({"video_path": "/tmp/x.mkv", "title": "X"}))
    check("内置源满足时不调服务", service_calls == [], str(service_calls))



    # 扫描路由：服务健康时沿用服务扫描
    plugin._native_mode = "auto"
    plugin._service_healthy = lambda: True  # type: ignore[method-assign]
    service_scan_calls: list[str] = []

    async def fake_service_scan(payload):
        service_scan_calls.append("service")
        return {"status": "ok", "discovered": 5, "checked": 5, "downloaded": 1, "items": []}

    plugin._call_service_scan = fake_service_scan  # type: ignore[method-assign]
    scan_result = asyncio.run(plugin._run_scan({"paths": ["/tmp"]}))
    check("自动模式服务健康时走服务扫描", service_scan_calls == ["service"] and scan_result["downloaded"] == 1, str(scan_result))

    # 扫描路由：服务不可用时走内置扫描
    plugin._service_healthy = lambda: False  # type: ignore[method-assign]
    native_scan_calls: list[str] = []

    class _EngineScan:
        def has_source(self):
            return True

        async def scan(self, payload):
            native_scan_calls.append("native")
            return {"status": "ok", "discovered": 3, "checked": 3, "downloaded": 2, "items": []}

        def describe(self):
            return []

    plugin._native_engine = _EngineScan()
    service_scan_calls.clear()
    scan_result = asyncio.run(plugin._run_scan({"paths": ["/tmp"]}))
    check("服务不可用时走内置扫描", native_scan_calls == ["native"] and not service_scan_calls and scan_result["downloaded"] == 2, str(scan_result))

def test_transfer_event(module, native, tmp: Path) -> None:
    print("\n[6] 整理完成事件全链路")

    media = tmp / "event-media"
    media.mkdir()
    video = media / "Show.S01E02.1080p.mkv"
    video.write_bytes(os.urandom(100 * 1024))

    payload_dir = tmp / "event-payload"
    payload_dir.mkdir()
    (payload_dir / "Show.S01E02.chs.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n这是一个中文字幕测试。\n", encoding="utf-8"
    )
    with zipfile.ZipFile(payload_dir / "show.zip", "w") as zf:
        zf.write(payload_dir / "Show.S01E02.chs.srt", "Show.S01E02.chs.srt")

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8897", "--directory", str(payload_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)
    try:
        plugin = module.CSFReborn()
        plugin.init_plugin(
            {"enabled": True, "languages": ["zh-Hans"], "native_mode": "native_only", "notify": False}
        )
        plugin._native_engine._dir = tmp / "event-plugin-data"
        (tmp / "event-plugin-data").mkdir(exist_ok=True)

        async def fake_search(req):
            return [
                native.SubCandidate(
                    source="xunlei",
                    title="Show S01E02",
                    language=native.LANG_CHS,
                    url="http://127.0.0.1:8897/show.zip",
                )
            ]

        plugin._native_engine.search = fake_search  # type: ignore[method-assign]

        event = types.SimpleNamespace(
            event_data={
                "mediainfo": types.SimpleNamespace(
                    title="Show", en_title="Show", year="2023", type="电视剧", imdb_id="tt1"
                ),
                "meta": types.SimpleNamespace(begin_season=1, begin_episode=2),
                "transferinfo": types.SimpleNamespace(
                    file_list_new=[str(video)],
                    target_diritem=types.SimpleNamespace(path=str(media)),
                    target_item=types.SimpleNamespace(path=str(video)),
                ),
            }
        )
        asyncio.run(plugin.on_transfer_complete(event))

        placed = media / "Show.S01E02.1080p.zh-CN.srt"
        check("事件触发后字幕落盘", placed.exists(), str(list(media.iterdir())))
        history = plugin._load_history()
        check("事件写入历史", len(history) == 1 and history[0]["title"] == "Show", str(history[:1]))
        check("历史状态为已下载", history[0]["result"]["status"] == "downloaded" if history else False)
        check("详情页含记录", "Show" in json.dumps(plugin.get_page(), ensure_ascii=False))
    finally:
        server.terminate()
        server.wait(timeout=5)


def main() -> int:
    install_fake_host()
    module = load_plugin()
    native = sys.modules["csfreborn.native"]
    tmp = Path(tempfile.mkdtemp(prefix="csfreborn-test-"))
    try:
        test_helpers(native, tmp)
        test_name_mismatch(native)
        test_sources(native, tmp)
        test_engine(native, tmp)
        test_orchestration(module, native, tmp)
        test_transfer_event(module, native, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
