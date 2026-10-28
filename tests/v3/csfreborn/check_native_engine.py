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


class _ResponseModel:
    """极简 schemas.Response。"""

    def __init__(self, success: bool = True, message: str = "", data=None):
        self.success = success
        self.message = message
        self.data = data


class _MetaInfo:
    """极简 MetaInfo：拆英文名、季集、年份，并去掉发布组信息。"""

    def __init__(self, title: str = "", **kwargs):
        import re as _re

        season_episode = _re.search(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})", title)
        self.begin_season = int(season_episode.group(1)) if season_episode else None
        self.begin_episode = int(season_episode.group(2)) if season_episode else None

        cleaned = _re.sub(r"[._]+", " ", title)
        cleaned = _re.sub(
            r"\b(1080p|2160p|720p|480p|WEB-?DL|WEBRip|BluRay|BDRip|H\.?26[45]|HEVC|"
            r"x26[45]|DDP|DTS|Atmos|HDR10?|REMUX)\b",
            " ",
            cleaned,
            flags=_re.IGNORECASE,
        )
        cleaned = _re.sub(r"[Ss]\d{1,2}\s*[Ee]\d{1,3}", " ", cleaned)
        parts = [
            part
            for part in cleaned.split()
            if not _re.fullmatch(r"[A-Z0-9][A-Z0-9-]{3,}", part)
        ]
        name = " ".join(parts).strip()
        self.cn_name = name or title
        self.name = self.cn_name
        self.en_name = name if name.isascii() else ""
        year = _re.search(r"\b(19|20)\d{2}\b", title)
        self.year = year.group(0) if year else ""


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
    fastapi = types.ModuleType("fastapi")

    class _Body:
        def __init__(self, default=None, **kwargs):
            self.default = default

    fastapi.Body = _Body
    sys.modules["fastapi"] = fastapi

    app = types.ModuleType("app")
    app.__path__ = []
    sys.modules["app"] = app

    def make(name: str, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    make("app.schemas", Response=_ResponseModel)
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



    # 扫描路由：auto 且内置源可用 → 内置扫描
    plugin._native_mode = "auto"
    plugin._service_healthy = lambda: True  # type: ignore[method-assign]
    service_scan_calls: list[str] = []

    async def fake_service_scan(payload):
        service_scan_calls.append("service")
        return {"status": "ok", "discovered": 5, "checked": 5, "downloaded": 1, "items": []}

    plugin._call_service_scan = fake_service_scan  # type: ignore[method-assign]
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
    scan_result = asyncio.run(plugin._run_scan({"paths": ["/tmp"]}))
    check(
        "自动模式内置源优先扫描",
        native_scan_calls == ["native"] and not service_scan_calls and scan_result["downloaded"] == 2,
        f"{native_scan_calls} {service_scan_calls} {scan_result}",
    )

    # 扫描路由：auto 但内置源不可用 → 服务扫描
    plugin._native_engine = types.SimpleNamespace(
        has_source=lambda: False, describe=lambda: []
    )
    service_scan_calls.clear()
    scan_result = asyncio.run(plugin._run_scan({"paths": ["/tmp"]}))
    check(
        "内置源不可用时走服务扫描",
        service_scan_calls == ["service"] and scan_result["downloaded"] == 1,
        f"{service_scan_calls} {scan_result}",
    )


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



def test_queue(module, native, tmp: Path) -> None:
    print("\n[7] 待补全队列与手动下载")

    media = tmp / "queue-media"
    media.mkdir()
    video = media / "Queue.Movie.2024.1080p.mkv"
    video.write_bytes(os.urandom(4096))

    plugin = module.CSFReborn()
    plugin.init_plugin(
        {
            "enabled": True,
            "languages": ["zh-Hans"],
            "native_mode": "native_only",
            "notify": False,
        }
    )

    scan_result = {
        "status": "ok",
        "items": [
            {
                "path": str(video),
                "title": "Queue Movie",
                "year": "2024",
                "media_type": "movie",
                "season": None,
                "episode": None,
                "missing": [native.LANG_CHS],
                "status": "dry_run",
                "downloaded": [],
            }
        ],
    }
    plugin._queue_sync_from_scan(scan_result)
    queue = plugin._load_queue()
    check("扫描后写入队列", len(queue) == 1 and queue[0]["title"] == "Queue Movie", str(queue))
    check("队列条目含键与路径", bool(queue[0].get("key")) and queue[0]["path"] == str(video))

    # 服务端 /v1/scan 的条目结构（video_path / missing_languages）也要能入队
    service_video = media / "Service.Movie.2023.mkv"
    service_video.write_bytes(os.urandom(2048))
    plugin._queue_sync_from_scan(
        {
            "items": [
                {
                    "video_path": str(service_video),
                    "title": "Service Movie",
                    "year": 2023,
                    "media_type": "movie",
                    "status": "queued",
                    "missing_languages": ["zh-Hans"],
                    "errors": [],
                }
            ]
        }
    )
    queue = plugin._load_queue()
    check("服务端条目也能入队", len(queue) == 2, str(len(queue)))

    # 剧集按季分组：同季多集只显示一行
    show_dir = media / "Show"
    show_dir.mkdir()
    episodes = []
    for episode in (1, 2, 3):
        path = show_dir / f"Show.S02E{episode:02d}.1080p.mkv"
        path.write_bytes(os.urandom(1024))
        episodes.append(
            {
                "path": str(path),
                "title": "Show",
                "year": "2024",
                "media_type": "tv",
                "season": 2,
                "episode": episode,
                "missing": [native.LANG_CHS],
                "status": "dry_run",
                "downloaded": [],
            }
        )
    plugin._queue_sync_from_scan({"items": episodes})
    groups_movies, groups_seasons = plugin._queue_groups(plugin._load_queue())
    season_group = next(item for item in groups_seasons if item["title"] == "Show")
    check("剧集按季合并为一行", len(groups_seasons) == 1 and season_group["episodes"] == [1, 2, 3], str(season_group["episodes"]))
    check("电影与剧集分开", len(groups_movies) == 2, str(len(groups_movies)))
    resolved = plugin._resolve_queue_entries(plugin._load_queue(), [season_group["id"]])
    check("勾选整季选中所有集", len(resolved) == 3, str(len(resolved)))

    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    check("页面渲染队列表格", "csf-queue" in page and "待补全" in page, page[:120])
    check("页面分电影与剧集", "电影（" in page and "剧集 · 按季" in page, page[:160])
    check("页面含下载按钮", "下载选中" in page and "queue/download" in page)

    calls: list[str] = []

    async def fake_process(payload):
        calls.append(payload["video_path"])
        return {
            "status": "downloaded",
            "downloaded": [{"path": str(media / "Queue.Movie.2024.1080p.zh-CN.srt"), "language": "zh-Hans", "provider": "迅雷字幕"}],
            "errors": [],
            "missing": [],
        }

    plugin._process_payload = fake_process  # type: ignore[method-assign]

    target = next(entry for entry in queue if entry["title"] == "Queue Movie")
    movie_row_id = "m:" + target["key"]

    async def run_selected():
        response = await plugin.api_queue_download({"keys": [movie_row_id]})
        if plugin._queue_task is not None:
            await plugin._queue_task
        return response

    response = asyncio.run(run_selected())
    check("手动下载接口返回成功", response.success is True and "1" in response.message, response.message)
    check("队列条目被调用", calls == [str(video)], str(calls))
    remaining = plugin._load_queue()
    check(
        "下载成功后移出队列",
        len(remaining) == 4 and all(item["title"] != "Queue Movie" for item in remaining),
        str([item["title"] for item in remaining]),
    )
    check("手动下载写入历史", len(plugin._load_history()) == 1)

    # 失败时保留在队列并累加尝试次数
    async def fake_fail(payload):
        return {"status": "not_found", "downloaded": [], "errors": ["未找到字幕"], "missing": [native.LANG_CHS]}

    plugin._process_payload = fake_fail  # type: ignore[method-assign]
    plugin._queue_sync_from_scan(scan_result)
    queue = plugin._load_queue()

    async def run_retry():
        await plugin.api_queue_download({"all": True})
        if plugin._queue_task is not None:
            await plugin._queue_task

    asyncio.run(run_retry())
    queue = plugin._load_queue()
    retried = next(entry for entry in queue if entry["title"] == "Queue Movie")
    check("失败后仍在队列", len(queue) == 5 and retried["attempts"] == 1, str(len(queue)))
    check("失败原因已记录", "未找到字幕" in (retried.get("reason") or ""), str(retried.get("reason")))

    # 候选级失败写入 notes 而不是 errors
    async def fake_partial(payload):
        return {
            "status": "downloaded",
            "downloaded": [{"path": "/x.srt", "language": "zh-Hans", "provider": "ASSRT"}],
            "errors": [],
            "notes": ["迅雷字幕: 语言不符（繁體中文）"],
            "missing": [],
        }

    plugin._process_payload = fake_partial  # type: ignore[method-assign]
    plugin._queue_sync_from_scan(scan_result)
    async def run_partial():
        await plugin.api_queue_download({"all": True})
        if plugin._queue_task is not None:
            await plugin._queue_task
    asyncio.run(run_partial())
    last = plugin._load_history()[0]
    check("候选失败记入 notes", not (last["result"].get("errors")) and last["result"].get("notes"), str(last["result"]))
    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    check("历史卡片显示提示", "提示:" in page and "语言不符" in page, page[:120])

    diag = asyncio.run(plugin.api_diagnose())
    check("诊断接口可用", diag.success is True and diag.data.get("mode") == plugin._native_mode, str(diag.data)[:120])

    # 文件消失后清理
    still_missing = {
        "path": str(video),
        "title": "Queue Movie",
        "year": "2024",
        "media_type": "movie",
        "missing": [native.LANG_CHS],
        "status": "not_found",
        "downloaded": [],
    }
    ghost = {
        "path": str(media / "gone.mkv"),
        "title": "Gone",
        "missing": [native.LANG_CHS],
        "status": "not_found",
        "downloaded": [],
    }
    plugin._queue_sync_from_scan({"items": [still_missing, ghost]})
    cleared = asyncio.run(plugin.api_queue_clear())
    check("清理失效条目", cleared.success is True and "1" in cleared.message, cleared.message)
    check("仍缺字幕的条目保留", len(plugin._load_queue()) == 1, str(len(plugin._load_queue())))



def test_scan_card(module, tmp: Path) -> None:
    print("\n[8] 扫描卡片文案")

    plugin = module.CSFReborn()
    plugin.init_plugin({"enabled": True, "languages": ["zh-Hans"]})
    plugin.save_data(
        "latest_scan",
        json.dumps(
            {
                "time": "2026-10-28 14:55:50",
                "discovered": 635,
                "checked": 635,
                "existing": 454,
                "queued": 181,
                "queue_remaining": 181,
                "downloaded": 0,
                "downloads_today": 0,
                "daily_download_limit": 10,
                "dry_run": False,
                "items": [
                    {"title": "牧神记", "status": "queued"},
                    {"title": "Alarum", "status": "queued"},
                ],
            },
            ensure_ascii=False,
        ),
    )
    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    check("扫描条目状态中文化", "牧神记: 排队中" in page, page[:200])
    check("未下载时给出提示", "本轮没有下载字幕" in page)



def test_titles_and_posters(module, native, tmp: Path) -> None:
    print("\n[9] 英文标题与 NFO 海报")

    media = tmp / "nfo-media"
    media.mkdir()
    video = media / "Alarum.2025.1080p.WEB-DL.mkv"
    video.write_bytes(os.urandom(2048))
    (media / "movie.nfo").write_text(
        '<?xml version="1.0"?><movie><title>Alarum</title>'
        "<actor><name>Actor One</name>"
        "<thumb>https://image.tmdb.org/t/p/original/actor999.jpg</thumb></actor>"
        '<thumb aspect="poster">https://image.tmdb.org/t/p/original/abc123.jpg</thumb></movie>',
        encoding="utf-8",
    )

    plugin = module.CSFReborn()
    plugin.init_plugin({"enabled": True, "languages": ["zh-Hans"]})

    engine = plugin._native_engine
    req = engine.request_from_payload(
        {"video_path": str(video), "title": "余烬夺宝", "languages": ["zh-Hans"]}
    )
    check("文件名补出英文标题", "Alarum" in (req.en_title or ""), req.en_title)
    check("英文名进入检索词", any("Alarum" in word for word in req.keywords), str(req.keywords))
    check("文件名补出年份", req.year == "2025", req.year)

    plugin._queue_sync_from_scan(
        {
            "items": [
                {
                    "path": str(video),
                    "title": "余烬夺宝",
                    "year": "2025",
                    "media_type": "movie",
                    "missing": ["简体中文"],
                    "status": "not_found",
                    "downloaded": [],
                }
            ]
        }
    )
    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    check("NFO 海报写入队列", "abc123.jpg" in page, page[:160])
    check("不会误用演员头像", "actor999.jpg" not in page)
    saved = plugin._load_queue()[0]
    check("海报已持久化", saved.get("poster", "").endswith("abc123.jpg"), saved.get("poster"))



def test_bdmv_and_inspect(module, native, tmp: Path) -> None:
    print("\n[10] 蓝光分片与字幕检查")

    plugin = module.CSFReborn()
    plugin.init_plugin({"enabled": True, "languages": ["zh-Hans"]})

    disc = tmp / "disc" / "BDMV" / "STREAM"
    disc.mkdir(parents=True)
    segment = disc / "00003.m2ts"
    segment.write_bytes(os.urandom(2048))
    plugin._queue_sync_from_scan(
        {
            "items": [
                {
                    "path": str(segment),
                    "title": "电动维纳斯",
                    "media_type": "movie",
                    "missing": ["简体中文"],
                    "status": "queued",
                    "downloaded": [],
                }
            ]
        }
    )
    check("蓝光分片不入队", plugin._load_queue() == [], str(plugin._load_queue()))

    folder = tmp / "inspect"
    folder.mkdir()
    video = folder / "Movie.2024.1080p.mkv"
    video.write_bytes(os.urandom(2048))
    (folder / "Movie.2024.1080p.zh-CN.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n这是一个中文字幕测试。\n", encoding="utf-8"
    )
    (folder / "Movie.2024.1080p.chs.ass").write_text(
        "[Script Info]\nTitle: test\n\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,另一个中文字幕\n",
        encoding="utf-8",
    )
    result = asyncio.run(plugin.api_inspect(str(video)))
    check("检查接口成功", result.success is True, result.message)
    check("识别到简体字幕", result.data.get("existing") == [native.LANG_CHS], str(result.data.get("existing")))
    names = [item["name"] for item in result.data.get("files") or []]
    check("列出目录内字幕", len(names) == 2 and any(n.endswith(".zh-CN.srt") for n in names), str(names))

    missing = asyncio.run(plugin.api_inspect(str(folder / "nope.mkv")))
    check("检查不存在文件报错", missing.success is False)



def test_title_cleanup(module, native, tmp: Path) -> None:
    print("\n[11] 文件名标题清理")

    plugin = module.CSFReborn()
    plugin.init_plugin({"enabled": True, "languages": ["zh-Hans"]})

    folder = tmp / "cleanup"
    folder.mkdir()
    episode = folder / "如果我是一只动物 S01E43 1080p.FROGWeb.mkv"
    episode.write_bytes(os.urandom(2048))
    movie = folder / "狩猎场 (2025) - 1080p.AVC.DDP 5.1 3Audios.QHstudIo.mkv"
    movie.write_bytes(os.urandom(2048))

    plugin._queue_sync_from_scan(
        {
            "items": [
                {
                    "path": str(episode),
                    "title": episode.stem,
                    "media_type": "movie",
                    "missing": ["简体中文"],
                    "status": "queued",
                    "downloaded": [],
                },
                {
                    "path": str(movie),
                    "title": movie.stem,
                    "media_type": "movie",
                    "missing": ["简体中文"],
                    "status": "queued",
                    "downloaded": [],
                },
            ]
        }
    )
    queue = {item["path"]: item for item in plugin._load_queue()}
    show = queue[str(episode)]
    check("标题去掉发布信息", show["title"] == "如果我是一只动物", show["title"])
    check("从文件名补出季集", show["season"] == 1 and show["episode"] == 43, f"{show['season']}/{show['episode']}")
    check("识别为剧集", show["media_type"] == "tv", show["media_type"])
    check("电影标题清理", queue[str(movie)]["title"] == "狩猎场", queue[str(movie)]["title"])

    movies, seasons = plugin._queue_groups(plugin._load_queue())
    check("剧集归入按季分组", len(seasons) == 1 and seasons[0]["season"] == 1, str(seasons))
    check("电影单独一行", len(movies) == 1, str(movies))



def test_cooldown(native, tmp: Path) -> None:
    print("\n[12] 限流冷却与搜索缓存")

    plugin = FakePlugin(tmp / "cool", _assrt_token="token")
    (tmp / "cool").mkdir(exist_ok=True)
    engine = native.NativeSubtitleEngine(plugin)
    calls: list[str] = []
    original_get = AsyncRequestUtils.get_res

    async def fake_509(self, url, params=None, **kwargs):
        calls.append(url)
        if url.endswith("/sub/search"):
            return _Response(509, b"")
        return _Response(404, b"")

    AsyncRequestUtils.get_res = fake_509
    try:
        req = native.SubtitleRequest(
            video_path=tmp / "cool" / "x.mkv",
            media_type="tv",
            title="牧神记",
            season=1,
            episode=1,
        )
        candidates = asyncio.run(engine.search(req))
        check("限流时返回空结果", candidates == [])
        keys = [source.key for source in engine.active_sources()]
        check("ASSRT 进入冷却", "assrt" not in keys, str(keys))
        described = {row["key"]: row for row in engine.describe()}
        check("诊断显示冷却中", "冷却" in (described["assrt"]["reason"] or ""), described["assrt"]["reason"])
        before = len(calls)
        asyncio.run(engine.search(req))
        check("冷却期间不再请求", len(calls) == before, f"{before} -> {len(calls)}")
    finally:
        AsyncRequestUtils.get_res = original_get

    # 同一关键词只搜一次（同一部剧的多集共用缓存）
    cache: dict = {}
    source = native.AssrtSource(native.SourceConfig(assrt_token="t"), cache)
    keywords: list[str] = []

    async def fake_ok(self, url, params=None, **kwargs):
        if url.endswith("/sub/search"):
            keywords.append(str((params or {}).get("q")))
            body = {
                "status": 0,
                "sub": {
                    "subs": [
                        {
                            "id": 1,
                            "native_name": "牧神记",
                            "videoname": "Mu Shen Ji 2024",
                            "lang": {"langlist": {"langchs": True}},
                        }
                    ]
                },
            }
            return _Response(200, json.dumps(body).encode())
        return _Response(404, b"")

    AsyncRequestUtils.get_res = fake_ok
    try:
        for episode in (1, 2, 3):
            req = native.SubtitleRequest(
                video_path=tmp / "cool" / f"e{episode}.mkv",
                media_type="tv",
                title="牧神记",
                en_title="Mu Shen Ji",
                season=1,
                episode=episode,
            )
            result = asyncio.run(source.search(req))
            check(f"第 {episode} 集命中", len(result) == 1, str(len(result)))
        check("同一关键词只请求一次", len(keywords) == 1, str(keywords))
    finally:
        AsyncRequestUtils.get_res = original_get


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
        test_queue(module, native, tmp)
        test_scan_card(module, tmp)
        test_titles_and_posters(module, native, tmp)
        test_bdmv_and_inspect(module, native, tmp)
        test_title_cleanup(module, native, tmp)
        test_cooldown(native, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
