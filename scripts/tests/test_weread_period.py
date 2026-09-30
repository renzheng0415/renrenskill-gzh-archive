"""期限筛选的离线测试 —— 不依赖网络。

## 这个测试防的是什么

早期实现是「先抓固定页数，再在结果里按期限过滤」。
后果：说好抓「1 年」，如果只抓了 3 页（约半年数据），
**它会安静地只给半年，不报错**，用户根本不知道少了。

正确行为是「按时间边界决定翻几页」—— 翻到覆盖住期限起点为止。

所以这里的核心断言是：
  ① 期限短时，**不会多翻**（覆盖到就停）
  ② 期限是 all 时，**一直翻到底**
  ③ 页数不够覆盖期限时，**如实报告 covered=False**，而不是假装成功

用假取页函数注入，不碰网络，也不消耗微信读书额度。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gzh.period import parse_period  # noqa: E402
from gzh import weread  # noqa: E402

DAY = 86400
NOW = time.time()

# 每页 20 群发、每页往前 30 天；第 10 页之后没有内容
PAGES_AVAILABLE = 10


def fake_fetch_page(book_id, offset, **kw):
    """模拟取页：从今天开始，每页往前 30 天，每页 20 篇。"""
    page_i = offset // 20
    if page_i >= PAGES_AVAILABLE:
        return {"ok": True, "reviews": 0, "items": [], "oldest": 0}
    base = NOW - page_i * 30 * DAY
    items = [
        {
            "t": int(base - j * DAY),
            "title": f"第{page_i}页-{j}",
            "url": f"https://mp.weixin.qq.com/s/fake{page_i}_{j}",
            "rid": "",
        }
        for j in range(20)
    ]
    return {
        "ok": True,
        "reviews": 20,
        "items": items,
        "oldest": int(min(i["t"] for i in items)),
    }


def fetch_with(period_key: str, max_pages: int = 20):
    with patch("gzh.crawl.fetch_page", fake_fetch_page):
        return weread.fetch_until("MP_WXS_TEST", "测试号",
                                  parse_period(period_key),
                                  max_pages=max_pages, verbose=False)


class TestPeriodCoverage(unittest.TestCase):
    """翻页必须由期限驱动，而不是固定页数。"""

    def test_short_period_stops_early(self):
        """3 个月：覆盖到就停，不该把 10 页全翻完。"""
        arts, meta = fetch_with("3m")
        cutoff = parse_period("3m").cutoff_ts()

        self.assertTrue(meta["covered"], "3 个月应当能覆盖（账上共 10 页≈10 个月）")
        self.assertLessEqual(meta["pages"], 6,
                             f"不该多翻：3 个月最多约 4 页，实际翻了 {meta['pages']} 页")
        self.assertLessEqual(meta["oldest_ts"], cutoff,
                             "抓到的最早时间必须早于期限起点，才算真覆盖")
        self.assertGreater(len(arts), 0)

    def test_all_period_goes_to_the_end(self):
        """all：一路翻到没有内容为止。"""
        arts, meta = fetch_with("all")

        self.assertTrue(meta["covered"])
        self.assertEqual(meta["pages"], PAGES_AVAILABLE,
                         "all 应当翻完所有有内容的页")
        # 空页不该被计入翻页数
        self.assertEqual(len(arts), PAGES_AVAILABLE * 20)

    def test_page_limit_reports_incomplete(self):
        """页数不够覆盖期限时，必须如实说「没覆盖到」，不能假装成功。"""
        _arts, meta = fetch_with("1y", max_pages=3)

        self.assertFalse(meta["covered"],
                         "只翻 3 页（约 3 个月）不可能覆盖 1 年，必须报 False")
        self.assertEqual(meta["pages"], 3)
        self.assertIn("页数上限", meta["reason"])

    def test_no_duplicate_urls(self):
        """跨页去重：同一 URL 不该重复出现。"""
        arts, _meta = fetch_with("all")
        urls = [a.url for a in arts]
        self.assertEqual(len(urls), len(set(urls)), "有重复 URL")

    def test_empty_account(self):
        """一个号都没有内容时不崩。"""
        with patch("gzh.crawl.fetch_page",
                   lambda book_id, offset, **kw: {"ok": True, "reviews": 0,
                                                  "items": [], "oldest": 0}):
            arts, meta = weread.fetch_until("X", "空号", parse_period("all"),
                                            max_pages=5, verbose=False)
        self.assertEqual(arts, [])
        self.assertEqual(meta["pages"], 0)


class TestPeriodParsing(unittest.TestCase):
    """期限参数的边界。"""

    def test_all_has_no_cutoff(self):
        self.assertIsNone(parse_period("all").cutoff_ts())

    def test_known_periods(self):
        for key, days in (("1m", 30), ("3m", 90), ("6m", 182), ("1y", 365)):
            with self.subTest(key=key):
                self.assertEqual(parse_period(key).days, days)

    def test_cutoff_is_in_the_past(self):
        self.assertLess(parse_period("1m").cutoff_ts(), time.time())


if __name__ == "__main__":
    unittest.main(verbosity=2)
