"""In-process subtitle engine for CSFReborn.

Implements the ChineseSubFinder subtitle sources (SubtitleBest, ASSRT, Xunlei,
Shooter) directly inside the MoviePilot plugin process, so the plugin keeps
working when the SubtitleForge service is not deployed or is temporarily
unreachable.

Return values use the same JSON shape as the SubtitleForge service API
(`/v1/process` and `/v1/scan`) so the plugin's history page, notifications and
scan summary keep working unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

try:  # MoviePilot V3
    from app.sdk.config import settings
    from app.sdk.logging import logger
    from app.sdk.media import MetaInfo
    from app.sdk.network import AsyncRequestUtils
except ImportError:  # MoviePilot V2
    from app.core.config import settings
    from app.core.metainfo import MetaInfo
    from app.log import logger
    from app.utils.http import AsyncRequestUtils

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

LANG_CHS = "简体中文"
LANG_CHT = "繁體中文"
LANG_EN = "英文"
LANG_UNKNOWN = "未知"

# 站点语言代码 → 内部语言
LANG_ALIASES = {
    "zh-hans": LANG_CHS,
    "zh-cn": LANG_CHS,
    "zh-sg": LANG_CHS,
    "chs": LANG_CHS,
    "sc": LANG_CHS,
    "chi_sim": LANG_CHS,
    "zh-hant": LANG_CHT,
    "zh-tw": LANG_CHT,
    "zh-hk": LANG_CHT,
    "cht": LANG_CHT,
    "tc": LANG_CHT,
    "chi_tra": LANG_CHT,
    "简体中文": LANG_CHS,
    "简体": LANG_CHS,
    "繁體中文": LANG_CHT,
    "繁体中文": LANG_CHT,
    "繁體": LANG_CHT,
    "繁体": LANG_CHT,
}

TRAD_CHARS = set(
    "說見時會過個們來學開關頭門體國對發現後經麼樣點從當還沒給讓問聽"
    "實際報長東動裡種類華機產萬話風電業處際識達爾羅馬魚鳥龍書愛專門難"
    "擊變聲讀寫買賣進遠運區彆麵願媽爺貓豬車軍農邊號歲與專業務員"
    "單輪轉軟體網路檔案資訊電影節目錄製畫質選擇確定取消開始結束"
)

SUBTITLE_BEST_ROOT = "https://api.subtitle.best/share-sub/v1"
SUBTITLE_BEST_TOKEN = "5akwmGAbuFWqgaZf9QwT"
SUBTITLE_BEST_LANG = {1: LANG_CHS, 2: LANG_CHT}

ASSRT_ROOT = "https://api.assrt.net/v1"
XUNLEI_ROOT = "http://sub.xmp.sandai.net:8000/subxl/%s.json"
SHOOTER_ROOT = "https://www.shooter.cn/api/subapi.php"

TAG_STYLES = {
    "iso": {LANG_CHS: "zh-CN", LANG_CHT: "zh-TW", LANG_EN: "en"},
    "legacy": {LANG_CHS: "chs", LANG_CHT: "cht", LANG_EN: "eng"},
    "plex": {LANG_CHS: "zh-Hans", LANG_CHT: "zh-Hant", LANG_EN: "en"},
}

SOURCE_LABEL = {
    "subtitle_best": "SubtitleBest",
    "assrt": "ASSRT",
    "xunlei": "迅雷字幕",
    "shooter": "Shooter",
}

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv",
    ".flv", ".webm", ".iso", ".rmvb", ".m4v",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class LangInfo:
    """字幕语言判定结果。"""

    chinese: bool = False
    variant: str = ""
    bilingual: bool = False

    @property
    def display(self) -> str:
        if not self.chinese:
            return LANG_EN if self.bilingual else LANG_UNKNOWN
        return self.variant or LANG_UNKNOWN

    @property
    def api_code(self) -> str:
        """SubtitleForge 使用的语言代码。"""

        return "zh-Hant" if self.variant == LANG_CHT else "zh-Hans"

    def covers(self, required: Iterable[str]) -> bool:
        return self.chinese and bool(self.variant) and self.variant in required


@dataclass
class SubtitleRequest:
    """一次字幕检索的输入。"""

    video_path: Path
    media_type: str = "movie"
    title: str = ""
    en_title: str = ""
    aliases: list[str] = field(default_factory=list)
    year: str = ""
    imdb_id: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None
    overwrite: bool = False

    @property
    def is_tv(self) -> bool:
        return self.media_type == "tv" or self.episode is not None

    @property
    def keywords(self) -> list[str]:
        result: list[str] = []
        for name in [self.title, *self.aliases, self.en_title]:
            name = (name or "").strip()
            if len(name) < 2 or name in result:
                continue
            result.append(name)
        return result[:4]


@dataclass
class SubCandidate:
    """一条候选字幕。"""

    source: str
    title: str
    language: str = LANG_UNKNOWN
    url: str = ""
    ext: str = ""
    score: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)
    resolve: str = ""

    def lang_info(self) -> LangInfo:
        return lang_from_label(self.language)


@dataclass
class SourceConfig:
    """字幕来源凭据（来自插件已有配置，用户无需额外填写）。"""

    subtitle_best_api_key: str = ""
    subtitle_best_header_token: str = SUBTITLE_BEST_TOKEN
    assrt_token: str = ""
    xunlei_enabled: bool = True
    shooter_enabled: bool = False


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def md5_chunks_hash(path: Path) -> str:
    """Shooter 指纹：4 段 4KB 采样的 MD5，用 ; 连接。"""

    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size < 0xF000:
        return ""
    positions = (4 * 1024, int(size / 3 * 2), int(size / 3), size - 8 * 1024)
    parts: list[str] = []
    try:
        with path.open("rb") as fp:
            for pos in positions:
                fp.seek(pos)
                parts.append(hashlib.md5(fp.read(4 * 1024)).hexdigest())
    except OSError:
        return ""
    return ";".join(parts)


def sha1_cid(path: Path) -> str:
    """迅雷 cid：3 段 20KB 采样拼接后的 SHA1（大写）。"""

    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size < 0xF000:
        return ""
    buffer_size = 0x5000
    ctx = hashlib.sha1()
    try:
        with path.open("rb") as fp:
            for pos in (0, int(size / 3), size - buffer_size):
                fp.seek(pos)
                ctx.update(fp.read(buffer_size))
    except OSError:
        return ""
    return ctx.hexdigest().upper()


def lang_from_label(label: str) -> LangInfo:
    """把站点返回的语言标签转成 LangInfo。"""

    text = (label or "").strip().lower()
    if not text:
        return LangInfo()

    def has(*tokens: str) -> bool:
        return any(token in text for token in tokens)

    if not has("中", "简", "繁", "chs", "cht", "chi", "zho", "zh", "cn", "tw", "hk", "双语", "雙語", "bilingual"):
        return LangInfo(chinese=False, bilingual=has("eng", "english", "英文"))

    if has("繁", "cht", "chi_tra", "chi-tra", "zh-tw", "zh_tw", "zh-hant", "zh_hant", "hant", "big5"):
        variant = LANG_CHT
    else:
        variant = LANG_CHS

    bilingual = has("双语", "雙語", "简英", "繁英", "中英", "bilingual", "chs&eng", "cht&eng")
    return LangInfo(chinese=True, variant=variant, bilingual=bilingual)


def detect_language(text: str) -> LangInfo:
    """从字幕正文判定语言：繁体特征字占比 + 中英双语行占比。"""

    if not text:
        return LangInfo()

    han_total = 0
    trad_total = 0
    han_lines = 0
    en_lines = 0

    for line in text.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit():
            continue
        plain = re.sub(r"\{[^}]*\}|<[^>]*>|\\[Nn]", " ", line)
        han = [ch for ch in plain if "\u4e00" <= ch <= "\u9fff"]
        if han:
            han_lines += 1
            han_total += len(han)
            trad_total += sum(1 for ch in han if ch in TRAD_CHARS)
        elif re.search(r"[A-Za-z]{3,}", plain):
            en_lines += 1

    if han_lines == 0:
        return LangInfo(chinese=False, bilingual=bool(en_lines))

    variant = LANG_CHS
    if trad_total >= 5 and trad_total / max(han_total, 1) > 0.03:
        variant = LANG_CHT

    return LangInfo(
        chinese=True,
        variant=variant,
        bilingual=en_lines >= max(3, int(han_lines * 0.15)),
    )


def decode_subtitle_bytes(data: bytes) -> str:
    """字幕文本解码：UTF-8 优先，其次 GB18030 / BIG5。"""

    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def normalize_title(text: str) -> str:
    text = unquote(text or "").lower()
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def title_matches(names: Iterable[str], candidate: str, year: str = "") -> bool:
    """标题归一化后互相包含即算命中；候选年份冲突则否决。"""

    cand_norm = normalize_title(candidate)
    if not cand_norm:
        return True

    cand_year = re.search(r"\b(19|20)\d{2}\b", candidate or "")
    if year and cand_year and cand_year.group(0) != str(year):
        return False

    for name in names:
        name_norm = normalize_title(name)
        if len(name_norm) < 2:
            continue
        if name_norm in cand_norm or cand_norm in name_norm:
            return True
    return False


def subtitle_name_mismatch(name: str, req: SubtitleRequest, strict: bool = False) -> bool:
    """判断字幕文件名是否明显属于别的剧集/影片。"""

    text = (name or "").lower()
    if not text:
        return False

    has_episode_marker = False
    if req.is_tv and req.episode is not None:
        season = req.season or 1
        pairs = re.findall(r"s(\d{1,2})[ ._-]?e(\d{1,3})", text)
        if pairs:
            has_episode_marker = True
            if not any(int(s) == season and int(e) == req.episode for s, e in pairs):
                return True
        else:
            episodes = [int(x) for x in re.findall(r"(?:^|[^0-9a-z])e(?:p)?[ ._-]?(\d{1,3})(?![0-9])", text)]
            episodes += [int(x) for x in re.findall(r"第\s*(\d{1,3})\s*[集话話]", text)]
            if episodes:
                has_episode_marker = True
                if req.episode not in episodes:
                    return True

    has_year = False
    if req.year:
        years = re.findall(r"(?:19|20)\d{2}", text)
        has_year = bool(years)
        if years and not req.is_tv and str(req.year) not in years:
            return True

    if strict and not has_episode_marker and not has_year:
        tokens = [token for token in (req.title, req.en_title) if token]
        if tokens and not title_matches(tokens, name, ""):
            return True
    return False


def guess_ext(url: str, fallback: str = "") -> str:
    path = urlparse(url or "").path
    ext = Path(unquote(path)).suffix.lower()
    if ext in {".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub", ".zip", ".rar", ".7z"}:
        return ext
    return fallback


def sniff_ext(data: bytes) -> str:
    if data[:2] == b"PK":
        return ".zip"
    if data[:4] == b"Rar!":
        return ".rar"
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return ".7z"
    head = data[:4096].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith(b"[Script Info]") or head.startswith(b"[V4+ Styles]"):
        return ".ass"
    if head.startswith(b"WEBVTT"):
        return ".vtt"
    if head[:6] == b"\x00\x00\x01\xba" or head[:4] == b"PG\x00\x00":
        return ".sup"
    return ".srt"


# ---------------------------------------------------------------------------
# 字幕来源适配器
# ---------------------------------------------------------------------------


class BaseSource:
    """字幕来源基类：search 返回候选，resolve 返回可下载直链。"""

    key = ""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @property
    def label(self) -> str:
        return SOURCE_LABEL.get(self.key, self.key)

    def client(self, timeout: float = 30) -> AsyncRequestUtils:
        return AsyncRequestUtils(
            ua=settings.USER_AGENT,
            proxies=getattr(settings, "PROXY", None) or None,
            timeout=timeout,
        )

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        raise NotImplementedError

    async def resolve(self, cand: SubCandidate) -> str:
        return cand.url

    @staticmethod
    def _json(resp: Any) -> Any:
        """容错解析 JSON（部分站点会返回夹杂坏字节的 UTF-8）。"""

        if not resp or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except Exception:
            pass
        raw = getattr(resp, "content", b"") or b""
        for encoding in ("utf-8", "gb18030"):
            try:
                return json.loads(raw.decode(encoding, errors="replace"))
            except Exception:
                continue
        return None


class SubtitleBestSource(BaseSource):
    """SubtitleBest：按 IMDb 检索，支持电影 / 单集 / 整季包。"""

    key = "subtitle_best"

    async def _post(self, client: AsyncRequestUtils, path: str, body: dict) -> dict:
        resp = await client.post_res(
            f"{SUBTITLE_BEST_ROOT}{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {self.config.subtitle_best_header_token}",
                "Content-Type": "application/json",
            },
        )
        data = self._json(resp)
        if not isinstance(data, dict):
            return {}
        if data.get("status") not in (0, None):
            logger.warning(f"SubtitleBest {path} 返回异常：{data.get('message')}")
            return {}
        return data

    def _candidates(
        self, subs: Iterable[dict], req: SubtitleRequest, package_id: str = ""
    ) -> list[SubCandidate]:
        result: list[SubCandidate] = []
        for item in subs or []:
            try:
                lang_code = int(item.get("language") or 0)
            except (TypeError, ValueError):
                lang_code = 0
            ext = str(item.get("ext") or "").strip()
            if ext and not ext.startswith("."):
                ext = f".{ext}"
            result.append(
                SubCandidate(
                    source=self.key,
                    title=str(item.get("title") or req.title or req.video_path.stem),
                    language=SUBTITLE_BEST_LANG.get(lang_code, LANG_UNKNOWN),
                    ext=ext,
                    resolve="subtitle_best",
                    payload={
                        "sub_sha256": item.get("sub_sha256"),
                        "is_movie": bool(item.get("is_movie")),
                        "season": item.get("season") or (req.season or 0),
                        "episode": item.get("episode") or (req.episode or 0),
                        "language": lang_code,
                        "token": item.get("token"),
                        "season_package_id": package_id,
                        "imdb_id": req.imdb_id,
                    },
                )
            )
        return result

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        api_key = (self.config.subtitle_best_api_key or "").strip()
        imdb = (req.imdb_id or "").strip()
        if not api_key or not imdb.startswith("tt"):
            return []

        client = self.client()
        try:
            if req.is_tv and req.season is not None and req.episode is not None:
                data = await self._post(
                    client,
                    "/search-tv-eps",
                    {
                        "imdb_id": imdb,
                        "season": int(req.season),
                        "episode": int(req.episode),
                        "api_key": api_key,
                    },
                )
                cands = self._candidates(data.get("subtitles") or [], req)
                if cands:
                    return cands
                packages = await self._post(
                    client,
                    "/search-tv-season-package",
                    {"imdb_id": imdb, "season": int(req.season), "api_key": api_key},
                )
                for package_id in (packages.get("season_package_ids") or [])[:2]:
                    detail = await self._post(
                        client,
                        "/search-tv-season-package-id",
                        {
                            "imdb_id": imdb,
                            "season_package_id": package_id,
                            "api_key": api_key,
                        },
                    )
                    cands.extend(
                        self._candidates(
                            detail.get("subtitles") or [], req, package_id=package_id
                        )
                    )
                return cands

            data = await self._post(
                client, "/search-movie", {"imdb_id": imdb, "api_key": api_key}
            )
            return self._candidates(data.get("subtitles") or [], req)
        except Exception as err:  # noqa: BLE001 - one provider must not break the run
            logger.warning(f"SubtitleBest 检索失败：{err}")
            return []

    async def resolve(self, cand: SubCandidate) -> str:
        payload = cand.payload or {}
        body = {
            "sub_sha256": payload.get("sub_sha256"),
            "imdb_id": payload.get("imdb_id"),
            "is_movie": bool(payload.get("is_movie")),
            "season": int(payload.get("season") or 0),
            "episode": int(payload.get("episode") or 0),
            "season_package_id": payload.get("season_package_id") or "",
            "language": int(payload.get("language") or 0),
            "api_key": self.config.subtitle_best_api_key,
            "token": payload.get("token"),
        }
        data = await self._post(self.client(), "/get-dl-url", body)
        return str(data.get("download_link") or "")


class AssrtSource(BaseSource):
    """ASSRT：按中英文片名检索，需要 Token。"""

    key = "assrt"

    @staticmethod
    def _lang_of(langlist: dict) -> LangInfo:
        langlist = langlist or {}
        chs = bool(langlist.get("langchs"))
        cht = bool(langlist.get("langcht"))
        eng = bool(langlist.get("langeng"))
        dou = bool(langlist.get("langdou"))
        if not (chs or cht):
            return LangInfo(chinese=False, bilingual=eng)
        return LangInfo(
            chinese=True,
            variant=LANG_CHS if chs or not cht else LANG_CHT,
            bilingual=eng or dou,
        )

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        token = (self.config.assrt_token or "").strip()
        if not token:
            return []

        client = self.client(timeout=20)
        names = [req.title, req.en_title, *req.aliases]
        for keyword in req.keywords:
            try:
                resp = await client.get_res(
                    f"{ASSRT_ROOT}/sub/search",
                    params={"q": keyword, "cnt": 15, "pos": 0, "token": token},
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(f"ASSRT 检索失败（{keyword}）：{err}")
                continue

            data = self._json(resp)
            if not isinstance(data, dict):
                continue
            subs = (data.get("sub") or {}).get("subs") or []
            if isinstance(subs, dict):
                subs = []

            cands: list[SubCandidate] = []
            for item in subs:
                lang = self._lang_of((item.get("lang") or {}).get("langlist") or {})
                if not lang.chinese:
                    continue
                video_name = str(item.get("videoname") or "")
                native_name = str(item.get("native_name") or "")
                if not title_matches(names, video_name or native_name, req.year):
                    continue
                cands.append(
                    SubCandidate(
                        source=self.key,
                        title=native_name or video_name or keyword,
                        language=lang.display,
                        resolve="assrt",
                        payload={"id": item.get("id")},
                        score=float(item.get("vote_score") or 0),
                    )
                )
            if cands:
                return cands
            await asyncio.sleep(1)
        return []

    async def resolve(self, cand: SubCandidate) -> str:
        token = (self.config.assrt_token or "").strip()
        sub_id = (cand.payload or {}).get("id")
        if not token or not sub_id:
            return ""
        try:
            resp = await self.client(timeout=20).get_res(
                f"{ASSRT_ROOT}/sub/detail", params={"id": sub_id, "token": token}
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"ASSRT 详情失败：{err}")
            return ""
        data = self._json(resp)
        if not isinstance(data, dict):
            return ""
        subs = (data.get("sub") or {}).get("subs") or []
        if isinstance(subs, dict) or not subs:
            return ""
        return str(subs[0].get("url") or "")


class XunleiSource(BaseSource):
    """迅雷字幕：按视频文件 cid 精确匹配，无需账号。"""

    key = "xunlei"

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        cid = sha1_cid(req.video_path)
        if not cid:
            return []

        try:
            resp = await self.client(timeout=20).get_res(XUNLEI_ROOT % cid)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"迅雷字幕检索失败：{err}")
            return []

        data = self._json(resp)
        if not isinstance(data, dict):
            return []
        items = data.get("sublist") or data.get("Sublist") or []
        if isinstance(items, dict):
            items = []

        cands: list[SubCandidate] = []
        for item in items:
            url = str(item.get("surl") or "")
            if not url:
                continue
            name = str(item.get("sname") or "")
            # 该接口的 language 字段偶尔是坏字节，优先用文件名里的 .chs/.cht 标记
            lang = lang_from_label(f"{name} {item.get('language') or ''}")
            if subtitle_name_mismatch(name, req, strict=True):
                logger.info(f"迅雷字幕与目标不符，跳过：{name}")
                continue
            cands.append(
                SubCandidate(
                    source=self.key,
                    title=name or req.video_path.stem,
                    language=lang.display,
                    url=url,
                    ext=guess_ext(url),
                    score=float(item.get("svote") or 0),
                )
            )
        cands.sort(key=lambda c: (c.lang_info().chinese, c.score), reverse=True)
        return cands


class ShooterSource(BaseSource):
    """Shooter：按视频文件 hash 精确匹配。"""

    key = "shooter"

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        file_hash = md5_chunks_hash(req.video_path)
        if not file_hash:
            return []

        try:
            resp = await self.client(timeout=20).post_res(
                SHOOTER_ROOT,
                data={
                    "filehash": file_hash,
                    "pathinfo": req.video_path.name,
                    "format": "json",
                    "lang": "Chn",
                },
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"Shooter 检索失败：{err}")
            return []

        data = self._json(resp)
        if not isinstance(data, list):
            return []

        cands: list[SubCandidate] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            files = item.get("files") or []
            if not files:
                continue
            first = files[0]
            desc = str(item.get("desc") or "")
            lang = lang_from_label(desc)
            cands.append(
                SubCandidate(
                    source=self.key,
                    title=f"{req.video_path.stem} · {desc}" if desc else req.video_path.stem,
                    language=lang.display,
                    url=str(first.get("link") or ""),
                    ext=guess_ext(
                        str(first.get("link") or ""),
                        f".{first.get('ext')}" if first.get("ext") else "",
                    ),
                )
            )
        return [cand for cand in cands if cand.url]


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class NativeSubtitleEngine:
    """
    内置字幕引擎。

    读取插件已有配置（SubtitleBest API Key、ASSRT Token、语言、覆盖开关），
    结果结构与 SubtitleForge 服务 `/v1/process`、`/v1/scan` 保持一致。
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Any = None

    def _get_lock(self) -> asyncio.Lock:
        """Return a lock bound to the current event loop (safe across reloads)."""

        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    # -- 配置 ---------------------------------------------------------

    def source_config(self) -> SourceConfig:
        plugin = self.plugin
        return SourceConfig(
            subtitle_best_api_key=str(
                getattr(plugin, "_subtitle_best_api_key", "") or ""
            ),
            subtitle_best_header_token=str(
                getattr(plugin, "_subtitle_best_header_token", "") or ""
            )
            or SUBTITLE_BEST_TOKEN,
            assrt_token=str(getattr(plugin, "_assrt_token", "") or ""),
            xunlei_enabled=bool(getattr(plugin, "_native_xunlei", True)),
            shooter_enabled=bool(getattr(plugin, "_native_shooter", False)),
        )

    @property
    def _tag_style(self) -> str:
        style = str(getattr(self.plugin, "_native_tag_style", "iso") or "iso")
        return style if style in TAG_STYLES else "iso"

    @property
    def _max_subs(self) -> int:
        try:
            return max(1, min(5, int(getattr(self.plugin, "_native_max_subs", 2) or 2)))
        except (TypeError, ValueError):
            return 2

    @property
    def _request_delay(self) -> float:
        try:
            return max(0.0, float(getattr(self.plugin, "_native_request_delay", 1.0) or 1.0))
        except (TypeError, ValueError):
            return 1.0

    @property
    def _auto_sync(self) -> bool:
        return bool(getattr(self.plugin, "_native_auto_sync", False))

    @property
    def _auto_encoding(self) -> bool:
        return bool(getattr(self.plugin, "_native_encoding", True))

    @property
    def _subtitle_exts(self) -> set[str]:
        exts = getattr(settings, "RMT_SUBEXT", None) or [".srt", ".ass", ".ssa", ".sup"]
        return {str(ext).lower() for ext in exts}

    def active_sources(self) -> list[BaseSource]:
        config = self.source_config()
        sources: list[BaseSource] = []
        if config.subtitle_best_api_key:
            sources.append(SubtitleBestSource(config))
        if config.assrt_token:
            sources.append(AssrtSource(config))
        if config.xunlei_enabled:
            sources.append(XunleiSource(config))
        if config.shooter_enabled:
            sources.append(ShooterSource(config))
        return sources

    def describe(self) -> list[dict[str, Any]]:
        """返回各来源的可用状态，供插件页面展示。"""

        config = self.source_config()
        return [
            {
                "key": "subtitle_best",
                "name": SOURCE_LABEL["subtitle_best"],
                "ready": bool(config.subtitle_best_api_key),
                "reason": "" if config.subtitle_best_api_key else "未配置 API Key",
            },
            {
                "key": "assrt",
                "name": SOURCE_LABEL["assrt"],
                "ready": bool(config.assrt_token),
                "reason": "" if config.assrt_token else "未配置 Token",
            },
            {
                "key": "xunlei",
                "name": SOURCE_LABEL["xunlei"],
                "ready": config.xunlei_enabled,
                "reason": "" if config.xunlei_enabled else "已关闭",
            },
            {
                "key": "shooter",
                "name": SOURCE_LABEL["shooter"],
                "ready": config.shooter_enabled,
                "reason": "" if config.shooter_enabled else "已关闭",
            },
        ]

    def has_source(self) -> bool:
        return bool(self.active_sources())

    @staticmethod
    def required_langs(values: Any) -> list[str]:
        """Expose the language normalizer to the plugin."""

        return required_langs(values)

    # -- 对外入口 -----------------------------------------------------

    async def process(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理单个视频，返回与 `/v1/process` 一致的结构。"""

        async with self._get_lock():
            return await self._process(payload)

    async def _process(self, payload: dict[str, Any]) -> dict[str, Any]:
        video = Path(str(payload.get("video_path") or ""))
        if not video.is_file():
            return {
                "status": "error",
                "downloaded": [],
                "errors": [f"视频文件不存在：{video}"],
            }

        langs = required_langs(payload.get("languages"))
        if not langs:
            return {"status": "error", "downloaded": [], "errors": ["未配置目标字幕语言"]}

        req = self.request_from_payload(payload)
        overwrite = bool(payload.get("overwrite"))
        dry_run = bool(payload.get("dry_run"))

        existing = await self.scan_existing(video)
        missing = set(langs) if overwrite else set(langs) - existing
        if not missing:
            return {"status": "ok", "downloaded": [], "errors": [], "missing": []}

        candidates = await self.search(req)
        if not candidates:
            return {
                "status": "not_found",
                "downloaded": [],
                "errors": [],
                "missing": sorted(missing),
            }

        if dry_run:
            return {
                "status": "dry_run",
                "downloaded": [],
                "errors": [],
                "missing": sorted(missing),
                "candidates": [cand.title for cand in candidates[:5]],
            }

        downloaded: list[dict[str, Any]] = []
        notes: list[str] = []
        for cand in candidates:
            if not missing or len(downloaded) >= self._max_subs:
                break
            info = cand.lang_info()
            if info.chinese and not any(info.covers([lang]) for lang in missing):
                continue

            try:
                dests, detected = await self.download_and_place(cand, req)
            except Exception as err:  # noqa: BLE001 - keep going with other candidates
                notes.append(f"{cand.source}: {err}")
                continue

            if not dests or detected is None:
                notes.append(f"{cand.source}: 下载或解析失败（{cand.title}）")
                continue

            covered = {lang for lang in missing if detected.covers([lang])}
            if not covered:
                notes.append(f"{cand.source}: 语言不符（{detected.display}）")
                continue

            missing -= covered
            for dest in dests:
                downloaded.append(
                    {
                        "path": str(dest),
                        "language": detected.api_code,
                        "provider": SOURCE_LABEL.get(cand.source, cand.source),
                    }
                )
            await asyncio.sleep(self._request_delay)

        if downloaded:
            status = "downloaded"
        elif missing:
            status = "not_found"
        else:
            status = "ok"
        return {
            "status": status,
            "downloaded": downloaded,
            "errors": [],
            "notes": notes,
            "missing": sorted(missing),
        }

    async def scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """扫描媒体库并补全，返回与 `/v1/scan` 一致的结构。"""

        lock = self._get_lock()
        if lock.locked():
            return {"status": "busy", "message": "内置字幕引擎正在处理其他任务"}

        async with lock:
            return await self._scan(payload)

    async def _scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        paths = [Path(str(item)) for item in payload.get("paths") or [] if str(item).strip()]
        excludes = [str(item) for item in payload.get("exclude_paths") or [] if str(item)]
        extensions = {
            str(ext).lower()
            for ext in (payload.get("video_extensions") or VIDEO_EXTS)
        }
        langs = required_langs(payload.get("languages"))
        dry_run = bool(payload.get("dry_run"))
        try:
            max_files = max(1, int(payload.get("max_files") or 10))
        except (TypeError, ValueError):
            max_files = 10
        try:
            daily_limit = max(1, int(payload.get("daily_download_limit") or max_files))
        except (TypeError, ValueError):
            daily_limit = max_files

        if not langs:
            return {"status": "error", "errors": ["未配置目标字幕语言"]}

        existing_roots = [path for path in paths if path.is_dir()]
        if not existing_roots:
            return {"status": "error", "errors": ["没有可扫描的媒体库目录"]}

        discovered = 0
        checked = 0
        already = 0
        missing_items: list[tuple[Path, list[str]]] = []

        for root in existing_roots:
            for video in self.iter_videos(root, extensions):
                discovered += 1
                if len(missing_items) >= max_files:
                    break
                if self.is_excluded(video, excludes):
                    continue
                checked += 1
                have = await self.scan_existing(video)
                missing = sorted(set(langs) - have)
                if not missing:
                    already += 1
                    continue
                missing_items.append((video, missing))
            if len(missing_items) >= max_files:
                break

        items: list[dict[str, Any]] = []
        downloaded_total = 0
        errors: list[str] = []

        for video, missing in missing_items:
            base = self.scan_item(video, missing)
            if dry_run:
                items.append({**base, "status": "dry_run", "downloaded": []})
                continue

            if downloaded_total >= daily_limit:
                items.append({**base, "status": "queued", "downloaded": []})
                continue

            result = await self._process(
                {
                    "video_path": str(video),
                    "languages": [API_LANG_INVERSE[lang] for lang in missing],
                    "overwrite": bool(payload.get("overwrite")),
                    "dry_run": False,
                }
            )
            entries = result.get("downloaded") or []
            downloaded_total += len(entries)
            errors.extend(str(err) for err in result.get("errors") or [])
            items.append(
                {
                    **base,
                    "status": (
                        "downloaded"
                        if entries
                        else ("partial" if result.get("missing") else "not_found")
                    ),
                    "missing": result.get("missing") or missing,
                    "downloaded": entries,
                }
            )

        return {
            "status": "ok",
            "dry_run": dry_run,
            "discovered": discovered,
            "checked": checked,
            "existing": already,
            "queued": len(missing_items),
            "queue_remaining": 0,
            "downloaded": downloaded_total,
            "downloads_today": downloaded_total,
            "daily_download_limit": daily_limit,
            "items": items,
            "errors": errors,
        }

    # -- 内部实现 -----------------------------------------------------

    def request_from_payload(self, payload: dict[str, Any]) -> SubtitleRequest:
        """把服务 API 的请求体转成内部检索请求。"""

        video = Path(str(payload.get("video_path") or ""))
        media_type = str(payload.get("media_type") or "movie").lower()
        season = payload.get("season")
        episode = payload.get("episode")

        aliases: list[str] = []
        for key in ("original_title", "title_aliases"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                aliases.append(value)
            elif isinstance(value, list):
                aliases.extend(str(item) for item in value if item)

        req = SubtitleRequest(
            video_path=video,
            media_type="tv" if media_type in {"tv", "电视剧", "剧集"} else "movie",
            title=str(payload.get("title") or ""),
            en_title=str(payload.get("english_title") or ""),
            aliases=aliases,
            year=str(payload.get("year") or ""),
            imdb_id=str(payload.get("imdb_id") or ""),
            season=int(season) if season not in (None, "") else None,
            episode=int(episode) if episode not in (None, "") else None,
            overwrite=bool(payload.get("overwrite")),
        )

        # 队列条目通常只有中文标题，文件名里的英文名对 ASSRT 命中很关键
        try:
            meta = MetaInfo(title=video.stem)
        except Exception:  # noqa: BLE001 - filename parsing is best effort
            meta = None
        if meta is not None:
            en_name = str(getattr(meta, "en_name", "") or "").strip()
            cn_name = str(getattr(meta, "cn_name", "") or "").strip()
            if not req.title:
                req.title = cn_name or video.stem
            if not req.en_title and en_name:
                req.en_title = en_name
            if en_name and en_name not in req.aliases and en_name != req.title:
                req.aliases.append(en_name)
            if not req.year:
                req.year = str(getattr(meta, "year", "") or "")
            if req.episode is None:
                req.episode = getattr(meta, "begin_episode", None)
            if req.season is None:
                req.season = getattr(meta, "begin_season", None)
        if not req.title:
            req.title = video.stem
        return req

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        sources = self.active_sources()
        if not sources:
            return []

        results = await asyncio.gather(
            *(source.search(req) for source in sources), return_exceptions=True
        )
        merged: list[SubCandidate] = []
        for source, result in zip(sources, results):
            if isinstance(result, BaseException):
                logger.warning(f"{source.label} 检索异常：{result}")
                continue
            if result:
                logger.info(f"{source.label} 命中 {len(result)} 条字幕")
            merged.extend(result)

        merged.sort(key=self._sort_key, reverse=True)
        return merged

    def _sort_key(self, cand: SubCandidate) -> tuple:
        info = cand.lang_info()
        lang_rank = 1 if info.chinese else 0
        try:
            source_rank = 4 - ["subtitle_best", "assrt", "xunlei", "shooter"].index(cand.source)
        except ValueError:
            source_rank = 0
        return (lang_rank, 1 if info.bilingual else 0, source_rank, min(cand.score, 1e6))

    async def scan_existing(self, video: Path) -> set[str]:
        """扫描外挂与内封字幕，返回已满足的语言。"""

        satisfied: set[str] = set()
        stem_lower = video.stem.lower()
        sub_exts = self._subtitle_exts
        folders = [video.parent]
        for extra in ("Subs", "subtitles", "Subtitles"):
            extra_dir = video.parent / extra
            if extra_dir.is_dir():
                folders.append(extra_dir)

        for folder in folders:
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_file() or entry.suffix.lower() not in sub_exts:
                        continue
                except OSError:
                    continue
                tail = self._subtitle_tail(entry.name.lower(), stem_lower)
                if tail is None:
                    continue
                variant = self._variant_from_tag(tail)
                if variant:
                    satisfied.add(variant)
                    continue
                detected = self._detect_file(entry)
                if detected.chinese and detected.variant:
                    satisfied.add(detected.variant)

        for variant in await self.probe_embedded(video):
            satisfied.add(variant)
        return satisfied

    @staticmethod
    def _subtitle_tail(name_lower: str, stem_lower: str) -> Optional[str]:
        common = 0
        limit = min(len(name_lower), len(stem_lower))
        while common < limit and name_lower[common] == stem_lower[common]:
            common += 1
        if common < 6:
            return None

        cut = max(name_lower.rfind(sep, 0, common) for sep in "._- ")
        if cut < 6:
            cut = common
        tail = name_lower[cut:]

        video_se = re.search(r"s(\d{1,2})[ ._-]?e(\d{1,3})", stem_lower)
        sub_se = re.search(r"s(\d{1,2})[ ._-]?e(\d{1,3})", tail)
        if sub_se:
            if not video_se:
                return None
            if (sub_se.group(1), sub_se.group(2)) != (video_se.group(1), video_se.group(2)):
                return None
        return tail

    @staticmethod
    def _variant_from_tag(tail: str) -> str:
        text = tail.lower()
        if re.search(r"zh[-_.]?tw|zh[-_.]?hant|cht|chi[-_.]?tra|hant|big5|繁", text):
            return LANG_CHT
        if re.search(r"zh[-_.]?cn|zh[-_.]?hans|zh[-_.]?sg|chs|chi[-_.]?sim|hans|简", text):
            return LANG_CHS
        return ""

    def _detect_file(self, path: Path) -> LangInfo:
        try:
            with path.open("rb") as fp:
                data = fp.read(256 * 1024)
        except OSError:
            return LangInfo()
        return detect_language(decode_subtitle_bytes(data))

    async def probe_embedded(self, video: Path) -> set[str]:
        """用 ffprobe 检测内封字幕流；ffprobe 不可用时静默跳过。"""

        result: set[str] = set()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except (FileNotFoundError, OSError):
            return result

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-select_streams",
                "s",
                str(video),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not stdout:
                return result
            streams = json.loads(stdout).get("streams") or []
        except Exception as err:  # noqa: BLE001
            logger.debug(f"ffprobe 解析失败：{err}")
            return result

        for stream in streams:
            tags = stream.get("tags") or {}
            lang = str(tags.get("language") or "").lower()
            title = str(tags.get("title") or "").lower()
            if re.search(r"zh[-_]?tw|cht|hant|chi[-_]?tra", lang) or any(
                key in title for key in ("繁", "cht", "hant", "tw")
            ):
                result.add(LANG_CHT)
            elif re.search(r"zh|chi|zho", lang) or any(
                key in title for key in ("简", "chs", "hans", "cn", "中")
            ):
                result.add(LANG_CHS)
        return result

    # -- 下载与落盘 ---------------------------------------------------

    async def download_and_place(
        self, cand: SubCandidate, req: SubtitleRequest
    ) -> tuple[list[Path], Optional[LangInfo]]:
        url = cand.url
        if not url:
            source = next(
                (item for item in self.active_sources() if item.key == cand.source), None
            )
            if source is None:
                return [], None
            url = await source.resolve(cand)
        if not url:
            return [], None

        logger.info(f"内置源下载字幕（{SOURCE_LABEL.get(cand.source, cand.source)}）：{cand.title}")
        data = await self._fetch(url)
        if not data:
            return [], None

        work = self.plugin.get_data_path() / "native" / f"{int(time.time() * 1000)}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            files = await self._unpack_payload(data, work, cand, url)
            if not files:
                logger.warning(f"未从下载内容中提取到字幕文件：{cand.title}")
                return [], None

            files = self._pick_episode_files(files, req)
            placed: list[Path] = []
            detected: Optional[LangInfo] = None

            for sub_file in files[:2]:
                try:
                    text = decode_subtitle_bytes(sub_file.read_bytes())
                except OSError:
                    continue
                info = detect_language(text)
                if not info.chinese:
                    logger.info(f"字幕内容不含中文，跳过：{sub_file.name}")
                    continue
                if self._auto_encoding:
                    self._ensure_utf8(sub_file, text)
                if self._auto_sync:
                    await self._sync(sub_file, req.video_path)
                dest = self._place(sub_file, req, info)
                if dest:
                    placed.append(dest)
                    detected = info
                    logger.info(f"字幕已落盘：{dest}")
            return placed, detected
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _fetch(self, url: str) -> bytes:
        client = AsyncRequestUtils(
            ua=settings.USER_AGENT,
            proxies=getattr(settings, "PROXY", None) or None,
            timeout=60,
        )
        try:
            resp = await client.get_res(url)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"字幕下载异常：{err}")
            return b""
        if not resp or resp.status_code != 200:
            code = getattr(resp, "status_code", "N/A")
            logger.warning(f"字幕下载失败 HTTP {code}：{url[:120]}")
            return b""
        return resp.content or b""

    async def _unpack_payload(
        self, data: bytes, work: Path, cand: SubCandidate, url: str
    ) -> list[Path]:
        ext = (cand.ext or "").lower()
        if ext not in {".zip", ".rar", ".7z"}:
            ext = sniff_ext(data)

        if ext in {".zip", ".rar", ".7z"}:
            archive = work / f"payload{ext}"
            archive.write_bytes(data)
            return await self._unpack_archive(archive, work)

        target = work / f"subtitle{ext if ext in self._subtitle_exts else '.srt'}"
        target.write_bytes(data)
        return [target]

    async def _unpack_archive(self, archive: Path, work: Path) -> list[Path]:
        if archive.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(work)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"ZIP 解压失败：{err}")
                return []
        else:
            commands: list[list[str]] = []
            seven = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
            if seven:
                commands.append([seven, "x", "-y", f"-o{work}", str(archive)])
            if archive.suffix.lower() == ".rar":
                unrar = shutil.which("unrar") or shutil.which("unar")
                if unrar:
                    commands.append([unrar, "x", "-o+", str(archive), f"{work}/"])
            bsdtar = shutil.which("bsdtar")
            if bsdtar:
                commands.append([bsdtar, "-xf", str(archive), "-C", str(work)])

            if not commands:
                logger.warning("需要解压 rar/7z，但容器内没有 7z / unrar / bsdtar")
                return []

            ok = False
            for cmd in commands:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                    if proc.returncode == 0:
                        ok = True
                        break
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"解压命令失败 {cmd[0]}：{err}")
            if not ok:
                logger.warning(f"压缩包解压失败：{archive.name}")
                return []

        return [
            path
            for path in work.rglob("*")
            if path.is_file() and path.suffix.lower() in self._subtitle_exts
        ]

    @staticmethod
    def _pick_episode_files(files: list[Path], req: SubtitleRequest) -> list[Path]:
        if not req.is_tv or req.episode is None or len(files) <= 1:
            return files

        season = req.season or 1
        episode = req.episode
        matched: list[Path] = []
        for path in files:
            name = path.stem
            patterns = [
                rf"[Ss]{season:02d}[Ee]{episode:02d}",
                rf"[Ss]{season}[Ee]{episode}\b",
                rf"(?<![A-Za-z])[Ee]{episode:02d}(?![0-9])",
                rf"第\s*{episode}\s*[集话話]",
            ]
            if any(re.search(pattern, name) for pattern in patterns):
                matched.append(path)
        return matched or files

    @staticmethod
    def _ensure_utf8(path: Path, text: str) -> None:
        try:
            raw = path.read_bytes()
        except OSError:
            return
        try:
            raw.decode("utf-8")
            return
        except UnicodeDecodeError:
            pass
        try:
            path.write_bytes(text.encode("utf-8"))
            logger.info(f"字幕编码已转为 UTF-8：{path.name}")
        except OSError as err:
            logger.debug(f"字幕转码失败：{err}")

    async def _sync(self, sub_file: Path, video: Path) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffsubsync",
                "--help",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except (FileNotFoundError, OSError):
            logger.warning("未安装 ffsubsync，跳过调轴")
            return False

        synced = sub_file.with_name(f"{sub_file.stem}.synced{sub_file.suffix}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffsubsync",
                str(video),
                "-i",
                str(sub_file),
                "-o",
                str(synced),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and synced.exists():
                synced.replace(sub_file)
                logger.info(f"调轴完成：{sub_file.name}")
                return True
            message = (stderr or b"").decode(errors="ignore")[:200]
            logger.warning(f"调轴失败：{message}")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"调轴异常：{err}")
        return False

    def _place(
        self, sub_file: Path, req: SubtitleRequest, info: LangInfo
    ) -> Optional[Path]:
        style = TAG_STYLES[self._tag_style]
        tag = style.get(info.variant or LANG_CHS, "zh-CN")
        dest = req.video_path.parent / f"{req.video_path.stem}.{tag}{sub_file.suffix.lower()}"
        if dest.exists() and not req.overwrite:
            logger.debug(f"目标字幕已存在，跳过：{dest.name}")
            return None
        try:
            shutil.move(str(sub_file), str(dest))
        except OSError as err:
            logger.error(f"字幕移动失败：{err}")
            return None
        return dest

    # -- 扫描辅助 -----------------------------------------------------

    @staticmethod
    def is_bdmv_segment(path: Path) -> bool:
        """True for Blu-ray disc fragments (BDMV/STREAM/*.m2ts)."""

        parts = [part.lower() for part in path.parts]
        return "bdmv" in parts or "certificate" in parts

    async def inspect(self, video: Path) -> dict[str, Any]:
        """Report which subtitle files exist beside a video and how they parse."""

        exts = self._subtitle_exts
        stem_lower = video.stem.lower()
        files: list[dict[str, Any]] = []
        folders = [video.parent]
        for extra in ("Subs", "subtitles", "Subtitles"):
            candidate = video.parent / extra
            if candidate.is_dir():
                folders.append(candidate)

        for folder in folders:
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_file() or entry.suffix.lower() not in exts:
                        continue
                except OSError:
                    continue
                name_lower = entry.name.lower()
                if not (
                    name_lower.startswith(stem_lower)
                    or stem_lower.startswith(entry.stem.lower())
                ):
                    continue
                info = self._detect_file(entry)
                files.append(
                    {
                        "name": entry.name,
                        "detected": info.display,
                        "variant": info.variant,
                        "chinese": info.chinese,
                        "size": entry.stat().st_size,
                    }
                )

        return {
            "video": str(video),
            "exists": video.is_file(),
            "files": files,
            "embedded": sorted(await self.probe_embedded(video)),
            "existing": sorted(await self.scan_existing(video)),
        }

    @staticmethod
    def scan_item(video: Path, missing: list[str]) -> dict[str, Any]:
        """构造扫描条目（含路径与季集，供插件队列展示）。"""

        season: Optional[int] = None
        episode: Optional[int] = None
        matched = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})", video.stem)
        if matched:
            season, episode = int(matched.group(1)), int(matched.group(2))
        year_match = re.search(r"(?:19|20)\d{2}", video.stem)
        return {
            "path": str(video),
            "title": video.stem,
            "year": year_match.group(0) if year_match else "",
            "season": season,
            "episode": episode,
            "missing": missing,
        }

    @staticmethod
    def iter_videos(root: Path, extensions: set[str]) -> Iterable[Path]:
        for current, dirs, files in os.walk(root):
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".")
                and name.lower()
                not in {"@eadir", "#recycle", "bdmv", "certificate"}
            ]
            for name in files:
                if Path(name).suffix.lower() in extensions:
                    yield Path(current) / name

    @staticmethod
    def is_excluded(video: Path, patterns: list[str]) -> bool:
        text = str(video)
        for pattern in patterns:
            try:
                if re.search(pattern, text):
                    return True
            except re.error:
                if pattern in text:
                    return True
        return False


def required_langs(values: Any) -> list[str]:
    """把 zh-Hans/zh-Hant/zh-CN/chs 等写法统一成内部语言名。"""

    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values or []:
        key = str(value).strip().lower()
        lang = LANG_ALIASES.get(key)
        if lang and lang not in result:
            result.append(lang)
    return result


API_LANG_INVERSE = {LANG_CHS: "zh-Hans", LANG_CHT: "zh-Hant"}

__all__ = [
    "NativeSubtitleEngine",
    "SourceConfig",
    "required_langs",
    "detect_language",
    "md5_chunks_hash",
    "sha1_cid",
    "LANG_CHS",
    "LANG_CHT",
]
