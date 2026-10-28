"""
ChineseSubFinder 插件的离线自测脚本。

用假的 app.* 模块把插件跑起来，验证：
1. 语言判定 / 标题匹配 / 文件指纹
2. SubtitleBest、Assrt 的响应解析
3. 完整链路：检索 → 下载 → 解压 → 转码 → 命名落盘 → 历史记录 → 去重
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

PLUGIN_PATH = Path(__file__).resolve().parents[3] / "plugins.v3" / "chinesesubfinder" / "__init__.py"

# ---------------------------------------------------------------------------
# 伪造宿主模块
# ---------------------------------------------------------------------------


class MediaType(Enum):
    MOVIE = "电影"
    TV = "电视剧"


class EventType(Enum):
    TransferComplete = "transfer.complete"


class MessageType(Enum):
    Plugin = "插件"


class _EventManager:
    def register(self, etype):
        def decorator(func):
            return func

        return decorator


class _Logger:
    def info(self, msg, *a, **k):
        print(f"  [INFO] {msg}")

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
    """用 urllib 实现的极简异步 HTTP 客户端。"""

    def __init__(self, headers=None, ua=None, cookies=None, proxies=None, timeout=None, **kwargs):
        self.headers = dict(headers or {})
        if ua:
            self.headers.setdefault("User-Agent", ua)
        self.timeout = timeout or 20
        self.calls: list[dict] = []

    async def _request(self, method: str, url: str, params=None, data=None, json_body=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "json": json_body})
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
        return await self._request("POST", url, params=params, data=data, json_body=json, headers=headers)


class _MetaInfo:
    """极简版 MetaInfo：只提供文件名解析后的字段。"""

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
        self._path = Path(tempfile.mkdtemp(prefix="csf-plugin-data-"))

    def get_data_path(self, plugin_id=None):
        return self._path

    def save_data(self, key, value, plugin_id=None):
        self._data[key] = value

    def get_data(self, key=None, plugin_id=None):
        return self._data.get(key)

    def update_config(self, config, plugin_id=None):
        return True

    def post_message(self, **kwargs):
        print(f"  [NOTIFY] {kwargs.get('title')} :: {str(kwargs.get('text')).replace(chr(10), ' | ')}")


def install_fake_host() -> None:
    """把 app.sdk.* 等宿主模块塞进 sys.modules。"""
    app = types.ModuleType("app")
    app.__path__ = []

    def make(name: str, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        return module

    make("app.schemas")
    make("app.schemas.types", EventType=EventType, MediaType=MediaType, MessageType=MessageType)
    make("app.sdk")
    make(
        "app.sdk.config",
        settings=types.SimpleNamespace(
            RMT_SUBEXT=[".srt", ".ass", ".ssa", ".sup"],
            USER_AGENT="csf-test-agent",
            PROXY=None,
        ),
    )
    make("app.sdk.events", eventmanager=_EventManager())
    make("app.sdk.logging", logger=_Logger())
    make("app.sdk.media", MediaInfo=object, MetaBase=object, MetaInfo=_MetaInfo)
    make("app.sdk.network", AsyncRequestUtils=AsyncRequestUtils)
    make("app.sdk.plugin", _PluginBase=_PluginBase)
    sys.modules["app.schemas.types"] = sys.modules["app.schemas.types"]


def load_plugin():
    spec = importlib.util.spec_from_file_location("csf_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["csf_plugin"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


def test_language(plugin) -> None:
    print("\n[1] 语言判定")
    chs = plugin.detect_language("你好，世界。这是一个中文字幕测试。")
    check("简体判定", chs.chinese and chs.variant == plugin.LANG_CHS, str(chs))

    cht = plugin.detect_language("你好，世界。這是一個測試字幕，我們來說明這個問題。")
    check("繁体判定", cht.chinese and cht.variant == plugin.LANG_CHT, str(cht))

    lines = []
    for i in range(20):
        lines.append(f"第{i}行中文字幕内容")
        lines.append(f"English subtitle line number {i}")
    bi = plugin.detect_language("\n".join(lines))
    check("中英双语判定", bi.chinese and bi.bilingual, str(bi))

    en = plugin.detect_language("Just english subtitle text here.")
    check("纯英文判定", (not en.chinese), str(en))

    check("站点标签解析 chs", plugin.lang_from_label("简体&英文").variant == plugin.LANG_CHS)
    check("站点标签解析 cht", plugin.lang_from_label("繁體中文").variant == plugin.LANG_CHT)
    check("站点标签解析 双语", plugin.lang_from_label("中英双语").bilingual)


def test_hashes(plugin, tmp: Path) -> None:
    print("\n[2] 文件指纹")
    video = tmp / "video.mkv"
    payload = os.urandom(200 * 1024)
    video.write_bytes(payload)

    size = len(payload)
    expect_md5 = ";".join(
        hashlib.md5(payload[pos : pos + 4096]).hexdigest()
        for pos in (4 * 1024, int(size / 3 * 2), int(size / 3), size - 8 * 1024)
    )
    check("Shooter md5 指纹", plugin.md5_chunks_hash(video) == expect_md5)

    ctx = hashlib.sha1()
    for pos in (0, int(size / 3), size - 0x5000):
        ctx.update(payload[pos : pos + 0x5000])
    check("迅雷 cid 指纹", plugin.sha1_cid(video) == ctx.hexdigest().upper())


def test_title(plugin) -> None:
    print("\n[3] 标题匹配")
    check("英文名命中", plugin.title_matches(["The Matrix"], "The.Matrix.1999.1080p.BluRay", "1999"))
    check("年份不符否决", not plugin.title_matches(["The Matrix"], "The.Matrix.2003.1080p", "1999"))
    check("中文名命中", plugin.title_matches(["黑镜"], "黑镜.第六季.1080p", "2023"))
    check("无关片名否决", not plugin.title_matches(["The Matrix"], "Inception.2010.1080p", "1999"))

    movie = plugin.SubtitleRequest(
        video_path=Path("/tmp/m.mkv"), media_type="电影", title="流浪地球2", en_title="The Wandering Earth II", year="2023"
    )
    tv = plugin.SubtitleRequest(
        video_path=Path("/tmp/t.mkv"), media_type="电视剧", title="黑镜", en_title="Black Mirror", season=6, episode=1
    )
    check("电影年份不符否决", plugin.subtitle_name_mismatch("Other.Movie.2019.chs.srt", movie))
    check("电影年份相符通过", not plugin.subtitle_name_mismatch("The.Wandering.Earth.II.2023.chs.srt", movie))
    check("剧集集数不符否决", plugin.subtitle_name_mismatch("Black.Mirror.S06E02.chs.srt", tv))
    check("剧集集数相符通过", not plugin.subtitle_name_mismatch("Black.Mirror.S06E01.chs.srt", tv))
    check("strict 无交集否决", plugin.subtitle_name_mismatch("SNIS-497.1080p.srt", tv, strict=True))
    check("strict 有交集通过", not plugin.subtitle_name_mismatch("Black.Mirror.第01集.chs.srt", tv, strict=True))


def test_xunlei(plugin) -> None:
    print("\n[3.5] 迅雷响应容错")
    corrupt = (
        b'{"sublist":[{"scid":"A","sname":"Black.Mirror.S06E01.chs.srt","language":"\xe7\xb9?\xe4\xbd\x93",'
        b'"rate":"5","surl":"http://x/a.srt","svote":9,"roffset":0},'
        b'{"scid":"B","sname":"SNIS-497.1080p.srt","language":"\xe7\xae\x80\xe4\xbd\x93","surl":"http://x/b.srt","svote":50},'
        b"{}]}"
    )
    data = plugin.BaseSource._json(_Response(200, corrupt))
    check("坏字节 JSON 可解析", isinstance(data, dict) and len(data.get("sublist", [])) == 3)

    source = plugin.XunleiSource(_FakePlugin(plugin))
    original_get = AsyncRequestUtils.get_res

    async def fake_get(self, url, params=None, **kwargs):
        return _Response(200, corrupt)

    AsyncRequestUtils.get_res = fake_get
    try:
        import tempfile

        video = Path(tempfile.mkdtemp()) / "Black.Mirror.S06E01.mkv"
        video.write_bytes(os.urandom(80 * 1024))
        req = plugin.SubtitleRequest(
            video_path=video, media_type="电视剧", title="黑镜", en_title="Black Mirror", season=6, episode=1
        )
        cands = asyncio.run(source.search(req))
        check("迅雷过滤无关字幕", len(cands) == 1 and cands[0].url == "http://x/a.srt", str([c.title for c in cands]))
        check("迅雷从文件名识别语言", cands[0].lang_info().variant == plugin.LANG_CHS, cands[0].language)
    finally:
        AsyncRequestUtils.get_res = original_get


def test_subtitle_best(plugin) -> None:
    print("\n[4] SubtitleBest 解析")
    source = plugin.SubtitleBestSource(_FakePlugin(plugin))
    fake_payload = {
        "status": 0,
        "subtitles": [
            {"sub_sha256": "abc", "title": "Movie.2023", "language": 1, "ext": "srt",
             "is_movie": True, "season": 0, "episode": 0, "token": "t1"},
            {"sub_sha256": "def", "title": "Movie.2023", "language": 2, "ext": "ass",
             "is_movie": True, "season": 0, "episode": 0, "token": "t2"},
        ],
    }
    original_post = AsyncRequestUtils.post_res

    async def fake_post(self, url, data=None, json=None, params=None, headers=None, **kwargs):
        if url.endswith("/search-movie"):
            assert headers and headers.get("Authorization", "").startswith("Bearer ")
            return _Response(200, globals()["json"].dumps(fake_payload).encode(), {"X-Daily-Limit": "100"})
        if url.endswith("/get-dl-url"):
            return _Response(200, globals()["json"].dumps({"status": 0, "download_link": "http://x/sub.zip"}).encode())
        return _Response(404, b"")

    AsyncRequestUtils.post_res = fake_post
    try:
        req = plugin.SubtitleRequest(video_path=Path("/tmp/x.mkv"), media_type="电影", title="Movie", imdb_id="tt1234567")
        cands = asyncio.run(source.search(req))
        check("SubtitleBest 返回 2 条", len(cands) == 2, str(len(cands)))
        check("语言映射正确", cands[0].language == plugin.LANG_CHS and cands[1].language == plugin.LANG_CHT)
        check("扩展名补齐点", cands[0].ext == ".srt", cands[0].ext)
        link = asyncio.run(source.resolve(cands[0]))
        check("下载链接解析", link == "http://x/sub.zip", link)
    finally:
        AsyncRequestUtils.post_res = original_post


def test_assrt(plugin) -> None:
    print("\n[5] Assrt 解析")
    source = plugin.AssrtSource(_FakePlugin(plugin))
    original_get = AsyncRequestUtils.get_res

    async def fake_get(self, url, params=None, **kwargs):
        if url.endswith("/sub/search"):
            return _Response(
                200,
                json.dumps(
                    {
                        "status": 0,
                        "sub": {
                            "subs": [
                                {
                                    "id": 111,
                                    "native_name": "黑镜 第六季",
                                    "videoname": "Black.Mirror.S06.1080p",
                                    "vote_score": 12,
                                    "lang": {"langlist": {"langchs": True, "langeng": True}},
                                },
                                {
                                    "id": 222,
                                    "native_name": "别的片子",
                                    "videoname": "Other.Show.2020",
                                    "lang": {"langlist": {"langcht": True}},
                                },
                            ]
                        },
                    }
                ).encode(),
            )
        if url.endswith("/sub/detail"):
            return _Response(200, json.dumps({"status": 0, "sub": {"subs": [{"url": "http://a/1.zip"}]}}).encode())
        return _Response(404, b"")

    AsyncRequestUtils.get_res = fake_get
    try:
        req = plugin.SubtitleRequest(
            video_path=Path("/tmp/x.mkv"),
            media_type="电视剧",
            title="黑镜",
            en_title="Black Mirror",
            year="2023",
            season=6,
            episode=1,
        )
        cands = asyncio.run(source.search(req))
        check("Assrt 过滤掉无关片名", len(cands) == 1, str(len(cands)))
        check("Assrt 语言/双语", cands[0].lang_info().variant == plugin.LANG_CHS and cands[0].lang_info().bilingual, str(cands[0].language))
        link = asyncio.run(source.resolve(cands[0]))
        check("Assrt 详情链接", link == "http://a/1.zip", link)
    finally:
        AsyncRequestUtils.get_res = original_get


def test_pipeline(plugin, tmp: Path) -> None:
    print("\n[6] 完整链路（下载 → 解压 → 落盘）")
    media = tmp / "media"
    media.mkdir()
    video = media / "Black.Mirror.S06E01.1080p.mkv"
    video.write_bytes(os.urandom(120 * 1024))

    # 构造字幕包：GBK 编码的简体字幕
    payload_dir = tmp / "payload"
    payload_dir.mkdir()
    srt_text = "1\n00:00:01,000 --> 00:00:03,000\n这是一个中文字幕测试。\n"
    (payload_dir / "Black.Mirror.S06E01.chs.srt").write_bytes(srt_text.encode("gb18030"))
    zip_path = payload_dir / "sub.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(payload_dir / "Black.Mirror.S06E01.chs.srt", "Black.Mirror.S06E01.chs.srt")

    # 本地 HTTP 服务
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8899", "--directory", str(payload_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.2)
    try:
        inst = plugin.ChineseSubFinder()
        inst.init_plugin(
            {
                "enabled": True,
                "subtitle_langs": [plugin.LANG_CHS],
                "xunlei_enabled": False,
                "subtitle_best_enabled": False,
                "notify": True,
            }
        )

        async def fake_search(req):
            return [
                plugin.SubCandidate(
                    source="xunlei",
                    title="Black Mirror S06E01",
                    language=plugin.LANG_CHS,
                    url="http://127.0.0.1:8899/sub.zip",
                )
            ]

        inst._ChineseSubFinder__search = fake_search
        req = plugin.SubtitleRequest(
            video_path=video,
            media_type="电视剧",
            title="Black Mirror",
            year="2023",
            season=6,
            episode=1,
        )
        ok = asyncio.run(inst._ChineseSubFinder__process_video(req))
        placed = media / "Black.Mirror.S06E01.1080p.zh-CN.srt"
        check("流程返回成功", ok)
        check("字幕按 zh-CN 落盘", placed.exists(), str(list(media.iterdir())))
        if placed.exists():
            raw = placed.read_bytes()
            check("GBK 已转 UTF-8", "这是一个中文字幕测试".encode("utf-8") in raw)
            check("内容正确", "这是一个中文字幕测试" in raw.decode("utf-8"))

        history = inst._ChineseSubFinder__load_history()
        check("历史记录写入", len(history) == 1 and history[0]["success"] is True, str(history))

        # 再次处理：应当识别已有字幕并跳过
        ok2 = asyncio.run(inst._ChineseSubFinder__process_video(req))
        check("已有字幕时跳过", ok2 is False)

        # 语言不符时不落盘
        cht_video = media / "Other.Movie.2023.mkv"
        cht_video.write_bytes(os.urandom(120 * 1024))

        async def fake_search_cht(req):
            return [
                plugin.SubCandidate(
                    source="xunlei",
                    title="Other",
                    language=plugin.LANG_CHT,
                    url="http://127.0.0.1:8899/sub.zip",
                )
            ]

        inst._ChineseSubFinder__search = fake_search_cht
        req2 = plugin.SubtitleRequest(video_path=cht_video, media_type="电影", title="Other", year="2023")
        ok3 = asyncio.run(inst._ChineseSubFinder__process_video(req2))
        check("目标语言不符时不落盘", ok3 is False and not (media / "Other.Movie.2023.zh-CN.srt").exists())

        # 短名字幕（Show.S01E01.srt 对应 Show.S01E01.1080p.mkv）也能被识别
        short_video = media / "Short.Name.2023.1080p.mkv"
        short_video.write_bytes(os.urandom(2048))
        (media / "Short.Name.2023.chs.srt").write_text("这是一个中文字幕测试", encoding="utf-8")
        found = asyncio.run(inst._ChineseSubFinder__scan_existing(short_video))
        check("短名字幕可识别", plugin.LANG_CHS in found, str(found))

        # 语言标签风格
        tmp_sub = media / "tmp_style.srt"
        tmp_sub.write_text("这是一个中文字幕测试", encoding="utf-8")
        inst._tag_style = "plex"
        dest = inst._ChineseSubFinder__place(
            tmp_sub, req, plugin.LangInfo(chinese=True, variant=plugin.LANG_CHS, bilingual=False)
        )
        check("Plex 标签命名", dest is not None and dest.name.endswith(".zh-Hans.srt"), str(dest))
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_ui(plugin) -> None:
    print("\n[7] 配置页 / 详情页 / API 序列化")
    inst = plugin.ChineseSubFinder()
    inst.init_plugin(
        {
            "enabled": True,
            "subtitle_langs": [plugin.LANG_CHS, plugin.LANG_CHT],
            "subtitle_best_api_key": "k",
            "assrt_token": "t",
            "scan_cron": "0 3 * * *",
        }
    )
    form, defaults = inst.get_form()
    check("表单可序列化", bool(json.dumps(form)) and isinstance(defaults, dict))
    check("默认值齐全", set(defaults) >= {"enabled", "subtitle_langs", "scan_cron"})
    check("详情页可序列化", isinstance(json.dumps(inst.get_page()), str))
    check("来源状态可序列化", len(json.dumps(inst.api_sources())) > 2)
    check("历史可序列化", json.dumps(inst.api_history()) == "[]")
    check("定时服务注册", len(inst.get_service()) == 1)
    check("API 列表完整", len(inst.get_api()) == 4)
    inst.init_plugin({"enabled": False})
    check("停用后无定时服务", inst.get_service() == [])


class _FakePlugin:
    """只暴露来源需要的配置属性。"""

    def __init__(self, module):
        self.subtitle_best_api_key = "test-key"
        self.assrt_token = "test-token"
        self._module = module


def main() -> int:
    install_fake_host()
    plugin = load_plugin()
    tmp = Path(tempfile.mkdtemp(prefix="csf-test-"))
    try:
        test_language(plugin)
        test_hashes(plugin, tmp)
        test_title(plugin)
        test_xunlei(plugin)
        test_subtitle_best(plugin)
        test_assrt(plugin)
        test_pipeline(plugin, tmp)
        test_ui(plugin)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
