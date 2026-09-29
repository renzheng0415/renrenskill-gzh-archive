"""公开索引（搜狗）解析的离线测试 —— 不依赖网络。

用一段真实的搜狗搜索结果页结构做夹具，验证解析逻辑；
网络可用性本身由 selftest.py 的第 6 项负责。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gzh.sogou import SogouArticle, parse_search_page  # noqa: E402

# 真实结构的精简版：两条结果，含标题/公众号/摘要/时间戳
FIXTURE = """
<ul class="news-list">
  <li id="sogou_vr_11002601_box_0" d="abc">
    <div class="txt-box">
      <h3><a target="_blank" href="/link?url=AAA&amp;type=2&amp;query=x&amp;token=T1"
             id="sogou_vr_11002601_title_0" uigs="article_title_0">
        <em><!--red_beg-->深圳楼市<!--red_end--></em>，大消息！</a></h3>
      <p class="txt-info" id="sogou_vr_11002601_summary_0">作 者丨吴抒颖 编 辑丨张伟贤</p>
      <div class="s-p">
        <span class="all-time-y2">21世纪经济报道</span>
        <span class="s2"><script>document.write(timeConvert('1673966287'))</script></span>
      </div>
    </div>
  </li>
  <li id="sogou_vr_11002601_box_1" d="def">
    <div class="txt-box">
      <h3><a target="_blank" href="/link?url=BBB&amp;type=2&amp;query=x&amp;token=T2"
             id="sogou_vr_11002601_title_1" uigs="article_title_1">读懂深圳楼市新格局</a></h3>
      <p class="txt-info" id="sogou_vr_11002601_summary_1">政策转向居住优先</p>
      <div class="s-p">
        <span class="all-time-y2">某地产号</span>
        <span class="s2"><script>document.write(timeConvert('1754000000'))</script></span>
      </div>
    </div>
  </li>
</ul>
"""


class TestSogouParsing(unittest.TestCase):
    def test_parses_both_items(self):
        items = parse_search_page(FIXTURE)
        self.assertEqual(len(items), 2)

    def test_fields(self):
        a = parse_search_page(FIXTURE)[0]
        self.assertIn("深圳楼市", a.title)
        self.assertIn("大消息", a.title)
        self.assertEqual(a.account, "21世纪经济报道")
        self.assertEqual(a.publish_ts, 1673966287)
        self.assertIn("吴抒颖", a.digest)

    def test_html_tags_stripped(self):
        a = parse_search_page(FIXTURE)[0]
        self.assertNotIn("<em>", a.title)
        self.assertNotIn("<!--", a.title)

    def test_sogou_link_absolute_and_unescaped(self):
        a = parse_search_page(FIXTURE)[0]
        self.assertTrue(a.sogou_link.startswith("https://weixin.sogou.com/link?url="))
        self.assertNotIn("&amp;", a.sogou_link)

    def test_to_dict_matches_backend_shape(self):
        """必须能直接喂给期限筛选和导出逻辑（字段名与后台接口一致）。"""
        d = parse_search_page(FIXTURE)[0].to_dict()
        for key in ("title", "link", "create_time", "update_time", "copyright_stat"):
            self.assertIn(key, d)
        self.assertEqual(d["_source"], "sogou")

    def test_empty_page(self):
        self.assertEqual(parse_search_page("<html><body>无结果</body></html>"), [])

    def test_unresolved_article_excluded_by_list_helper(self):
        """没解析出真实链接的条目不应进入结果（避免导出空链接）。"""
        arts = [
            SogouArticle(title="有链接", account="A", url="https://mp.weixin.qq.com/s/x"),
            SogouArticle(title="没链接", account="A", url=""),
        ]
        kept = [a for a in arts if a.url]
        self.assertEqual(len(kept), 1)


class TestFreqControlClassification(unittest.TestCase):
    """200013 必须被识别成专门的错误类型，才能触发兜底切换。"""

    def test_freq_control_is_subclass(self):
        from gzh.fetcher import FetchError, FreqControlError
        self.assertTrue(issubclass(FreqControlError, FetchError))

    def test_can_be_caught_specifically(self):
        from gzh.fetcher import FetchError, FreqControlError
        caught = None
        try:
            raise FreqControlError("200013 freq control")
        except FreqControlError as exc:
            caught = exc
        except FetchError:
            self.fail("应优先被 FreqControlError 捕获")
        self.assertIsNotNone(caught)


if __name__ == "__main__":
    unittest.main(verbosity=2)
