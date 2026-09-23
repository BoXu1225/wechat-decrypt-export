"""Tests for formatters.py (stdlib unittest; synthetic records only).

Run: ./venv/bin/python -m unittest discover -s tests -v
"""
import csv
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import formatters as F  # noqa: E402

T0 = int(datetime(2026, 3, 1, 9, 5, 7).timestamp())
T1 = int(datetime(2026, 3, 1, 23, 59, 59).timestamp())
T2 = int(datetime(2026, 3, 2, 0, 0, 1).timestamp())
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)> & "quote"'


def rec(ts, sender, text, is_self=False, kind="text", image_path=None):
    r = {"ts": ts, "sender": sender, "is_self": is_self, "kind": kind, "text": text}
    if image_path is not None:
        r["image_path"] = image_path
    return r


def legacy_txt(records):
    """Reproduces the pre-formatters export_chat.py output exactly."""
    lines = []
    for r in records:
        time_str = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{time_str}] {r['sender']}: {r['text']}")
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.img = os.path.join(self.dir, "images", "a b 图.jpg")
        os.makedirs(os.path.dirname(self.img))
        open(self.img, "wb").close()
        self.records = [
            rec(T0, "我", "你好，世界", is_self=True),
            rec(T0 + 1, "张三", XSS),
            rec(T0 + 2, "张三", ""),
            rec(T1, "李四", "第一行\n第二行\n\n第四行"),
            rec(T1 + 1, "张三", "[图片]", kind="image", image_path=self.img),
            rec(T2, "我", "[图片]", is_self=True, kind="image"),  # no decoded file
            rec(T2 + 5, "", "张三 撤回了一条消息", kind="system"),
        ]
        self.group = {"name": "家庭群<b>", "is_group": True}
        self.single = {"name": "张三", "is_group": False}

    def tearDown(self):
        self.tmp.cleanup()

    def out(self, name):
        return os.path.join(self.dir, name)

    def read(self, path, enc="utf-8"):
        with open(path, encoding=enc, newline="") as f:
            return f.read()


class TestTxt(Base):
    def test_matches_legacy_format_exactly(self):
        buf = io.StringIO()
        F.write_txt(self.records, self.single, buf)
        self.assertEqual(buf.getvalue(), legacy_txt(self.records))
        self.assertIn(f"[2026-03-01 09:05:07] 我: 你好，世界\n", buf.getvalue())

    def test_file_bytes_identical_and_append(self):
        p = self.out("c.txt")
        F.write(self.records[:3], self.single, p, "txt")
        F.write(self.records[3:], self.single, p, "txt", append=True)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), legacy_txt(self.records).encode("utf-8"))


class TestMarkdown(Base):
    def test_structure(self):
        p = self.out("c.md")
        F.write(self.records, self.group, p, "md")
        s = self.read(p)
        self.assertEqual(re.findall(r"^## (.+)$", s, re.M), ["2026-03-01", "2026-03-02"])
        self.assertIn("**我** 09:05  你好，世界\n", s)
        self.assertIn("第一行  \n第二行", s)
        self.assertIn("![[图片]](images/a%20b%20%E5%9B%BE.jpg)", s)
        self.assertIn("**我** 00:00  [图片]\n", s)  # no image_path -> label only

    def test_no_raw_html(self):
        p = self.out("c.md")
        records = self.records + [rec(T2 + 9, "<b>x</b>", "a < b")]
        F.write(records, self.group, p, "md")
        s = self.read(p)
        self.assertNotIn("<", s)
        self.assertIn("# 家庭群&lt;b>\n", s)
        self.assertIn('&lt;script>alert("x")&lt;/script>&lt;img src=x onerror=alert(1)>', s)
        self.assertIn("**&lt;b>x&lt;/b>** 00:00  a &lt; b\n", s)

    def test_append_does_not_repeat_heading(self):
        p = self.out("c.md")
        F.write(self.records[:2], self.group, p, "md")
        F.write(self.records[2:4], self.group, p, "md", append=True)
        F.write(self.records[4:], self.group, p, "md", append=True)
        s = self.read(p)
        self.assertEqual(re.findall(r"^## (.+)$", s, re.M), ["2026-03-01", "2026-03-02"])
        self.assertEqual(s.count("# 家庭群"), 1)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.imgs, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "img":
            self.imgs.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


class TestHtml(Base):
    def render(self, meta):
        p = self.out("sub/c.html")
        F.write(self.records, meta, p, "html")
        return self.read(p)

    def test_escaping_and_no_injection(self):
        s = self.render(self.group)
        c = _Collector()
        c.feed(s)
        self.assertNotIn("script", c.tags)
        self.assertEqual(len(c.imgs), 1)  # only the real image, not the injected one
        self.assertIn(XSS, "".join(c.text))  # round-trips as literal text
        self.assertIn("家庭群&lt;b&gt;", s)
        self.assertNotIn("<b>", s)

    def test_layout(self):
        s = self.render(self.group)
        self.assertEqual(s.count('class="day"'), 2)
        self.assertIn('class="msg self"', s)
        self.assertIn('<div class="name">张三</div>', s)
        self.assertNotIn('<div class="name">我</div>', s)
        self.assertIn("第一行\n第二行\n\n第四行", s)  # newlines kept (pre-wrap)
        self.assertIn("white-space:pre-wrap", s)
        self.assertIn("prefers-color-scheme:dark", s)
        self.assertIn('name="viewport"', s)
        self.assertIn('class="sys"', s)

    def test_image_relative_lazy(self):
        c = _Collector()
        c.feed(self.render(self.group))
        img = c.imgs[0]
        self.assertEqual(img["loading"], "lazy")
        self.assertEqual(img["src"], "../images/a%20b%20%E5%9B%BE.jpg")

    def test_one_on_one_has_no_names(self):
        self.assertNotIn('class="name"', self.render(self.single))

    def test_self_contained(self):
        s = self.render(self.group)
        self.assertIsNone(re.search(r"(src|href)=\"https?:|@import|url\(", s))

    def test_no_append(self):
        self.assertFalse(F.supports_append("html"))
        with self.assertRaises(ValueError):
            F.write(self.records, self.group, self.out("c.html"), "html", append=True)


class TestJson(Base):
    def test_roundtrip(self):
        p = self.out("c.json")
        F.write(self.records, self.group, p, "json")
        raw = self.read(p)
        self.assertIn("你好，世界", raw)  # ensure_ascii=False
        d = json.loads(raw)
        self.assertEqual(d["chat"], self.group)
        self.assertEqual(len(d["messages"]), len(self.records))
        m0 = d["messages"][0]
        self.assertEqual(m0["time"], "2026-03-01T09:05:07")
        self.assertEqual(m0["ts"], T0)
        self.assertEqual(d["messages"][1]["text"], XSS)
        self.assertEqual(d["messages"][4]["image_path"], "images/a b 图.jpg")
        self.assertNotIn("image_path", d["messages"][5])
        with self.assertRaises(ValueError):
            F.write(self.records, self.group, p, "json", append=True)


class TestCsv(Base):
    def test_bom_columns_and_append(self):
        p = self.out("c.csv")
        F.write(self.records[:4], self.group, p, "csv")
        F.write(self.records[4:], self.group, p, "csv", append=True)
        with open(p, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.count(b"\xef\xbb\xbf"), 1)
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["time", "sender", "is_self", "kind", "text"])
        self.assertEqual(len(rows), 1 + len(self.records))
        self.assertEqual(rows[1], ["2026-03-01 09:05:07", "我", "1", "text", "你好，世界"])
        self.assertEqual(rows[2][4], XSS)
        self.assertEqual(rows[4][4], "第一行\n第二行\n\n第四行")
        self.assertEqual(rows[3][4], "")


class TestRich(Base):
    """Records with msg_parse "extra" (quote, link, forwarded chat bundle)."""

    def setUp(self):
        super().setUp()
        evil_url = "javascript:alert(1)"
        self.rich = [
            rec(T0, "张三", "好的 [引用 李四: 明天<b>见</b>]", kind="quote"),
            rec(T0 + 1, "张三", "[链接] 标题<i> (来源) https://example.com/a?x=1&y=(2)", kind="link"),
            rec(T0 + 2, "张三", "[链接] 坏链接", kind="link"),
            rec(T0 + 3, "我", "[聊天记录] 群聊的聊天记录 (2条)", is_self=True, kind="chat_history"),
            rec(T0 + 4, "张三", "[拍一拍] 张三 拍了拍 我", kind="pat"),
            rec(T0 + 5, "张三", "[转账] ¥1.00 已收款", kind="transfer"),
        ]
        self.rich[0]["extra"] = {"reply": "好的", "quote": {"sender": "李四<x>",
                                                            "text": "明天<b>见</b>", "kind": "text"}}
        self.rich[1]["extra"] = {"title": "标题<i>", "url": "https://example.com/a?x=1&y=(2)",
                                 "desc": "描述\"><script>", "source": "来源"}
        self.rich[2]["extra"] = {"title": "坏链接", "url": evil_url}
        self.rich[3]["extra"] = {"title": "群聊的聊天记录", "items": [
            {"sender": "A<script>", "time": "2023-6-20 19:45", "kind": "text", "text": "第一行\n第二行"},
            {"sender": "B", "time": "2023-6-20 19:46", "kind": "chat_history",
             "text": "[聊天记录] 内层", "items": [
                 {"sender": "C", "time": "2023-6-20 10:00", "kind": "link", "text": "[链接] 文",
                  "url": "https://example.com/in"}]},
        ]}

    def test_txt(self):
        buf = io.StringIO()
        F.write_txt(self.rich, self.group, buf)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], "[2026-03-01 09:05:07] 张三: 好的 [引用 李四: 明天<b>见</b>]")
        i = lines.index("[2026-03-01 09:05:10] 我: [聊天记录] 群聊的聊天记录 (2条)")
        self.assertEqual(lines[i + 1:i + 5], [
            "    [2023-6-20 19:45] A<script>: 第一行",
            "      第二行",
            "    [2023-6-20 19:46] B: [聊天记录] 内层",
            "        [2023-6-20 10:00] C: [链接] 文",
        ])
        self.assertEqual(lines[i + 5], "[2026-03-01 09:05:11] 张三: [拍一拍] 张三 拍了拍 我")

    def test_html(self):
        p = self.out("r.html")
        F.write(self.rich, self.group, p, "html")
        s = self.read(p)
        c = _Collector()
        c.feed(s)
        self.assertNotIn("script", c.tags)
        self.assertNotIn("b", c.tags)
        self.assertNotIn("i", c.tags)
        self.assertNotIn("x", c.tags)
        self.assertNotIn("javascript:", s)
        self.assertIn('<div class="quote">李四&lt;x&gt;: 明天&lt;b&gt;见&lt;/b&gt;</div>', s)
        self.assertIn('好的<div class="quote">', s)
        self.assertIn('[链接] <a href="https://example.com/a?x=1&amp;y=(2)" target="_blank" '
                      'rel="noopener noreferrer nofollow">标题&lt;i&gt;</a>', s)
        self.assertIn("描述&quot;&gt;&lt;script&gt;", s)
        self.assertIn(">[链接] 坏链接<", s)  # unsafe URL -> plain text
        self.assertIn("<details><summary>[聊天记录] 群聊的聊天记录 (2条)</summary>", s)
        self.assertIn('<span class="rs">A&lt;script&gt;</span>', s)
        self.assertIn('<a href="https://example.com/in"', s)
        self.assertEqual(s.count("<details>"), 2)  # nested bundle is collapsible too
        self.assertIn('<div class="sys" title="2026-03-01 09:05:11">[拍一拍]', s)
        self.assertIn('class="bubble k-transfer"', s)

    def test_md(self):
        p = self.out("r.md")
        F.write(self.rich, self.group, p, "md")
        s = self.read(p)
        self.assertNotIn("<", s.replace("&lt;", ""))
        self.assertIn("**张三** 09:05  好的\n\n> 李四&lt;x>: 明天&lt;b>见&lt;/b>\n", s)
        self.assertIn("[链接] [标题&lt;i>](https://example.com/a?x=1&y=%282%29) (来源)", s)
        self.assertIn("  [链接] 坏链接\n", s)
        self.assertIn("> **A&lt;script>** 2023-6-20 19:45: 第一行  \n> 第二行  \n", s)
        self.assertIn("> > **C** 2023-6-20 10:00: [\\[链接\\] 文](https://example.com/in)", s)

    def test_json_and_csv(self):
        p = self.out("r.json")
        F.write(self.rich, self.group, p, "json")
        d = json.loads(self.read(p))
        self.assertEqual(d["messages"][3]["extra"]["items"][1]["items"][0]["url"],
                         "https://example.com/in")
        self.assertEqual(d["messages"][0]["extra"]["quote"]["sender"], "李四<x>")
        p = self.out("r.csv")
        F.write(self.rich, self.group, p, "csv")
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1][4], "好的 [引用 李四: 明天<b>见</b>]")

    def test_bad_extra_ignored(self):
        recs = [rec(T0, "张三", "x", kind="quote"), rec(T0, "张三", "y", kind="link")]
        recs[0]["extra"] = "not a dict"
        recs[1]["extra"] = {"url": 5, "items": "nope"}
        for fmt in F.FORMATS:
            F.write(recs, self.group, self.out("bad." + fmt), fmt)


class TestDispatch(Base):
    def test_formats_and_ext(self):
        self.assertEqual(set(F.FORMATS), {"txt", "md", "html", "json", "csv"})
        for fmt in F.FORMATS:
            self.assertEqual(F.ext_for(fmt), "." + fmt)
            p = self.out("x" + F.ext_for(fmt))
            F.write([], self.single, p, fmt)  # empty input must not crash
            self.assertTrue(os.path.exists(p))
        self.assertEqual([f for f in F.FORMATS if F.supports_append(f)], ["txt", "md", "csv"])
        with self.assertRaises(ValueError):
            F.ext_for("pdf")
        with self.assertRaises(ValueError):
            F.write([], self.single, self.out("x"), "pdf")


if __name__ == "__main__":
    unittest.main()
