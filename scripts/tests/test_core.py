"""期限筛选与文件名/元数据解析的单元测试（不依赖网络）。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gzh.converter import html_to_markdown, sanitize_filename
from gzh.fetcher import (
    FetchError,
    diagnose_url,
    extract_content_html,
    extract_images,
    fetch_article,
    looks_like_error_page,
    parse_meta,
)
from gzh.period import PeriodError, filter_by_period, parse_period


class TestUrlDiagnosis(unittest.TestCase):
    """链接完整性自查（回归：缺 chksm 时微信会 302 到无提示的错误页）。"""

    FULL = (
        "https://mp.weixin.qq.com/s?__biz=MjI3Njc0NTk4MQ=="
        "&mid=2650266004&idx=1&sn=9b8053fb0b7d92eec292941f52ad092e"
        "&chksm=b788ec0680ff651057dbfc75a6c8d702cb9022b0decc4b0f742422b8209e5661e8c5a59437b2"
    )

    def test_complete_url_passes(self):
        self.assertIsNone(diagnose_url(self.FULL))

    def test_missing_chksm_detected(self):
        """实测：缺 chksm 会被 302 到 2KB 错误页，必须提前拦下。"""
        broken = self.FULL.split("&chksm=")[0]
        problem = diagnose_url(broken)
        self.assertIsNotNone(problem)
        self.assertIn("chksm", problem)

    def test_missing_sn_detected(self):
        broken = self.FULL.replace("&sn=9b8053fb0b7d92eec292941f52ad092e", "")
        self.assertIsNotNone(diagnose_url(broken))

    def test_short_link_allowed(self):
        """短链 /s/xxxx 不带参数，是合法的。"""
        self.assertIsNone(diagnose_url("https://mp.weixin.qq.com/s/AbCdEf123"))

    def test_empty_url(self):
        self.assertIsNotNone(diagnose_url(""))

    def test_fetch_article_rejects_incomplete_link(self):
        """抓取入口应直接抛出可读错误，而不是去请求一个注定失败的页面。"""
        broken = self.FULL.split("&chksm=")[0]
        with self.assertRaises(FetchError) as ctx:
            fetch_article(broken)
        self.assertIn("chksm", str(ctx.exception))


class TestPeriod(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(parse_period("1m").days, 30)
        self.assertEqual(parse_period("3m").days, 90)
        self.assertEqual(parse_period("6m").days, 182)
        self.assertEqual(parse_period("1y").days, 365)
        self.assertIsNone(parse_period("all").days)

    def test_chinese_aliases(self):
        self.assertEqual(parse_period("1个月").key, "1m")
        self.assertEqual(parse_period("3个月以内").key, "3m")
        self.assertEqual(parse_period("半年").key, "6m")
        self.assertEqual(parse_period("1年").key, "1y")
        self.assertEqual(parse_period("全部历史文章").key, "all")
        self.assertEqual(parse_period("全部").key, "all")

    def test_default_is_all(self):
        self.assertTrue(parse_period(None).is_all)
        self.assertTrue(parse_period("").is_all)

    def test_invalid_raises(self):
        with self.assertRaises(PeriodError):
            parse_period("3年")

    def test_contains(self):
        now = time.time()
        p = parse_period("1m")
        self.assertTrue(p.contains(int(now - 10 * 86400), now=now))   # 10 天前 → 在
        self.assertFalse(p.contains(int(now - 40 * 86400), now=now))  # 40 天前 → 不在

    def test_all_contains_everything(self):
        p = parse_period("all")
        self.assertTrue(p.contains(0))
        self.assertTrue(p.contains(int(time.time() - 3650 * 86400)))

    def test_missing_ts_kept_only_for_all(self):
        items = [{"create_time": 0}, {"create_time": int(time.time())}]
        self.assertEqual(len(filter_by_period(items, parse_period("all"))), 2)
        self.assertEqual(len(filter_by_period(items, parse_period("1m"))), 1)

    def test_filter_by_period(self):
        now = time.time()
        items = [
            {"create_time": int(now - 5 * 86400), "title": "A"},    # 5 天
            {"create_time": int(now - 45 * 86400), "title": "B"},   # 45 天
            {"create_time": int(now - 100 * 86400), "title": "C"},  # 100 天
            {"create_time": int(now - 200 * 86400), "title": "D"},  # 200 天
        ]
        # 边界：1m=30 / 3m=90 / 6m=182 / 1y=365 天
        expect = {
            "1m": ["A"],
            "3m": ["A", "B"],
            "6m": ["A", "B", "C"],
            "1y": ["A", "B", "C", "D"],
            "all": ["A", "B", "C", "D"],
        }
        for key, want in expect.items():
            got = [i["title"] for i in filter_by_period(items, parse_period(key), now=now)]
            self.assertEqual(got, want, f"期限 {key} 过滤结果不符")

    def test_boundary_exact_cutoff(self):
        """正好落在边界上的文章应被包含（判据是 >= cutoff）。"""
        now = time.time()
        p = parse_period("1m")
        exactly = int(now - p.days * 86400)
        self.assertTrue(p.contains(exactly, now=now))
        self.assertFalse(p.contains(exactly - 1, now=now))


class TestFilename(unittest.TestCase):
    def test_strips_illegal_chars(self):
        self.assertEqual(sanitize_filename('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i")

    def test_empty_fallback(self):
        self.assertEqual(sanitize_filename("", fallback="x"), "x")
        self.assertEqual(sanitize_filename("   ", fallback="x"), "x")

    def test_truncates(self):
        self.assertLessEqual(len(sanitize_filename("字" * 200)), 80)


class TestMetaParsing(unittest.TestCase):
    HTML = """
    <html><head>
    <meta property="og:title" content="OG标题" />
    <meta property="og:description" content="摘要内容" />
    <meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg" />
    </head><body>
    <h1 class="rich_media_title" id="activity-name">
      <span class="js_title_inner">真实标题</span></h1>
    <span id="js_name">测试公众号</span>
    <script>
      var ct = "1673879904";
      var user_name = "wxid_test";
      var author = "张三";
      var copyright_stat = "1";
    </script>
    <div id="js_content"><p>正文第一段</p><img data-src="https://mmbiz.qpic.cn/a.jpg" /></div>
    </body></html>
    """

    def test_parses_fields(self):
        m = parse_meta(self.HTML, url="https://mp.weixin.qq.com/s?__biz=MzA5&mid=123&idx=1&sn=abc123")
        self.assertEqual(m.title, "真实标题")
        self.assertEqual(m.account, "测试公众号")
        self.assertEqual(m.author, "张三")
        self.assertEqual(m.account_id, "wxid_test")
        self.assertEqual(m.publish_ts, 1673879904)
        self.assertEqual(m.publish_date, "2023-01-16")
        self.assertTrue(m.is_original)
        self.assertEqual(m.mid, "123")
        self.assertEqual(m.sn, "abc123")

    def test_error_page_detection(self):
        self.assertIsNotNone(looks_like_error_page("<html><body>未知错误，请稍后再试</body></html>"))
        self.assertIsNone(looks_like_error_page(self.HTML))

    def test_content_and_images(self):
        content = extract_content_html(self.HTML)
        self.assertIn("正文第一段", content)
        self.assertEqual(extract_images(content), ["https://mmbiz.qpic.cn/a.jpg"])


class TestMarkdown(unittest.TestCase):
    def test_basic_conversion(self):
        html = "<p>段落一</p><h2>小标题</h2><p><strong>加粗</strong>普通</p>"
        md = html_to_markdown(html)
        self.assertIn("段落一", md)
        self.assertIn("## 小标题", md)
        self.assertIn("**加粗**", md)

    def test_images_survive_attribute_stripping(self):
        """回归：data-src 必须在剥属性前搬回 src，否则图片全丢。"""
        html = '<section><img class="x" data-src="https://mmbiz.qpic.cn/a.jpg" style="width:1px"/></section>'
        md = html_to_markdown(html)
        self.assertIn("https://mmbiz.qpic.cn/a.jpg", md)
        self.assertIn("![", md)

    def test_image_map_replacement(self):
        html = '<img data-src="https://mmbiz.qpic.cn/a.jpg" />'
        md = html_to_markdown(html, image_map={"https://mmbiz.qpic.cn/a.jpg": "images/a.jpg"})
        self.assertIn("](images/a.jpg)", md)
        self.assertNotIn("mmbiz.qpic.cn", md)
    def test_nested_sections(self):
        html = "<section><section><p>深层段落</p></section></section>"
        self.assertIn("深层段落", html_to_markdown(html))

    def test_drops_script_and_style(self):
        html = "<style>p{color:red}</style><script>var a=1</script><p>正文</p>"
        md = html_to_markdown(html)
        self.assertIn("正文", md)
        self.assertNotIn("color:red", md)
        self.assertNotIn("var a", md)

    def test_empty_input(self):
        self.assertEqual(html_to_markdown(""), "")
        self.assertEqual(html_to_markdown("   "), "")

    def test_link_preserved(self):
        md = html_to_markdown('<p><a href="https://x.com">链接</a></p>')
        self.assertIn("[链接](https://x.com)", md)


class TestExportLayout(unittest.TestCase):
    """导出目录结构与图片相对路径（回归：图片链接必须相对 Markdown 所在目录）。"""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.kb_root = self.tmp / "kb"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_image(self, url: str) -> Path:
        """造一张假图片，避免测试依赖网络。"""
        from gzh.exporter import _image_name
        name = _image_name(url, "image/jpeg")
        return name

    def test_image_link_is_relative_to_article_dir(self):
        """文章在 articles/ 下，图片在 images/ 下，链接必须是 ../images/x.jpg。

        这是真实踩过的坑：写成 images/x.jpg 会让 Obsidian 里所有图都裂。
        """
        from gzh.exporter import KnowledgeBase
        from gzh.fetcher import ArticleMeta

        kb = KnowledgeBase(self.kb_root, account="测试号")
        kb.ensure_dirs()

        img_url = "https://mmbiz.qpic.cn/mmbiz_jpg/abc/640?wx_fmt=jpeg"
        name = self._fake_image(img_url)
        (kb.images_dir / name).write_bytes(b"\xff\xd8\xff\xe0fake")

        meta = ArticleMeta(title="标题", account="测试号", url="https://mp.weixin.qq.com/s/x",
                           publish_ts=1673879904)
        content = f'<p>正文</p><img data-src="{img_url}" />'

        # 直接注入 image_map，绕过网络下载
        from gzh.converter import html_to_markdown
        from gzh.exporter import render_markdown
        md = html_to_markdown(content, image_map={img_url: f"../images/{name}"})
        doc = render_markdown(meta, md)

        art_path = kb.articles_dir / "2023-01-16-标题.md"
        art_path.write_text(doc, encoding="utf-8")

        import re as _re
        refs = _re.findall(r"!\[[^\]]*\]\(([^)]+)\)", doc)
        self.assertEqual(len(refs), 1, "应恰好有一张图")
        resolved = (art_path.parent / refs[0]).resolve()
        self.assertTrue(resolved.exists(), f"图片链接解析不到文件：{refs[0]} → {resolved}")
        self.assertTrue(refs[0].startswith("../images/"), "链接应是 ../images/ 开头")

    def test_index_links_relative_to_index(self):
        """index.md 与 articles/ 同级，链接应是 articles/x.md。"""
        from gzh.exporter import ExportedArticle, KnowledgeBase

        kb = KnowledgeBase(self.kb_root, account="测试号")
        arts = [ExportedArticle(
            title="标题", account="测试号", author="", publish_date="2023-01-16",
            publish_ts=1673879904, url="https://mp.weixin.qq.com/s/x",
            path="测试号/articles/2023-01-16-标题.md",
        )]
        index_path = kb.write_index_md(arts, "全部历史")
        text = index_path.read_text(encoding="utf-8")
        self.assertIn("](articles/2023-01-16-标题.md)", text)
        self.assertNotIn("](测试号/articles/", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
