"""
ChineseSubFinder 字幕插件（MoviePilot v3）

参考 https://github.com/ChineseSubFinder/ChineseSubFinder 的实现思路，
把「找字幕 → 语言匹配 → 下载 → 解压 → 转码 → 调轴 → 落盘」这一整套流程
直接做进 MoviePilot，无需再单独部署 ChineseSubFinder 容器。

字幕来源（可分别开关）：
- SubtitleBest：官方推荐的第三方字幕服务，按 IMDb 精确匹配，支持电影 / 单集 / 整季包
- Assrt：按中英文片名搜索，需 Token
- 迅雷字幕：按视频文件 cid 精确匹配，无需鉴权
- Shooter：按视频文件 hash 精确匹配（站点年久失修，默认关闭）

作者：Minis
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
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

from apscheduler.triggers.cron import CronTrigger

try:  # MoviePilot V3
    from app.schemas.types import EventType, MediaType, MessageType
    from app.sdk.config import settings
    from app.sdk.events import eventmanager
    from app.sdk.logging import logger
    from app.sdk.media import MetaInfo
    from app.sdk.network import AsyncRequestUtils
    from app.sdk.plugin import _PluginBase
except ImportError:  # MoviePilot V2
    from app.core.config import settings
    from app.core.event import eventmanager
    from app.core.metainfo import MetaInfo
    from app.log import logger
    from app.plugins import _PluginBase
    from app.schemas.types import EventType, MediaType
    from app.schemas.types import NotificationType as MessageType  # type: ignore[assignment]
    from app.utils.http import AsyncRequestUtils  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

LANG_CHS = "简体中文"
LANG_CHT = "繁體中文"
LANG_EN = "英文"
LANG_UNKNOWN = "未知"

# 字幕文件里常见的繁体特征字（用于内容判定，取自 CSF / AutoSubtitle 的经验集合）
TRAD_CHARS = set(
    "說見時會過個們來學開關頭門體國對發現後經麼樣點從當還沒給讓問聽"
    "實際報長東動裡種類華機產萬話風電業處際識達爾羅馬魚鳥龍書愛專門難"
    "擊變聲讀寫買賣進遠運區彆麵願媽爺媽貓豬車軍農邊號歲萬與專業務員"
    "單賣車輪轉軟體網路檔案資訊電影節目錄製畫質選擇確定取消開始結束"
)

# SubtitleBest 官方分享接口的公开 header token（与 CSF 保持一致）
SUBTITLE_BEST_ROOT = "https://api.subtitle.best/share-sub/v1"
SUBTITLE_BEST_TOKEN = "5akwmGAbuFWqgaZf9QwT"
# SubtitleBest 语言枚举：只映射能确定的 1/2，其余一律按未知处理，
# 下载后再用文件内容判定，避免误判导致漏下或下错语言。
SUBTITLE_BEST_LANG = {1: LANG_CHS, 2: LANG_CHT}

ASSRT_ROOT = "https://api.assrt.net/v1"
XUNLEI_ROOT = "http://sub.xmp.sandai.net:8000/subxl/%s.json"
SHOOTER_ROOT = "https://www.shooter.cn/api/subapi.php"

# 字幕语言标签风格：Emby/Jellyfin 认 ISO，Plex 认 zh-Hans/zh-Hant
TAG_STYLES = {
    "iso": {LANG_CHS: "zh-CN", LANG_CHT: "zh-TW", LANG_EN: "en"},
    "legacy": {LANG_CHS: "chs", LANG_CHT: "cht", LANG_EN: "eng"},
    "plex": {LANG_CHS: "zh-Hans", LANG_CHT: "zh-Hant", LANG_EN: "en"},
}

SOURCE_ORDER = ["subtitle_best", "assrt", "xunlei", "shooter"]
SOURCE_LABEL = {
    "subtitle_best": "SubtitleBest",
    "assrt": "Assrt",
    "xunlei": "迅雷字幕",
    "shooter": "Shooter",
}

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".flv", ".webm", ".iso", ".rmvb"}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class LangInfo:
    """字幕语言判定结果。"""

    chinese: bool = False
    variant: str = ""  # LANG_CHS / LANG_CHT
    bilingual: bool = False

    @property
    def display(self) -> str:
        if not self.chinese:
            return LANG_EN if self.bilingual else LANG_UNKNOWN
        if self.bilingual:
            return f"{self.variant}·中英双语"
        return self.variant or LANG_UNKNOWN

    def covers(self, required: Iterable[str]) -> bool:
        return self.chinese and bool(self.variant) and self.variant in required


@dataclass
class SubtitleRequest:
    """一次字幕检索的输入。"""

    video_path: Path
    media_type: str = ""
    title: str = ""
    en_title: str = ""
    aliases: list[str] = field(default_factory=list)
    year: str = ""
    imdb_id: str = ""
    season: Optional[int] = None
    episode: Optional[int] = None

    @property
    def is_tv(self) -> bool:
        return self.media_type == MediaType.TV.value or self.season is not None or self.episode is not None

    @property
    def keywords(self) -> list[str]:
        """关键词搜索用的候选词，中文名优先。"""
        result: list[str] = []
        for name in [self.title, *self.aliases, self.en_title]:
            name = (name or "").strip()
            if not name or len(name) < 2:
                continue
            if name in result:
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
    # 需要二次解析（Assrt 详情 / SubtitleBest 取链接）
    resolve: str = ""

    def lang_info(self) -> LangInfo:
        return lang_from_label(self.language)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def md5_chunks_hash(path: Path) -> str:
    """
    Shooter 的文件指纹：4 段 4KB 采样（偏移 4K、2/3、1/3、末尾 8K）的 md5，用 ; 连接。
    """
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
    """
    迅雷字幕的 cid：3 段 20KB（偏移 0、1/3、末尾 20K）拼接后的 SHA1，大写。
    """
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
    """把站点返回的语言标签（中文/英文/缩写混杂）转成 LangInfo。"""
    text = (label or "").strip().lower()
    if not text:
        return LangInfo()

    def has(*tokens: str) -> bool:
        return any(t in text for t in tokens)

    chinese = has("中", "简", "繁", "chs", "cht", "chi", "zho", "zh", "cn", "tw", "hk", "bilingual", "双语", "雙語")
    if not chinese:
        return LangInfo(chinese=False, bilingual=has("eng", "english", "英文", "en"))

    variant = ""
    if has("繁", "cht", "chi_tra", "chi-tra", "zh-tw", "zh_tw", "zh-hant", "zh_hant", "hant", "big5"):
        variant = LANG_CHT
    elif has("简", "chs", "chi_sim", "chi-sim", "zh-cn", "zh_cn", "zh-hans", "zh_hans", "hans", "gb"):
        variant = LANG_CHS
    else:
        # 只写了「中文」：按简体处理，下载后仍会做内容判定
        variant = LANG_CHS

    bilingual = has("双语", "雙語", "简英", "繁英", "中英", "bilingual", "chs&eng", "cht&eng", "简繁", "簡繁")
    return LangInfo(chinese=True, variant=variant, bilingual=bilingual)


def detect_language(text: str) -> LangInfo:
    """
    从字幕正文判定语言：统计中文字符里的繁体特征字占比，并检测是否中英双语。
    """
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
        # 去掉 ASS 特效标签与 HTML 标签
        plain = re.sub(r"\{[^}]*\}|<[^>]*>|\\[Nn]", " ", line)
        han = [c for c in plain if "\u4e00" <= c <= "\u9fff"]
        if han:
            han_lines += 1
            han_total += len(han)
            trad_total += sum(1 for c in han if c in TRAD_CHARS)
        elif re.search(r"[A-Za-z]{3,}", plain):
            en_lines += 1

    if han_lines == 0:
        return LangInfo(chinese=False, bilingual=bool(en_lines))

    variant = LANG_CHS
    if trad_total >= 5 and trad_total / max(han_total, 1) > 0.03:
        variant = LANG_CHT

    bilingual = en_lines >= max(3, int(han_lines * 0.15))
    return LangInfo(chinese=True, variant=variant, bilingual=bilingual)


def decode_subtitle_bytes(data: bytes) -> str:
    """字幕文本解码：UTF-8 优先，其次 GB18030 / BIG5，最后忽略错误强解。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def normalize_title(text: str) -> str:
    """标题归一化：去标点、空格、年份，仅保留字母数字与汉字。"""
    text = unquote(text or "").lower()
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def title_matches(names: Iterable[str], candidate: str, year: str = "") -> bool:
    """
    判断候选字幕的标题是否属于目标影视：
    标题归一化后互相包含即算命中；候选里带年份且与目标年份不一致则否决。
    """
    cand_norm = normalize_title(candidate)
    if not cand_norm:
        return True  # 没有可判断的信息，放行（后面还有季集校验）

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


def subtitle_name_mismatch(name: str, req: "SubtitleRequest", strict: bool = False) -> bool:
    """
    粗略判断字幕文件名是否明显属于别的剧集/影片。

    - 剧集：文件名带集数标记但与目标集不一致 → 否决
    - 电影：文件名带年份但与目标年份不一致 → 否决
    - strict：文件名既没有集数/年份，也和片名毫无交集 → 否决
      （只用于按文件指纹匹配的来源，这类来源的字幕名通常与发布名高度一致）
    """
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
        tokens = [t for t in (req.title, req.en_title) if t]
        if tokens and not title_matches(tokens, name, ""):
            return True
    return False


def guess_ext(url: str, fallback: str = "") -> str:
    """从 URL 猜字幕扩展名。"""
    path = urlparse(url or "").path
    ext = Path(unquote(path)).suffix.lower()
    if ext in {".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub", ".zip", ".rar", ".7z"}:
        return ext
    return fallback


def sniff_ext(data: bytes) -> str:
    """按文件头判断压缩包 / 字幕格式。"""
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


def format_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


# ---------------------------------------------------------------------------
# 字幕来源适配器
# ---------------------------------------------------------------------------


class BaseSource:
    """字幕来源基类：search 返回候选，resolve 返回可直接下载的直链。"""

    key = ""

    def __init__(self, plugin: "ChineseSubFinder") -> None:
        self.plugin = plugin

    @property
    def label(self) -> str:
        return SOURCE_LABEL.get(self.key, self.key)

    def client(self, timeout: float = 30) -> AsyncRequestUtils:
        return AsyncRequestUtils(
            ua=settings.USER_AGENT,
            proxies=settings.PROXY or None,
            timeout=timeout,
        )

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        raise NotImplementedError

    async def resolve(self, cand: SubCandidate) -> str:
        return cand.url

    @staticmethod
    def _json(resp) -> Any:
        """容错解析 JSON 响应（部分站点会返回夹杂坏字节的 UTF-8）。"""
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
    """
    SubtitleBest：CSF 官方推荐的分享接口。
    按 IMDb ID 检索，电影 / 单集 / 整季包三种粒度，命中率最高。
    """

    key = "subtitle_best"

    async def _post(self, client: AsyncRequestUtils, path: str, body: dict) -> dict:
        resp = await client.post_res(
            f"{SUBTITLE_BEST_ROOT}{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {SUBTITLE_BEST_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        data = self._json(resp)
        if resp is not None:
            limit = resp.headers.get("X-Daily-Limit")
            count = resp.headers.get("X-Daily-Count")
            if limit:
                logger.debug(f"[CSF] SubtitleBest 今日额度 {count}/{limit}")
        if not isinstance(data, dict):
            return {}
        if data.get("status") not in (0, None):
            logger.warning(f"[CSF] SubtitleBest {path} 返回异常: {data.get('message')}")
            return {}
        return data

    def _candidates(self, subs: Iterable[dict], req: SubtitleRequest, package_id: str = "") -> list[SubCandidate]:
        result: list[SubCandidate] = []
        for item in subs or []:
            try:
                lang_code = int(item.get("language") or 0)
            except (TypeError, ValueError):
                lang_code = 0
            language = SUBTITLE_BEST_LANG.get(lang_code, LANG_UNKNOWN)
            ext = str(item.get("ext") or "").strip()
            if ext and not ext.startswith("."):
                ext = f".{ext}"
            result.append(
                SubCandidate(
                    source=self.key,
                    title=str(item.get("title") or req.title or req.video_path.stem),
                    language=language,
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
        api_key = (self.plugin.subtitle_best_api_key or "").strip()
        imdb = (req.imdb_id or "").strip()
        if not api_key:
            return []
        if not imdb.startswith("tt"):
            logger.debug(f"[CSF] SubtitleBest 需要 IMDb ID，跳过: {req.video_path.name}")
            return []

        client = self.client()
        try:
            if req.is_tv and req.season is not None and req.episode is not None:
                data = await self._post(
                    client,
                    "/search-tv-eps",
                    {"imdb_id": imdb, "season": int(req.season), "episode": int(req.episode), "api_key": api_key},
                )
                cands = self._candidates(data.get("subtitles") or [], req)
                if cands:
                    return cands
                # 单集没有字幕时，退回整季包
                packages = await self._post(
                    client, "/search-tv-season-package", {"imdb_id": imdb, "season": int(req.season), "api_key": api_key}
                )
                for package_id in (packages.get("season_package_ids") or [])[:2]:
                    detail = await self._post(
                        client,
                        "/search-tv-season-package-id",
                        {"imdb_id": imdb, "season_package_id": package_id, "api_key": api_key},
                    )
                    cands.extend(self._candidates(detail.get("subtitles") or [], req, package_id=package_id))
                return cands

            data = await self._post(client, "/search-movie", {"imdb_id": imdb, "api_key": api_key})
            return self._candidates(data.get("subtitles") or [], req)
        except Exception as err:
            logger.warning(f"[CSF] SubtitleBest 检索失败: {err}")
            return []

    async def resolve(self, cand: SubCandidate) -> str:
        api_key = (self.plugin.subtitle_best_api_key or "").strip()
        payload = cand.payload or {}
        body = {
            "sub_sha256": payload.get("sub_sha256"),
            "imdb_id": payload.get("imdb_id"),
            "is_movie": bool(payload.get("is_movie")),
            "season": int(payload.get("season") or 0),
            "episode": int(payload.get("episode") or 0),
            "season_package_id": payload.get("season_package_id") or "",
            "language": int(payload.get("language") or 0),
            "api_key": api_key,
            "token": payload.get("token"),
        }
        client = self.client()
        data = await self._post(client, "/get-dl-url", body)
        link = data.get("download_link") or ""
        if not link:
            logger.warning(f"[CSF] SubtitleBest 未取到下载链接: {data.get('message')}")
        return link


class AssrtSource(BaseSource):
    """
    Assrt：按中英文片名检索，需要 Token（assrt.net 用户中心获取）。
    """

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
        variant = LANG_CHS if chs or not cht else LANG_CHT
        return LangInfo(chinese=True, variant=variant, bilingual=eng or dou)

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        token = (self.plugin.assrt_token or "").strip()
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
            except Exception as err:
                logger.warning(f"[CSF] Assrt 检索失败({keyword}): {err}")
                continue

            data = self._json(resp)
            if not isinstance(data, dict):
                continue
            subs = (data.get("sub") or {}).get("subs") or []
            if isinstance(subs, dict):  # 空结果时接口返回空对象
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
        token = (self.plugin.assrt_token or "").strip()
        sub_id = (cand.payload or {}).get("id")
        if not token or not sub_id:
            return ""
        client = self.client(timeout=20)
        try:
            resp = await client.get_res(f"{ASSRT_ROOT}/sub/detail", params={"id": sub_id, "token": token})
        except Exception as err:
            logger.warning(f"[CSF] Assrt 详情失败: {err}")
            return ""
        data = self._json(resp)
        if not isinstance(data, dict):
            return ""
        subs = (data.get("sub") or {}).get("subs") or []
        if isinstance(subs, dict) or not subs:
            return ""
        return str(subs[0].get("url") or "")


class XunleiSource(BaseSource):
    """
    迅雷字幕：用视频文件 cid 精确匹配，无需账号。
    """

    key = "xunlei"

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        cid = sha1_cid(req.video_path)
        if not cid:
            logger.debug(f"[CSF] 文件过小或不可读，跳过迅雷字幕: {req.video_path.name}")
            return []

        client = self.client(timeout=20)
        try:
            resp = await client.get_res(XUNLEI_ROOT % cid)
        except Exception as err:
            logger.warning(f"[CSF] 迅雷字幕检索失败: {err}")
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
                logger.info(f"[CSF] 迅雷字幕与目标不符，跳过: {name}")
                continue
            cands.append(
                SubCandidate(
                    source=self.key,
                    title=name or req.video_path.stem,
                    language=lang.display,
                    url=url,
                    ext=guess_ext(url),
                    score=float(item.get("svote") or 0),
                    payload={"roffset": item.get("roffset") or 0},
                )
            )
        # 中文优先
        cands.sort(key=lambda c: (c.lang_info().chinese, c.score), reverse=True)
        return cands


class ShooterSource(BaseSource):
    """
    Shooter：用视频文件 hash 精确匹配。站点年久失修，默认关闭。
    """

    key = "shooter"

    async def search(self, req: SubtitleRequest) -> list[SubCandidate]:
        file_hash = md5_chunks_hash(req.video_path)
        if not file_hash:
            return []

        client = self.client(timeout=20)
        try:
            resp = await client.post_res(
                SHOOTER_ROOT,
                data={
                    "filehash": file_hash,
                    "pathinfo": req.video_path.name,
                    "format": "json",
                    "lang": "Chn",
                },
            )
        except Exception as err:
            logger.warning(f"[CSF] Shooter 检索失败: {err}")
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
                    ext=guess_ext(str(first.get("link") or ""), f".{first.get('ext')}" if first.get("ext") else ""),
                )
            )
        return [c for c in cands if c.url]


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """兼容 dict / pydantic 对象的属性读取。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_dict(value: Any) -> dict[str, Any]:
    """把事件负载统一成 dict。"""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            return {}
    raw = getattr(value, "raw", None)
    if isinstance(raw, dict):
        return raw
    return {}


def _type_str(mediainfo: Any) -> str:
    """MediaInfo.type 可能是枚举也可能是字符串。"""
    value = _attr(mediainfo, "type")
    if value is None:
        return ""
    return str(getattr(value, "value", value))


class ChineseSubFinder(_PluginBase):
    # 插件名称
    plugin_name = "ChineseSubFinder 字幕"
    # 插件描述
    plugin_desc = "整理入库/定时扫描时，自动从 SubtitleBest、Assrt、迅雷、Shooter 检索并下载中文字幕"
    # 插件图标
    plugin_icon = "chinesesubfinder.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "huangdale522-gif"
    # 作者主页
    author_url = "https://github.com/huangdale522-gif"
    # 插件配置项ID前缀
    plugin_config_prefix = "chinesesubfinder_"
    # 加载顺序
    plugin_order = 3
    # 可使用的用户级别
    auth_level = 1

    _HISTORY_KEY = "download_history"
    _STATE_KEY = "scan_state"
    _HISTORY_MAX = 80

    def __init__(self) -> None:
        super().__init__()
        # 基础开关
        self._enabled: bool = False
        self._notify: bool = False
        self._on_transfer: bool = True
        # 字幕偏好
        self._subtitle_langs: list[str] = [LANG_CHS]
        self._tag_style: str = "iso"
        self._max_subs: int = 2
        self._request_delay: float = 1.0
        self._auto_sync: bool = False
        self._auto_encoding: bool = True
        self._skip_sync_pattern: str = ""
        # 落盘
        self._save_path_strategy: str = "same"
        self._custom_save_path: str = ""
        # 扫描
        self._scan_cron: str = ""
        self._scan_paths: str = ""
        self._scan_limit: int = 30
        self._exclude_pattern: str = ""
        # 来源
        self._subtitle_best_enabled: bool = True
        self.subtitle_best_api_key: str = ""
        self._assrt_enabled: bool = False
        self.assrt_token: str = ""
        self._xunlei_enabled: bool = True
        self._shooter_enabled: bool = False

        # 运行时
        self._scan_lock = asyncio.Lock()
        self._transfer_lock = asyncio.Lock()
        self._scan_task: Optional[asyncio.Task] = None
        self._subtitle_exts: set[str] = set(settings.RMT_SUBEXT or [".srt", ".ass", ".ssa", ".sup"])

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._on_transfer = bool(config.get("on_transfer", True))
        self._subtitle_langs = config.get("subtitle_langs") or [LANG_CHS]
        self._tag_style = config.get("tag_style") or "iso"
        self._max_subs = int(config.get("max_subs") or 2)
        self._request_delay = float(config.get("request_delay") or 1.0)
        self._auto_sync = bool(config.get("auto_sync", False))
        self._auto_encoding = bool(config.get("auto_encoding", True))
        self._skip_sync_pattern = config.get("skip_sync_pattern") or ""
        self._save_path_strategy = config.get("save_path_strategy") or "same"
        self._custom_save_path = config.get("custom_save_path") or ""
        self._scan_cron = config.get("scan_cron") or ""
        self._scan_paths = config.get("scan_paths") or ""
        self._scan_limit = int(config.get("scan_limit") or 30)
        self._exclude_pattern = config.get("exclude_pattern") or ""
        self._subtitle_best_enabled = bool(config.get("subtitle_best_enabled", True))
        self.subtitle_best_api_key = (config.get("subtitle_best_api_key") or "").strip()
        self._assrt_enabled = bool(config.get("assrt_enabled", False))
        self.assrt_token = (config.get("assrt_token") or "").strip()
        self._xunlei_enabled = bool(config.get("xunlei_enabled", True))
        self._shooter_enabled = bool(config.get("shooter_enabled", False))

        if not self._subtitle_langs:
            self._subtitle_langs = [LANG_CHS]
        if self._tag_style not in TAG_STYLES:
            self._tag_style = "iso"

        logger.info(
            f"[CSF] 插件初始化：启用={self._enabled} 语言={self._subtitle_langs} "
            f"来源={[s.label for s in self.__active_sources()] or '无'}"
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """插件停用/重载时取消还在跑的扫描任务。"""
        task = self._scan_task
        if task and not task.done():
            try:
                task.cancel()
                logger.info("[CSF] 已取消进行中的扫描任务")
            except Exception as err:
                logger.debug(f"[CSF] 取消扫描任务失败: {err}")

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/download",
                "endpoint": self.api_download,
                "methods": ["POST"],
                "summary": "为指定视频补齐字幕",
                "description": "参数 path：视频文件绝对路径",
            },
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["POST"],
                "summary": "立即扫描媒体库补齐字幕",
                "description": "后台执行，返回是否已启动",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "summary": "查询字幕下载历史",
                "description": "返回最近 80 条记录",
            },
            {
                "path": "/sources",
                "endpoint": self.api_sources,
                "methods": ["GET"],
                "summary": "查询字幕来源状态",
                "description": "返回各来源的启用与凭据配置情况",
            },
        ]

    async def api_download(self, path: str) -> dict[str, Any]:
        """手动为单个视频补齐字幕。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        video = Path(path)
        if not video.exists() or not video.is_file():
            return {"success": False, "message": f"文件不存在: {path}"}
        req = self.__request_from_file(video)
        ok = await self.__process_video(req)
        return {
            "success": True,
            "message": "已下载字幕" if ok else "未找到可下载的字幕（或字幕已齐全）",
            "file": str(video),
        }

    async def api_scan(self) -> dict[str, Any]:
        """触发一次媒体库扫描（后台执行）。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        if self._scan_lock.locked():
            return {"success": False, "message": "已有扫描任务在运行"}
        # 保存引用，避免任务被 GC 回收
        self._scan_task = asyncio.create_task(self.__scan_library())
        return {"success": True, "message": "扫描任务已启动"}

    def api_history(self) -> list[dict[str, Any]]:
        return self.__load_history()

    def api_sources(self) -> list[dict[str, Any]]:
        active = {source.key for source in self.__active_sources()}
        result = []
        for source in (SubtitleBestSource(self), AssrtSource(self), XunleiSource(self), ShooterSource(self)):
            enabled = source.key in active
            reason = ""
            if source.key == "subtitle_best" and not self.subtitle_best_api_key:
                reason = "未填写 API Key"
            elif source.key == "assrt" and not self.assrt_token:
                reason = "未填写 Token"
            result.append(
                {
                    "source": source.key,
                    "name": source.label,
                    "enabled": enabled,
                    "ready": enabled and not reason,
                    "reason": reason,
                }
            )
        return result

    # ------------------------------------------------------------------
    # 定时任务
    # ------------------------------------------------------------------

    def get_service(self) -> list[dict[str, Any]]:
        if self._enabled and self._scan_cron:
            try:
                trigger = CronTrigger.from_crontab(self._scan_cron)
            except Exception as err:
                logger.error(f"[CSF] 扫描定时表达式无效: {self._scan_cron} ({err})")
                return []
            return [
                {
                    "id": "ChineseSubFinderScan",
                    "name": "扫描媒体库补齐中文字幕",
                    "trigger": trigger,
                    "func": self.__scan_library,
                    "kwargs": {},
                }
            ]
        return []

    # ------------------------------------------------------------------
    # 事件：整理完成
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.TransferComplete)
    async def on_transfer_complete(self, event: Any) -> None:
        """整理完成后，为新增的视频文件补齐中文字幕。"""
        if not self._enabled or not self._on_transfer:
            return

        payload = _as_dict(getattr(event, "event_data", None))
        meta = payload.get("meta")
        mediainfo = payload.get("mediainfo")
        transferinfo = payload.get("transferinfo")
        if not transferinfo:
            return

        media_type = _type_str(mediainfo)
        if media_type and media_type not in (MediaType.MOVIE.value, MediaType.TV.value):
            return

        videos = self.__videos_from_transfer(transferinfo)
        if not videos:
            return

        logger.info(f"[CSF] 收到整理完成事件，待处理 {len(videos)} 个视频")
        async with self._transfer_lock:
            for video in videos:
                try:
                    req = self.__request_from_media(video, meta, mediainfo)
                    await self.__process_video(req)
                    await asyncio.sleep(self._request_delay)
                except Exception as err:
                    logger.error(f"[CSF] 处理失败 {video}: {err}")

    def __videos_from_transfer(self, transferinfo: Any) -> list[Path]:
        """从整理结果中提取视频文件路径（含蓝光原盘目录）。"""
        candidates: list[str] = []
        target_item = _attr(transferinfo, "target_item")
        target_path = _attr(target_item, "path")
        if target_path:
            candidates.append(str(target_path))
        for item in _attr(transferinfo, "file_list_new") or []:
            if isinstance(item, str):
                candidates.append(item)

        videos: list[Path] = []
        for raw in candidates:
            path = Path(raw)
            if path.is_dir():
                video = self.__largest_video(path)
                if video:
                    videos.append(video)
                continue
            if path.suffix.lower() in VIDEO_EXTS:
                videos.append(path)
        # 去重保序
        seen: set[str] = set()
        result = []
        for video in videos:
            key = str(video)
            if key not in seen:
                seen.add(key)
                result.append(video)
        return result

    @staticmethod
    def __largest_video(folder: Path) -> Optional[Path]:
        """蓝光原盘等目录：取最大的视频文件。"""
        best: Optional[Path] = None
        best_size = 0
        try:
            for entry in folder.rglob("*"):
                if entry.suffix.lower() not in VIDEO_EXTS or not entry.is_file():
                    continue
                size = entry.stat().st_size
                if size > best_size:
                    best, best_size = entry, size
        except OSError:
            return None
        return best

    # ------------------------------------------------------------------
    # 检索请求构建
    # ------------------------------------------------------------------

    def __request_from_media(self, video: Path, meta: Any, mediainfo: Any) -> SubtitleRequest:
        """从整理事件里的 meta / mediainfo 构建检索请求。"""
        aliases: list[str] = []
        for value in (
            _attr(mediainfo, "hk_title"),
            _attr(mediainfo, "tw_title"),
            _attr(mediainfo, "sg_title"),
            _attr(mediainfo, "original_title"),
            _attr(meta, "cn_name"),
            _attr(meta, "en_name"),
        ):
            if value:
                aliases.append(str(value))
        for value in _attr(mediainfo, "names") or []:
            if isinstance(value, str) and value:
                aliases.append(value)

        season = _attr(meta, "begin_season")
        episode = _attr(meta, "begin_episode")
        media_type = _type_str(mediainfo)
        if media_type == MediaType.MOVIE.value:
            season, episode = None, None
        elif media_type == MediaType.TV.value:
            if season is None:
                season = _attr(mediainfo, "season") or 1
            if episode is None:
                episode = _attr(meta, "begin_episode")

        title = str(_attr(mediainfo, "title") or _attr(meta, "cn_name") or video.stem)
        en_title = str(_attr(mediainfo, "en_title") or _attr(meta, "en_name") or "")

        return SubtitleRequest(
            video_path=video,
            media_type=media_type or (MediaType.TV.value if episode else MediaType.MOVIE.value),
            title=title,
            en_title=en_title,
            aliases=list(dict.fromkeys(aliases)),
            year=str(_attr(mediainfo, "year") or ""),
            imdb_id=str(_attr(mediainfo, "imdb_id") or ""),
            season=season,
            episode=episode,
        )

    def __request_from_file(self, video: Path) -> SubtitleRequest:
        """仅凭文件名解析（定时扫描时没有 TMDB 信息）。"""
        try:
            meta = MetaInfo(title=video.stem)
        except Exception as err:
            logger.debug(f"[CSF] 文件名解析失败: {err}")
            meta = None

        season = getattr(meta, "begin_season", None) if meta else None
        episode = getattr(meta, "begin_episode", None) if meta else None
        if episode is None:
            matched = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", video.stem)
            if matched:
                season, episode = int(matched.group(1)), int(matched.group(2))
        if season is None and episode is not None:
            season = 1

        return SubtitleRequest(
            video_path=video,
            media_type=MediaType.TV.value if episode is not None else MediaType.MOVIE.value,
            title=str(getattr(meta, "cn_name", "") or getattr(meta, "name", "") or video.stem),
            en_title=str(getattr(meta, "en_name", "") or ""),
            aliases=[],
            year=str(getattr(meta, "year", "") or ""),
            imdb_id="",
            season=season,
            episode=episode,
        )

    # ------------------------------------------------------------------
    # 来源调度
    # ------------------------------------------------------------------

    def __active_sources(self) -> list[BaseSource]:
        sources: list[BaseSource] = []
        if self._subtitle_best_enabled and self.subtitle_best_api_key:
            sources.append(SubtitleBestSource(self))
        if self._assrt_enabled and self.assrt_token:
            sources.append(AssrtSource(self))
        if self._xunlei_enabled:
            sources.append(XunleiSource(self))
        if self._shooter_enabled:
            sources.append(ShooterSource(self))
        return sources

    async def __search(self, req: SubtitleRequest) -> list[SubCandidate]:
        sources = self.__active_sources()
        if not sources:
            logger.warning("[CSF] 没有可用的字幕来源，请检查插件配置")
            return []

        results = await asyncio.gather(*(s.search(req) for s in sources), return_exceptions=True)
        merged: list[SubCandidate] = []
        for source, result in zip(sources, results):
            if isinstance(result, BaseException):
                logger.warning(f"[CSF] {source.label} 检索异常: {result}")
                continue
            if result:
                logger.info(f"[CSF] {source.label} 命中 {len(result)} 条")
            merged.extend(result)

        merged.sort(key=self.__cand_sort_key, reverse=True)
        return merged

    def __cand_sort_key(self, cand: SubCandidate) -> tuple:
        """排序：语言偏好 > 双语 > 来源顺序 > 站点热度。"""
        info = cand.lang_info()
        lang_rank = 0
        if info.chinese and info.variant in self._subtitle_langs:
            lang_rank = len(self._subtitle_langs) - self._subtitle_langs.index(info.variant)
        bilingual = 1 if info.bilingual else 0
        try:
            source_rank = len(SOURCE_ORDER) - SOURCE_ORDER.index(cand.source)
        except ValueError:
            source_rank = 0
        return (lang_rank, bilingual, source_rank, min(cand.score, 1e6))

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def __process_video(self, req: SubtitleRequest) -> bool:
        """检查缺失语言 → 检索 → 下载 → 落盘，返回是否成功下载。"""
        video = req.video_path
        if not video.exists() or not video.is_file():
            logger.debug(f"[CSF] 视频不存在，跳过: {video}")
            return False

        required = set(self._subtitle_langs)
        satisfied = await self.__scan_existing(video)
        missing = required - satisfied
        if not missing:
            logger.debug(f"[CSF] 字幕已齐全: {video.name}")
            return False

        logger.info(f"[CSF] {video.name} 缺失: {'/'.join(sorted(missing))}")
        candidates = await self.__search(req)
        if not candidates:
            logger.info(f"[CSF] 未检索到可用字幕: {video.name}")
            return False

        downloaded = 0
        success = False
        for cand in candidates:
            if not missing:
                break
            if downloaded >= max(1, self._max_subs):
                break

            info = cand.lang_info()
            # 站点明确标注了语言且不覆盖缺失语言 → 跳过
            if info.chinese and not any(info.covers([m]) for m in missing):
                continue

            ok, sub_title, detected = await self.__download_and_place(cand, req)
            downloaded += 1
            if not ok or not detected:
                self.__add_history(req, cand, sub_title, "", False, "下载或解析失败")
                await asyncio.sleep(self._request_delay)
                continue

            covered = {m for m in missing if detected.covers([m])}
            if not covered:
                self.__add_history(req, cand, sub_title, detected.display, False, "语言不符")
                await asyncio.sleep(self._request_delay)
                continue

            missing -= covered
            success = True
            self.__add_history(req, cand, sub_title, detected.display, True, "")
            if self._notify:
                self.__notify(req, sub_title, detected)
            await asyncio.sleep(self._request_delay)

        if missing:
            logger.warning(f"[CSF] {video.name} 仍有缺失: {'/'.join(sorted(missing))}")
        return success

    # ------------------------------------------------------------------
    # 已有字幕检测
    # ------------------------------------------------------------------

    async def __scan_existing(self, video: Path) -> set[str]:
        """扫描外挂与内封字幕，返回已满足的语言集合。"""
        satisfied: set[str] = set()
        stem = video.stem
        stem_lower = stem.lower()
        sub_exts = {ext.lower() for ext in self._subtitle_exts}
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
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if entry.suffix.lower() not in sub_exts:
                    continue
                name_lower = entry.name.lower()
                tail = self.__subtitle_tail(name_lower, stem_lower)
                if tail is None:
                    continue
                variant = self.__variant_from_tag(tail)
                if variant:
                    satisfied.add(variant)
                    continue
                # 文件名没有语言标记 → 读内容判定
                detected = self.__detect_file(entry)
                if detected.chinese and detected.variant:
                    satisfied.add(detected.variant)

        if len(satisfied) < len(self._subtitle_langs):
            for variant in await self.__probe_embedded(video):
                satisfied.add(variant)

        return satisfied

    @staticmethod
    def __subtitle_tail(name_lower: str, stem_lower: str) -> Optional[str]:
        """
        返回字幕文件名去掉视频名后的语言标记部分；判断不属于该视频时返回 None。

        允许字幕名比视频名短（Show.S01E01.srt ↔ Show.S01E01.1080p.mkv），
        但字幕里出现季集标记时必须与视频一致，避免把同剧其他集的字幕算进来。
        """
        common = 0
        limit = min(len(name_lower), len(stem_lower))
        while common < limit and name_lower[common] == stem_lower[common]:
            common += 1
        if common < 6:
            return None

        # 回退到最近的分隔符，避免把分辨率、年份截断成半个词
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
    def __variant_from_tag(tail: str) -> str:
        """从文件名后缀判断简繁，无法判断返回空串。"""
        text = tail.lower()
        if re.search(r"zh[-_.]?tw|zh[-_.]?hant|cht|chi[-_.]?tra|hant|big5|繁", text):
            return LANG_CHT
        if re.search(r"zh[-_.]?cn|zh[-_.]?hans|zh[-_.]?sg|chs|chi[-_.]?sim|hans|简", text):
            return LANG_CHS
        return ""

    def __detect_file(self, path: Path) -> LangInfo:
        """读取字幕文件内容判定语言（只读前 256KB）。"""
        try:
            with path.open("rb") as fp:
                data = fp.read(256 * 1024)
        except OSError:
            return LangInfo()
        return detect_language(decode_subtitle_bytes(data))

    async def __probe_embedded(self, video: Path) -> set[str]:
        """用 ffprobe 检测内封字幕流。ffprobe 不可用时静默跳过。"""
        result: set[str] = set()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except (FileNotFoundError, OSError):
            return result

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "s", str(video),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not stdout:
                return result
            streams = json.loads(stdout).get("streams") or []
        except Exception as err:
            logger.debug(f"[CSF] ffprobe 解析失败: {err}")
            return result

        for stream in streams:
            tags = stream.get("tags") or {}
            lang = str(tags.get("language") or "").lower()
            title = str(tags.get("title") or "").lower()
            if re.search(r"zh[-_]?tw|cht|hant|chi[-_]?tra", lang) or any(k in title for k in ("繁", "cht", "hant", "tw")):
                result.add(LANG_CHT)
            elif re.search(r"zh|chi|zho", lang) or any(k in title for k in ("简", "chs", "hans", "cn", "中")):
                result.add(LANG_CHS)
        return result

    # ------------------------------------------------------------------
    # 下载 / 解包 / 落盘
    # ------------------------------------------------------------------

    async def __download_and_place(self, cand: SubCandidate, req: SubtitleRequest) -> tuple[bool, str, Optional[LangInfo]]:
        """下载一条候选字幕，解包、转码、调轴后落到视频目录。"""
        url = cand.url
        if not url:
            try:
                url = await self.__resolve_candidate(cand)
            except Exception as err:
                logger.warning(f"[CSF] {cand.source} 取下载地址失败: {err}")
                return False, cand.title, None
        if not url:
            return False, cand.title, None

        logger.info(f"[CSF] 下载字幕（{SOURCE_LABEL.get(cand.source, cand.source)}）{cand.title}")
        data = await self.__fetch(url)
        if not data:
            return False, cand.title, None

        work = self.get_data_path() / "downloads" / f"{datetime.now():%Y%m%d%H%M%S}-{int(time.time() * 1000) % 1000}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            files = await self.__unpack_payload(data, work, cand, url)
            if not files:
                logger.warning(f"[CSF] 未从下载内容中提取到字幕文件: {cand.title}")
                return False, cand.title, None

            files = self.__pick_episode_files(files, req)

            placed = 0
            last_info: Optional[LangInfo] = None
            for sub_file in files[:2]:
                try:
                    text = decode_subtitle_bytes(sub_file.read_bytes())
                except OSError:
                    continue
                info = detect_language(text)
                if not info.chinese:
                    logger.info(f"[CSF] 字幕内容不含中文，跳过: {sub_file.name}")
                    continue
                if self._auto_encoding:
                    self.__ensure_utf8(sub_file, text)
                if self._auto_sync:
                    await self.__sync(sub_file, req.video_path)
                dest = self.__place(sub_file, req, info)
                if dest:
                    placed += 1
                    last_info = info
                    logger.info(f"[CSF] 字幕已落盘: {dest}")

            if not placed:
                return False, cand.title, None
            return True, cand.title, last_info
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def __resolve_candidate(self, cand: SubCandidate) -> str:
        source = next((s for s in self.__active_sources() if s.key == cand.source), None)
        if source is None:
            # 来源被关闭时仍允许手动下载，构造一个临时实例
            classes = {
                "subtitle_best": SubtitleBestSource,
                "assrt": AssrtSource,
                "xunlei": XunleiSource,
                "shooter": ShooterSource,
            }
            cls = classes.get(cand.source)
            if cls is None:
                return ""
            source = cls(self)
        return await source.resolve(cand)

    async def __fetch(self, url: str) -> bytes:
        client = AsyncRequestUtils(
            ua=settings.USER_AGENT,
            proxies=settings.PROXY or None,
            timeout=60,
        )
        try:
            resp = await client.get_res(url)
        except Exception as err:
            logger.warning(f"[CSF] 字幕下载异常: {err}")
            return b""
        if not resp or resp.status_code != 200:
            code = getattr(resp, "status_code", "N/A")
            logger.warning(f"[CSF] 字幕下载失败 HTTP {code}: {url[:120]}")
            return b""
        return resp.content or b""

    async def __unpack_payload(self, data: bytes, work: Path, cand: SubCandidate, url: str) -> list[Path]:
        """判断是压缩包还是单文件，返回解出的字幕文件列表。"""
        ext = (cand.ext or "").lower()
        if ext not in {".zip", ".rar", ".7z"}:
            ext = sniff_ext(data)

        if ext in {".zip", ".rar", ".7z"}:
            archive = work / f"payload{ext}"
            archive.write_bytes(data)
            return await self.__unpack_archive(archive, work)

        target = work / f"subtitle{ext if ext in self._subtitle_exts else '.srt'}"
        target.write_bytes(data)
        return [target]

    async def __unpack_archive(self, archive: Path, work: Path) -> list[Path]:
        """解压 zip/rar/7z；系统缺少解压工具时给出明确提示。"""
        if archive.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(archive) as zf:
                    zf.extractall(work)
            except Exception as err:
                logger.warning(f"[CSF] ZIP 解压失败: {err}")
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
                logger.warning("[CSF] 需要解压 rar/7z，但容器内没有 7z / unrar / bsdtar，请安装后重试")
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
                except Exception as err:
                    logger.debug(f"[CSF] 解压命令失败 {cmd[0]}: {err}")
            if not ok:
                logger.warning(f"[CSF] 压缩包解压失败: {archive.name}")
                return []

        return [
            path
            for path in work.rglob("*")
            if path.is_file() and path.suffix.lower() in {ext.lower() for ext in self._subtitle_exts}
        ]

    @staticmethod
    def __pick_episode_files(files: list[Path], req: SubtitleRequest) -> list[Path]:
        """整季包里只保留目标集；匹配不到时保留原列表（单集字幕常见）。"""
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
            if any(re.search(p, name) for p in patterns):
                matched.append(path)
        return matched or files

    @staticmethod
    def __ensure_utf8(path: Path, text: str) -> None:
        """GBK/BIG5 字幕转成 UTF-8，避免播放器乱码。"""
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
            logger.info(f"[CSF] 字幕编码已转为 UTF-8: {path.name}")
        except OSError as err:
            logger.debug(f"[CSF] 字幕转码失败: {err}")

    async def __sync(self, sub_file: Path, video: Path) -> bool:
        """用 ffsubsync 以视频音轨为基准校正时间轴。"""
        if self._skip_sync_pattern:
            try:
                if re.search(self._skip_sync_pattern, sub_file.name):
                    return False
            except re.error:
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffsubsync", "--help",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except (FileNotFoundError, OSError):
            logger.warning("[CSF] 未安装 ffsubsync，跳过调轴")
            return False

        synced = sub_file.with_name(f"{sub_file.stem}.synced{sub_file.suffix}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffsubsync", str(video), "-i", str(sub_file), "-o", str(synced),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and synced.exists():
                synced.replace(sub_file)
                logger.info(f"[CSF] 调轴完成: {sub_file.name}")
                return True
            message = (stderr or b"").decode(errors="ignore")[:200]
            logger.warning(f"[CSF] 调轴失败: {message}")
        except Exception as err:
            logger.warning(f"[CSF] 调轴异常: {err}")
        return False

    def __place(self, sub_file: Path, req: SubtitleRequest, info: LangInfo) -> Optional[Path]:
        """按语言标签重命名并移动到目标目录。"""
        style = TAG_STYLES.get(self._tag_style, TAG_STYLES["iso"])
        tag = style.get(info.variant or LANG_CHS, "zh-CN")
        ext = sub_file.suffix.lower()
        target_dir = self.__target_dir(req)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            logger.error(f"[CSF] 创建字幕目录失败: {target_dir} ({err})")
            return None

        dest = target_dir / f"{req.video_path.stem}.{tag}{ext}"
        if dest.exists():
            logger.debug(f"[CSF] 目标字幕已存在，跳过: {dest.name}")
            return None
        try:
            shutil.move(str(sub_file), str(dest))
        except OSError as err:
            logger.error(f"[CSF] 字幕移动失败: {err}")
            return None
        return dest

    def __target_dir(self, req: SubtitleRequest) -> Path:
        if self._save_path_strategy == "custom" and self._custom_save_path:
            text = self._custom_save_path
            values = {
                "title": req.title or req.video_path.stem,
                "year": req.year or "",
                "type": req.media_type or "",
                "season": f"S{req.season:02d}" if req.season else "",
            }
            for key, value in values.items():
                text = text.replace(f"{{{key}}}", str(value))
            try:
                return Path(text)
            except (OSError, ValueError):
                logger.warning(f"[CSF] 自定义路径无效，回退到视频目录: {text}")
        return req.video_path.parent

    # ------------------------------------------------------------------
    # 通知 / 历史
    # ------------------------------------------------------------------

    def __notify(self, req: SubtitleRequest, sub_title: str, info: LangInfo) -> None:
        try:
            season_episode = ""
            if req.is_tv and req.season is not None and req.episode is not None:
                season_episode = f" S{req.season:02d}E{req.episode:02d}"
            self.post_message(
                mtype=MessageType.Plugin,
                title="【ChineseSubFinder】字幕已下载",
                text=(
                    f"{req.title} ({req.year}){season_episode}\n"
                    f"语言：{info.display}\n字幕：{sub_title}"
                ),
            )
        except Exception as err:
            logger.debug(f"[CSF] 通知发送失败: {err}")

    def __load_history(self) -> list[dict[str, Any]]:
        data = self.get_data(self._HISTORY_KEY)
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        return data if isinstance(data, list) else []

    def __add_history(
        self,
        req: SubtitleRequest,
        cand: SubCandidate,
        sub_title: str,
        language: str,
        success: bool,
        message: str,
    ) -> None:
        history = self.__load_history()
        history.insert(
            0,
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": req.title or req.video_path.stem,
                "year": req.year,
                "type": req.media_type,
                "season_episode": f"S{req.season:02d}E{req.episode:02d}" if req.is_tv and req.episode else "",
                "source": SOURCE_LABEL.get(cand.source, cand.source),
                "sub_title": sub_title,
                "lang": language,
                "success": success,
                "message": message,
                "file": req.video_path.name,
            },
        )
        self.save_data(self._HISTORY_KEY, json.dumps(history[: self._HISTORY_MAX], ensure_ascii=False))

    # ------------------------------------------------------------------
    # 媒体库扫描
    # ------------------------------------------------------------------

    async def __scan_library(self) -> None:
        """遍历媒体库目录，为缺字幕的视频补齐。"""
        if not self._enabled:
            return
        if self._scan_lock.locked():
            logger.info("[CSF] 已有扫描任务在运行，本次跳过")
            return

        async with self._scan_lock:
            roots = self.__library_paths()
            if not roots:
                logger.warning("[CSF] 未配置扫描目录，也未读取到媒体库目录")
                return

            state = self.__load_state()
            now = time.time()
            limit = max(1, self._scan_limit)
            processed = 0
            found = 0

            for root in roots:
                if processed >= limit:
                    break
                for video in self.__iter_videos(root):
                    if processed >= limit:
                        break
                    key = str(video)
                    if now - float(state.get(key) or 0) < 7 * 86400:
                        continue
                    if self.__excluded(video):
                        continue
                    processed += 1
                    try:
                        if await self.__process_video(self.__request_from_file(video)):
                            found += 1
                    except Exception as err:
                        logger.error(f"[CSF] 扫描处理失败 {video}: {err}")
                    state[key] = now
                    await asyncio.sleep(max(0.2, self._request_delay))

            self.__save_state(state)
            logger.info(f"[CSF] 扫描结束：检查 {processed} 个视频，补齐 {found} 个")

    def __library_paths(self) -> list[Path]:
        """优先使用配置的目录，其次读取 MoviePilot 媒体库目录配置。"""
        paths: list[Path] = []
        for raw in re.split(r"[,;\n]", self._scan_paths or ""):
            raw = raw.strip()
            if not raw:
                continue
            path = Path(raw)
            if path.is_dir():
                paths.append(path)
            else:
                logger.warning(f"[CSF] 扫描目录不存在: {raw}")

        if not paths:
            try:
                from app.application.directory import DirectoryHelper

                for item in DirectoryHelper().get_library_dirs():
                    value = getattr(item, "path", None)
                    if value:
                        paths.append(Path(str(value)))
            except Exception as err:
                logger.debug(f"[CSF] 读取媒体库目录失败: {err}")

        if not paths:
            legacy = getattr(settings, "LIBRARY_PATH", None)
            if legacy:
                paths = [Path(item) for item in legacy if Path(item).is_dir()]

        seen: set[str] = set()
        unique: list[Path] = []
        for path in paths:
            key = str(path)
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def __iter_videos(self, root: Path) -> Iterable[Path]:
        for current, dirs, files in os.walk(root):
            # 跳过隐藏目录与回收站
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in {"@eadir", "#recycle", "recycle_bin"}]
            for name in files:
                if Path(name).suffix.lower() in VIDEO_EXTS:
                    yield Path(current) / name

    def __excluded(self, video: Path) -> bool:
        if not self._exclude_pattern:
            return False
        try:
            return bool(re.search(self._exclude_pattern, str(video)))
        except re.error:
            return False

    def __load_state(self) -> dict[str, float]:
        data = self.get_data(self._STATE_KEY)
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return data if isinstance(data, dict) else {}

    def __save_state(self, state: dict[str, float]) -> None:
        # 只保留最近 2000 条，避免无限膨胀
        if len(state) > 2000:
            keep = sorted(state.items(), key=lambda kv: kv[1], reverse=True)[:2000]
            state = dict(keep)
        self.save_data(self._STATE_KEY, json.dumps(state))

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------

    def get_page(self) -> list[dict[str, Any]]:
        sources = self.api_sources()
        ready = [item["name"] for item in sources if item["ready"]]
        summary = (
            f"可用来源：{'、'.join(ready) if ready else '无（请填写 SubtitleBest API Key 或 Assrt Token）'}｜"
            f"目标语言：{'/'.join(self._subtitle_langs)}"
        )

        page: list[dict[str, Any]] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if ready else "warning",
                    "variant": "tonal",
                    "text": summary,
                    "class": "mb-3",
                },
            }
        ]

        history = self.__load_history()
        if not history:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无下载记录。整理完成或定时扫描后，这里会显示每次字幕下载的结果。",
                    },
                }
            )
            return page

        total = len(history)
        success = sum(1 for item in history if item.get("success"))
        page.append(
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": f"最近 {total} 条记录：成功 {success} 次，失败 {total - success} 次。",
                    "class": "mb-3",
                },
            }
        )

        for item in history:
            ok = bool(item.get("success"))
            meta_line = " · ".join(
                part
                for part in (
                    item.get("source", ""),
                    item.get("lang", ""),
                    item.get("season_episode", ""),
                )
                if part
            )
            detail = item.get("message") or item.get("sub_title", "")
            page.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-2", "variant": "outlined"},
                    "content": [
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "VRow",
                                    "props": {"align": "center", "dense": True},
                                    "content": [
                                        {
                                            "component": "VCol",
                                            "props": {"cols": "auto"},
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {
                                                        "class": (
                                                            "text-success font-weight-bold"
                                                            if ok
                                                            else "text-error font-weight-bold"
                                                        )
                                                    },
                                                    "text": "✓" if ok else "✗",
                                                }
                                            ],
                                        },
                                        {
                                            "component": "VCol",
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {"class": "font-weight-medium"},
                                                    "text": f"{item.get('title', '')} ({item.get('year', '')})",
                                                },
                                                {
                                                    "component": "br",
                                                    "props": {},
                                                },
                                                {
                                                    "component": "span",
                                                    "props": {"class": "text-caption text-grey"},
                                                    "text": f"[{item.get('time', '')}] {meta_line}",
                                                },
                                                {
                                                    "component": "br",
                                                    "props": {},
                                                },
                                                {
                                                    "component": "span",
                                                    "props": {"class": "text-caption"},
                                                    "text": detail,
                                                },
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
        return page

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------

    @staticmethod
    def __col_switch(model: str, label: str, md: int = 3) -> dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": "VSwitch", "props": {"model": model, "label": label}}],
        }

    @staticmethod
    def __col_text(
        model: str,
        label: str,
        placeholder: str = "",
        type_: str = "text",
        md: int = 6,
        show: str = "",
    ) -> dict[str, Any]:
        props: dict[str, Any] = {"model": model, "label": label}
        if placeholder:
            props["placeholder"] = placeholder
        if type_ != "text":
            props["type"] = type_
        if show:
            props["v-show"] = show
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": "VTextField", "props": props}],
        }

    @staticmethod
    def __col_select(
        model: str,
        label: str,
        items: list[dict[str, str]],
        multiple: bool = False,
        md: int = 6,
    ) -> dict[str, Any]:
        props: dict[str, Any] = {"model": model, "label": label, "items": items}
        if multiple:
            props.update({"multiple": True, "chips": True})
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": "VSelect", "props": props}],
        }

    @staticmethod
    def __row(*cols: dict[str, Any]) -> dict[str, Any]:
        return {"component": "VRow", "content": list(cols)}

    def get_form(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        form = [
            {
                "component": "VForm",
                "content": [
                    self.__row(
                        self.__col_switch("enabled", "启用插件"),
                        self.__col_switch("on_transfer", "整理完成后自动处理"),
                        self.__col_switch("notify", "发送通知"),
                        self.__col_switch("auto_encoding", "字幕转 UTF-8"),
                    ),
                    self.__row(
                        self.__col_select(
                            "subtitle_langs",
                            "需要的字幕语言",
                            [{"title": LANG_CHS, "value": LANG_CHS}, {"title": LANG_CHT, "value": LANG_CHT}],
                            multiple=True,
                        ),
                        self.__col_select(
                            "tag_style",
                            "字幕语言标签",
                            [
                                {"title": "zh-CN / zh-TW（Emby、Jellyfin 推荐）", "value": "iso"},
                                {"title": "chs / cht（老播放器）", "value": "legacy"},
                                {"title": "zh-Hans / zh-Hant（Plex 推荐）", "value": "plex"},
                            ],
                            md=6,
                        ),
                    ),
                    self.__row(
                        self.__col_text("max_subs", "每个视频最多下载条数", "2", type_="number", md=3),
                        self.__col_text("request_delay", "请求间隔（秒）", "1", type_="number", md=3),
                        self.__col_switch("auto_sync", "自动调轴（需 ffsubsync）", md=3),
                        self.__col_text("skip_sync_pattern", "不调轴关键字", "e.g. WEB|NF", md=3, show="auto_sync"),
                    ),
                    # 来源
                    self.__row(
                        self.__col_switch("subtitle_best_enabled", "启用 SubtitleBest"),
                        self.__col_text("subtitle_best_api_key", "SubtitleBest API Key", "在 subtitle.best 申请", type_="password", md=9),
                    ),
                    self.__row(
                        self.__col_switch("assrt_enabled", "启用 Assrt"),
                        self.__col_text("assrt_token", "Assrt Token", "assrt.net 用户中心获取", type_="password", md=9),
                    ),
                    self.__row(
                        self.__col_switch("xunlei_enabled", "启用迅雷字幕（按文件 cid 精确匹配）", md=6),
                        self.__col_switch("shooter_enabled", "启用 Shooter（站点可能已失效）", md=6),
                    ),
                    # 落盘
                    self.__row(
                        self.__col_select(
                            "save_path_strategy",
                            "字幕存放位置",
                            [
                                {"title": "视频同目录", "value": "same"},
                                {"title": "自定义目录", "value": "custom"},
                            ],
                            md=4,
                        ),
                        self.__col_text(
                            "custom_save_path",
                            "自定义目录模板",
                            "/media/subtitles/{type}/{title} ({year})",
                            md=8,
                            show="save_path_strategy === 'custom'",
                        ),
                    ),
                    # 扫描
                    self.__row(
                        self.__col_text("scan_cron", "定时扫描（cron）", "0 3 * * *，留空不启用", md=4),
                        self.__col_text("scan_limit", "每次扫描视频数上限", "30", type_="number", md=4),
                        self.__col_text("exclude_pattern", "排除路径正则", "e.g. /Adult/|/预告/", md=4),
                    ),
                    self.__row(
                        self.__col_text(
                            "scan_paths",
                            "扫描目录（逗号分隔，留空自动读取媒体库目录）",
                            "/media/movies,/media/tv",
                            md=12,
                        )
                    ),
                    self.__row(
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": (
                                            "SubtitleBest 按 IMDb 匹配、命中率最高，需要 API Key；"
                                            "迅雷/Shooter 按视频文件指纹匹配，无需账号；"
                                            "Assrt 按片名搜索，需要 Token。"
                                        ),
                                    },
                                }
                            ],
                        }
                    ),
                    self.__row(
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": "rar/7z 格式的字幕包需要容器内装有 7z、unrar 或 bsdtar，否则只能处理 zip。",
                                    },
                                }
                            ],
                        }
                    ),
                ],
            }
        ]

        defaults = {
            "enabled": False,
            "on_transfer": True,
            "notify": False,
            "auto_encoding": True,
            "subtitle_langs": [LANG_CHS],
            "tag_style": "iso",
            "max_subs": 2,
            "request_delay": 1.0,
            "auto_sync": False,
            "skip_sync_pattern": "",
            "subtitle_best_enabled": True,
            "subtitle_best_api_key": "",
            "assrt_enabled": False,
            "assrt_token": "",
            "xunlei_enabled": True,
            "shooter_enabled": False,
            "save_path_strategy": "same",
            "custom_save_path": "",
            "scan_cron": "",
            "scan_limit": 30,
            "scan_paths": "",
            "exclude_pattern": "",
        }
        return form, defaults
