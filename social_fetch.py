"""
社媒投研 M1 入口 —— **只人工触发**，跑一次即退出，不含任何调度（PRD-social-research.md §2 红线 1）。

用法：
    python social_fetch.py                    # 收件箱：串行拉一次启用的文字 RSS，列出新条目 + 体检
    python social_fetch.py --show SRC ITEM    # 深挖：从本地缓存打印某条正文（不联网）
    python social_fetch.py --inbox [N]        # 重看最近 N 条收件箱（不联网，默认 20）

深挖哪条由人勾选；本脚本不做观点抽取，抽取在会话内按 PRD §4.4/§4.7 完成。
"""
from __future__ import annotations

import argparse
import json
import sys

from data import social_rss as sr

_ICON = {"ok": "✅", "stale": "⚠️", "broken": "❌", "off": "·"}


def _fetch() -> int:
    sources = sr.load_registry()
    print(f"上限：≤{sr.MAX_FEEDS_PER_RUN} feed / ≤{sr.MAX_NEW_ITEMS_PER_RUN} 新条目 / "
          f"间隔 {sr.MIN_REQUEST_INTERVAL_S:.0f}s / 单响应 ≤{sr.MAX_FEED_BYTES // 1024 ** 2}MB；"
          f"启用 {sum(s.kind in sr.M1_KINDS for s in sources)} 个 feed\n")
    reports = sr.run(sources)
    print("## 体检")
    for r in reports:
        print(f"{_ICON[r.status]} {r.status:<6} {r.source_id:<10} 最新 {r.latest or '—':<10} {r.detail}")
    new = [row for r in reports for row in r.new_items]
    print(f"\n## 收件箱：{len(new)} 条新内容")
    for row in new:
        print(f"- [{(row['published'] or '')[:10]}] {row['creator']}｜{row['title']}（正文 {row['body_chars']} 字）\n"
              f"  深挖：python social_fetch.py --show {row['source_id']} '{row['item_id']}'\n  {row['link']}")
    return 1 if any(r.status == "broken" for r in reports) else 0


def _show(source_id: str, item_id: str) -> int:
    it = sr.read_item(source_id, item_id)
    print(f"# {it.title}\n{it.published}  {it.link}\n")
    print(sr.html_to_text(it.body_html))
    return 0


def _inbox(n: int) -> int:
    p = sr.CACHE_DIR / "inbox.jsonl"
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
    for row in rows[-n:]:
        print(f"[{(row['published'] or '')[:10]}] {row['source_id']:<10} {row['title']}  ←  {row['item_id']}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", nargs=2, metavar=("SOURCE_ID", "ITEM_ID"))
    ap.add_argument("--inbox", nargs="?", const=20, type=int)
    a = ap.parse_args()
    if a.show:
        sys.exit(_show(*a.show))
    if a.inbox is not None:
        sys.exit(_inbox(a.inbox))
    sys.exit(_fetch())
