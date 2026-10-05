"""`data.social_rss` 的契约测试（PRD-social-research.md 附录 A ⑧）。

运行：
    .venv/bin/python -m unittest tests.test_social_rss -v

**全程禁止联网**：`setUp` 把 `urllib.request.urlopen` 与 opener 换成会直接报错的桩，
网络行为只通过注入的 `fetch` 假函数测试。断言的是红线不变量（上限、增量、不回填、跨域拒绝、
单源失败不拖垮整体），而不是逐个 feed 写用例。
"""
from __future__ import annotations

import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from data import social_rss as sr

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def _atom(entries):
    body = "".join(
        f"<entry><id>{i}</id><title>T{i}</title><published>{d}</published>"
        f"<link rel='alternate' href='https://x.blogspot.com/{i}'/>"
        f"<content type='html'>&lt;p&gt;hello {i}&lt;/p&gt;</content></entry>"
        for i, d in entries)
    return f"<feed xmlns='http://www.w3.org/2005/Atom'>{body}</feed>".encode()


def _rss(entries):
    body = "".join(
        f"<item><guid>{i}</guid><title>T{i}</title><link>https://y.substack.com/p/{i}</link>"
        f"<pubDate>{d}</pubDate><content:encoded><![CDATA[<p>body {i}</p><script>x</script>]]></content:encoded></item>"
        for i, d in entries)
    return (f"<rss xmlns:content='http://purl.org/rss/1.0/modules/content/'><channel>{body}</channel></rss>").encode()


def _src(sid="a", kind="text_rss", url="https://x.blogspot.com/feeds/posts/default"):
    return sr.Source(sid, kind, url, "C", "L")


def _days_ago(n):
    return (NOW - timedelta(days=n)).isoformat()


class NoNetwork(unittest.TestCase):
    def setUp(self):
        def boom(*a, **k):
            raise AssertionError("测试中禁止联网")
        p1 = mock.patch.object(urllib.request, "urlopen", boom)
        p2 = mock.patch.object(urllib.request.OpenerDirector, "open", boom)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self.tmp = Path(tempfile.mkdtemp())

    def run_(self, sources, fetch):
        return sr.run(sources, cache_dir=self.tmp, fetch=fetch, now=NOW, sleep=lambda s: None)


class ParseTest(unittest.TestCase):
    def test_atom_and_rss(self):
        a = sr.parse_feed(_atom([("1", "2026-10-01T10:00:00-04:00")]))
        self.assertEqual((a[0].item_id, a[0].link), ("1", "https://x.blogspot.com/1"))
        self.assertEqual(sr.html_to_text(a[0].body_html), "hello 1")
        r = sr.parse_feed(_rss([("g1", "Thu, 01 Oct 2026 10:00:00 GMT")]))
        self.assertEqual(r[0].published, datetime(2026, 10, 1, 10, tzinfo=timezone.utc))
        self.assertEqual(sr.html_to_text(r[0].body_html), "body g1")   # script 被剔除

    def test_unknown_root_rejected(self):
        with self.assertRaises(ValueError):
            sr.parse_feed(b"<html><body>captcha</body></html>")   # 验证页不得当正文

    def test_scrub(self):
        self.assertEqual(sr.scrub("https://h/x?part=a&key=SECRET&b=1"), "https://h/x?part=a&key=***&b=1")


class RegistryTest(unittest.TestCase):
    def _load(self, text):
        p = Path(tempfile.mkdtemp()) / "c.txt"
        p.write_text(text, encoding="utf-8")
        return sr.load_registry(p)

    def test_comments_disabled_and_fields(self):
        s = self._load("# x\na | text_rss | https://h.com/f | C | L\n# b | text_rss | https://h.com/g | C | L\n")
        self.assertEqual([x.source_id for x in s], ["a"])

    def test_rejects_bad_rows(self):
        for bad in ("a | text_rss | http://h.com/f | C | L",            # 非 https
                    "a | text_rss | https://u:p@h.com/f | C | L",       # 带凭证
                    "a | scrape | https://h.com/f | C | L",             # 未知 kind
                    "a | text_rss | https://h.com/f | C",               # 字段数
                    "a | text_rss | https://h.com/f | C | L\na | text_rss | https://h.com/g | C | L"):
            with self.assertRaises(ValueError, msg=bad):
                self._load(bad)


class RunTest(NoNetwork):
    def test_first_run_no_backfill_then_incremental(self):
        feed = _atom([("old", _days_ago(90)), ("new", _days_ago(3))])
        r = self.run_([_src()], lambda s, e, l: sr.FetchResult(200, feed, '"e1"'))
        self.assertEqual([x["item_id"] for x in r[0].new_items], ["new"])   # 90 天前的不回填
        r = self.run_([_src()], lambda s, e, l: sr.FetchResult(200, feed, '"e1"'))
        self.assertEqual(r[0].new_items, [])                                 # 二次运行零新增
        self.assertEqual(len((self.tmp / "inbox.jsonl").read_text().splitlines()), 1)

    def test_conditional_request_and_304(self):
        feed = _atom([("n", _days_ago(1))])
        self.run_([_src()], lambda s, e, l: sr.FetchResult(200, feed, '"e1"', "Mon"))
        got = {}
        def f(s, e, l):
            got.update(etag=e, lm=l)
            return sr.FetchResult(304)
        r = self.run_([_src()], f)
        self.assertEqual(got, {"etag": '"e1"', "lm": "Mon"})
        self.assertEqual((r[0].status, r[0].new_items), ("ok", []))

    def test_item_cap_leaves_rest_unseen(self):
        feed = _atom([(str(i), _days_ago(1)) for i in range(sr.MAX_NEW_ITEMS_PER_RUN + 5)])
        f = lambda s, e, l: sr.FetchResult(200, feed)
        r = self.run_([_src()], f)
        self.assertEqual(len(r[0].new_items), sr.MAX_NEW_ITEMS_PER_RUN)
        r = self.run_([_src()], f)
        self.assertEqual(len(r[0].new_items), 5)          # 超出部分下次补，不丢

    def test_feed_cap_refuses_whole_run(self):
        srcs = [_src(str(i)) for i in range(sr.MAX_FEEDS_PER_RUN + 1)]
        with self.assertRaises(RuntimeError):
            self.run_(srcs, lambda s, e, l: self.fail("超上限时不得发出任何请求"))

    def test_one_broken_source_does_not_sink_others(self):
        good = _atom([("n", _days_ago(1))])
        def f(s, e, l):
            if s.source_id == "bad":
                raise OSError("boom ?key=SECRET")
            return sr.FetchResult(200, good)
        r = {x.source_id: x for x in self.run_([_src("bad"), _src("good")], f)}
        self.assertEqual((r["bad"].status, r["good"].status), ("broken", "ok"))
        self.assertNotIn("SECRET", r["bad"].detail)

    def test_bad_response_does_not_overwrite_good_cache(self):
        good = _atom([("n", _days_ago(1))])
        self.run_([_src()], lambda s, e, l: sr.FetchResult(200, good))
        r = self.run_([_src()], lambda s, e, l: sr.FetchResult(200, b"<html>blocked</html>"))
        self.assertEqual(r[0].status, "broken")
        self.assertEqual((self.tmp / "feeds" / "a.xml").read_bytes(), good)

    def test_stale_and_off(self):
        feed = _atom([("n", _days_ago(sr.STALE_DAYS + 1))])
        r = {x.source_id: x for x in self.run_(
            [_src(), _src("p", kind="podcast_rss")], lambda s, e, l: sr.FetchResult(200, feed))}
        self.assertEqual((r["a"].status, r["p"].status), ("stale", "off"))

    def test_read_item_offline(self):
        self.run_([_src()], lambda s, e, l: sr.FetchResult(200, _atom([("n", _days_ago(1))])))
        self.assertEqual(sr.read_item("a", "n", cache_dir=self.tmp).title, "Tn")


class RedirectGuardTest(unittest.TestCase):
    def test_cross_host_redirect_rejected(self):
        h = [x for x in sr._same_host_opener("x.blogspot.com").handlers
             if isinstance(x, urllib.request.HTTPRedirectHandler)][0]
        req = urllib.request.Request("https://x.blogspot.com/f")
        with self.assertRaises(sr.CrossHostRedirect):
            h.redirect_request(req, None, 302, "", {}, "https://evil.test/f")
        with self.assertRaises(sr.CrossHostRedirect):
            h.redirect_request(req, None, 302, "", {}, "http://x.blogspot.com/f")   # 降级到 http 也拒绝
        self.assertIsNotNone(h.redirect_request(req, None, 302, "", {}, "https://x.blogspot.com/g"))

    def test_no_cookie_handler(self):
        names = {type(x).__name__ for x in sr._same_host_opener("h").handlers}
        self.assertNotIn("HTTPCookieProcessor", names)


if __name__ == "__main__":
    unittest.main()
