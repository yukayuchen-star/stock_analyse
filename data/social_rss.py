"""
社媒投研 M1：文字 RSS 增量拉取（PRD-social-research.md §3.3 / §4.6 / 附录 A）。

合规红线（用户 2026-10-04 定）代码化在这里，不靠自觉：
- 只人工触发：本模块不含任何调度；入口 `social_fetch.py` 跑一次即退出。
- 不批量：只请求 `creators.txt` 里启用的 feed，串行 + 间隔 ≥ MIN_REQUEST_INTERVAL_S，
  单次 ≤ MAX_FEEDS_PER_RUN 个 feed、≤ MAX_NEW_ITEMS_PER_RUN 条新条目；超限即停，不截断后继续。
- 只读登记过的地址：重定向跨域即拒绝（白名单是「登记的 feed 主机」，不是黑名单）。
- 不登录、无 cookie、User-Agent 如实标识自身；带 ETag / Last-Modified 条件请求，无更新不重复下载。
- 只读 feed 本身，不跟进条目链接、不抓网页。

落盘（全部在 gitignore 的 cache/social/ 下）：
- `state.json`  每个 feed 的游标（已见条目 id、ETag、上次成功时间），原子写。
- `feeds/<source_id>.xml`  最近一次 200 响应的 feed 原文 —— 等同 RSS 阅读器的本地缓存，
  只供会话内深挖时读取，**不转载、不进仓库**；观点台账只存短引用（PRD §4.7）。
- `inbox.jsonl`  新条目的元数据（标题/日期/链接），append-only；深挖哪条由人勾选。

体检四态（借鉴 Agent-Reach `probe.py`，并多一个 stale —— 「看着正常、实则停更」比报错更危险）：
ok | stale（能解析但最新条目早于 STALE_DAYS） | broken（网络/HTTP/解析失败） | off（未启用）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

# ── 硬上限（PRD §4.6，2026-10-05 用户定 20）──────────────────────
MAX_FEEDS_PER_RUN = 20
MAX_NEW_ITEMS_PER_RUN = 30
MAX_FEED_BYTES = 2 * 1024 ** 2
MIN_REQUEST_INTERVAL_S = 2.0
REQUEST_TIMEOUT_S = 20
STALE_DAYS = 60                 # Damodaran 发文 < 1 篇/周，间隔一个月属正常
FIRST_RUN_LOOKBACK_DAYS = 30    # 首次运行只把近 30 天的条目当新条目，更早的直接记为已见（不回填）

USER_AGENT = "stock_analyse-social-reader/0.1 (personal research; manually triggered; no automation)"
KINDS = {"text_rss", "podcast_rss", "youtube_rss"}
M1_KINDS = {"text_rss"}         # podcast 等 M3 转写批准、youtube 等频道 ID 到位后再开

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "creators.txt"
CACHE_DIR = ROOT / "cache" / "social"

_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
_SECRET_QS = re.compile(r"([?&#](?:key|api[_-]?key|token|access[_-]?token|signature|sig)=)[^&#\s]*", re.I)


def scrub(text) -> str:
    """出口脱敏：异常信息与报告里不得带出 URL 上的凭证（Data API key 走查询参数）。"""
    return _SECRET_QS.sub(r"\1***", str(text))


# ── 登记表 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    source_id: str
    kind: str
    url: str
    creator: str
    layer: str

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


def load_registry(path: Path = REGISTRY) -> List[Source]:
    """读 `creators.txt` 中启用（非 # 开头）的行。格式错误直接报错 —— 登记表是红线的一部分，不容模糊。"""
    out: List[Source] = []
    seen = set()
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5:
            raise ValueError(f"creators.txt:{n} 应为 5 个字段，实为 {len(parts)}")
        sid, kind, url, creator, layer = parts
        sp = urlsplit(url)
        if kind not in KINDS:
            raise ValueError(f"creators.txt:{n} 未知 kind {kind!r}")
        if sp.scheme != "https" or not sp.hostname or sp.username or sp.password:
            raise ValueError(f"creators.txt:{n} feed 须为不带凭证的 https 地址")
        if sid in seen:
            raise ValueError(f"creators.txt:{n} source_id {sid!r} 重复")
        seen.add(sid)
        out.append(Source(sid, kind, url, creator, layer))
    return out


# ── 解析（Atom: Blogger；RSS 2.0: Substack）─────────────────────

@dataclass
class Item:
    item_id: str
    title: str
    link: str
    published: Optional[datetime]
    body_html: str = field(default="", repr=False)


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(data: bytes) -> List[Item]:
    root = ET.fromstring(data)
    items: List[Item] = []
    if root.tag == f"{_ATOM}feed":
        for e in root.findall(f"{_ATOM}entry"):
            link = ""
            for l in e.findall(f"{_ATOM}link"):
                if l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            body = e.findtext(f"{_ATOM}content") or e.findtext(f"{_ATOM}summary") or ""
            items.append(Item(
                item_id=(e.findtext(f"{_ATOM}id") or link).strip(),
                title=(e.findtext(f"{_ATOM}title") or "").strip(),
                link=link,
                published=_parse_date(e.findtext(f"{_ATOM}published") or e.findtext(f"{_ATOM}updated")),
                body_html=body,
            ))
    elif root.tag == "rss":
        for e in root.iter("item"):
            link = (e.findtext("link") or "").strip()
            items.append(Item(
                item_id=(e.findtext("guid") or link).strip(),
                title=(e.findtext("title") or "").strip(),
                link=link,
                published=_parse_date(e.findtext("pubDate")),
                body_html=e.findtext(_CONTENT) or e.findtext("description") or "",
            ))
    else:
        raise ValueError(f"不认识的 feed 根元素 {root.tag!r}")
    if any(not it.item_id for it in items):
        raise ValueError("存在无 id 也无 link 的条目，无法做增量游标")
    return items


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "blockquote", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(s: str) -> str:
    p = _TextExtractor()
    p.feed(s)
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


# ── 网络（拒绝跨域重定向、大小上限、条件请求）──────────────────────

class CrossHostRedirect(Exception):
    pass


def _same_host_opener(allowed_host: str) -> urllib.request.OpenerDirector:
    class _Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            sp = urlsplit(newurl)
            if sp.scheme != "https" or (sp.hostname or "").lower() != allowed_host:
                raise CrossHostRedirect(f"重定向到未登记地址 {scrub(newurl)}，已拒绝")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    # 不装 HTTPCookieProcessor：零 cookie
    return urllib.request.build_opener(_Guard)


@dataclass
class FetchResult:
    status: int                      # 200 / 304
    body: bytes = b""
    etag: Optional[str] = None
    last_modified: Optional[str] = None


def http_fetch(src: Source, etag: Optional[str], last_modified: Optional[str]) -> FetchResult:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(src.url, headers=headers)
    try:
        with _same_host_opener(src.host).open(req, timeout=REQUEST_TIMEOUT_S) as r:
            body = r.read(MAX_FEED_BYTES + 1)
            if len(body) > MAX_FEED_BYTES:
                raise ValueError(f"响应超过 {MAX_FEED_BYTES} 字节上限，已停止")
            return FetchResult(r.status, body, r.headers.get("ETag"), r.headers.get("Last-Modified"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return FetchResult(304)
        raise


# ── 状态（原子写）───────────────────────────────────────────────

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_state(cache_dir: Path) -> Dict[str, dict]:
    p = cache_dir / "state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── 主流程 ──────────────────────────────────────────────────────

@dataclass
class FeedReport:
    source_id: str
    status: str                  # ok | stale | broken | off
    detail: str = ""
    new_items: List[dict] = field(default_factory=list)
    latest: Optional[str] = None


def run(sources: List[Source],
        cache_dir: Path = CACHE_DIR,
        fetch: Callable[[Source, Optional[str], Optional[str]], FetchResult] = http_fetch,
        now: Optional[datetime] = None,
        sleep: Callable[[float], None] = time.sleep) -> List[FeedReport]:
    """对启用的 M1 源串行拉一次。单个源失败只记 broken，不拖垮其余源；超上限则整体拒绝运行。"""
    now = now or datetime.now(timezone.utc)
    active = [s for s in sources if s.kind in M1_KINDS]
    if len(active) > MAX_FEEDS_PER_RUN:
        raise RuntimeError(f"启用的 feed {len(active)} 个 > 上限 {MAX_FEEDS_PER_RUN}，拒绝运行（先在 creators.txt 停用一部分）")
    reports = [FeedReport(s.source_id, "off", f"kind={s.kind} 未在 M1 启用") for s in sources if s.kind not in M1_KINDS]

    state = load_state(cache_dir)
    budget = MAX_NEW_ITEMS_PER_RUN
    inbox_rows: List[dict] = []
    for i, src in enumerate(active):
        if i:
            sleep(MIN_REQUEST_INTERVAL_S)
        st = state.get(src.source_id, {})
        rep = FeedReport(src.source_id, "broken")
        try:
            res = fetch(src, st.get("etag"), st.get("last_modified"))
            feed_path = cache_dir / "feeds" / f"{src.source_id}.xml"
            if res.status == 304:
                if not feed_path.exists():
                    raise ValueError("服务端返回 304 但本地无缓存，下次将不带条件头重拉")
                items = parse_feed(feed_path.read_bytes())
            else:
                items = parse_feed(res.body)        # 先解析成功再落盘，坏响应不覆盖好缓存
                _atomic_write(feed_path, res.body)
                st["etag"], st["last_modified"] = res.etag, res.last_modified
            seen = set(st.get("seen", []))
            first_run = "seen" not in st
            cutoff = now - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
            fresh = []
            for it in items:
                if it.item_id in seen:
                    continue
                if first_run and (it.published is None or it.published < cutoff):
                    seen.add(it.item_id)            # 首次运行：旧条目只记为已见，不回填
                    continue
                fresh.append(it)
            fresh.sort(key=lambda it: it.published or now)
            take, rest = fresh[:budget], fresh[budget:]
            budget -= len(take)
            for it in take:
                seen.add(it.item_id)
                row = {"source_id": src.source_id, "creator": src.creator, "layer": src.layer,
                       "item_id": it.item_id, "title": it.title, "link": it.link,
                       "published": it.published.isoformat() if it.published else None,
                       "fetched_at": now.isoformat(timespec="seconds"),
                       "body_chars": len(html_to_text(it.body_html))}
                rep.new_items.append(row)
                inbox_rows.append(row)
            dated = [it.published for it in items if it.published]
            latest = max(dated) if dated else None
            rep.latest = latest.date().isoformat() if latest else None
            st["seen"] = sorted(seen)
            st["last_ok"] = now.isoformat(timespec="seconds")
            state[src.source_id] = st
            if latest is None or latest < now - timedelta(days=STALE_DAYS):
                rep.status, rep.detail = "stale", f"最新条目 {rep.latest or '无日期'}，早于 {STALE_DAYS} 天"
            else:
                rep.status = "ok"
                rep.detail = "304 未更新" if res.status == 304 else f"{len(items)} 条"
            if rest:
                rep.detail += f"；另有 {len(rest)} 条因单次上限 {MAX_NEW_ITEMS_PER_RUN} 留待下次（未记为已见）"
        except Exception as e:  # 单源失败如实记 broken，不静默、不影响其余源
            rep.detail = scrub(f"{type(e).__name__}: {e}")
        reports.append(rep)

    _atomic_write(cache_dir / "state.json", json.dumps(state, ensure_ascii=False, indent=1).encode())
    if inbox_rows:
        with open(cache_dir / "inbox.jsonl", "a", encoding="utf-8") as f:
            for row in inbox_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return reports


def read_item(source_id: str, item_id: str, cache_dir: Path = CACHE_DIR) -> Item:
    """深挖用：从本地 feed 缓存取条目全文，不发网络请求。"""
    for it in parse_feed((cache_dir / "feeds" / f"{source_id}.xml").read_bytes()):
        if it.item_id == item_id or it.link == item_id:
            return it
    raise KeyError(f"{source_id} 的本地缓存中没有 {item_id}（可能已滚出 feed 窗口）")
