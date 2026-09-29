"""公开索引兜底：通过搜狗微信搜索获取公众号文章列表。

## 为什么需要这个模块

2026-07-30 起，微信对「公众号后台跨公众号文章列表」能力收紧：
`/cgi-bin/appmsgpublish` 第一页零结果就直接返回 `ret=200013 freq control`。
实测确认这与翻页累积限频无关 —— `begin=0` 即失败，等待也不会恢复。
（同期搜索接口 `searchbiz` 与单篇正文抓取仍然正常。）

所以历史列表必须走公开索引。搜狗微信（weixin.sogou.com）实测可用，
能返回标题、公众号名、发布时间与可解析的真实链接。

## 已知限制

- 覆盖范围是「近期公开文章」，不是完整历史。深度翻页会被反爬拦。
- 结果按相关性排序，不保证时间倒序，需要自己排序。
- 连续高频请求会触发验证码页，所以要控制频率并检测。
- 同名公众号可能混入，需要按公众号名过滤。
"""

from __future__ import annotations

import html as html_mod
import http.cookiejar
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .fetcher import FetchError

SOGOU_BASE = "https://weixin.sogou.com"
_SEARCH_URL = SOGOU_BASE + "/weixin"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 搜狗返回验证码页时的特征
_BLOCK_MARKERS = ("antispider", "请输入验证码", "您的访问过于频繁")


@dataclass
class SogouArticle:
    """从公开索引拿到的一条文章线索。"""

    title: str
    account: str
    url: str = ""            # 解析后的真实 mp.weixin.qq.com 链接
    sogou_link: str = ""     # 搜狗跳转链接
    publish_ts: int = 0
    digest: str = ""
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转成与后台接口一致的字段形状，便于复用上层的期限筛选与导出。"""
        return {
            "title": self.title,
            "link": self.url,
            "author_name": "",
            "digest": self.digest,
            "create_time": self.publish_ts,
            "update_time": self.publish_ts,
            "copyright_stat": 0,
            "is_deleted": False,
            "_source": "sogou",
            "_account": self.account,
        }


class SogouSession:
    """搜狗请求会话。

    ## 为什么不带 cookie jar（实测结论）

    带 cookie jar 时搜狗会下发 `IPLOC=US`、把请求判定为境外流量，
    之后每次都返回反爬页（5369 字节、零结果）。
    不带 cookie jar 时连续 4 次请求全部正常（31688 字节、每次 10 条结果）。

    所以这里刻意**不使用 cookie jar** —— 搜索页与跳转页都不需要它，
    跳转页的 URL 拼接逻辑直接写在 HTML 里。
    """

    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self._opener = urllib.request.build_opener()
        self._last_request = 0.0
        self.min_interval = 2.0   # 搜狗反爬较敏感，默认间隔 2 秒
        self.retries = 3          # 被拦后自动退避重试次数

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.time()

    def get(self, url: str, referer: str = "") -> str:
        """GET 一个页面；被反爬拦下时自动退避重试。

        搜狗的拦截通常是瞬时的（短时间请求多了就拦，等一下就好），
        所以退避重试比直接报错更实用。
        """
        last_error = ""
        for attempt in range(self.retries):
            self._throttle()
            headers = dict(_HEADERS)
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(url, headers=headers)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(2.0 * (attempt + 1))
                continue

            text = raw.decode("utf-8", errors="replace")
            if not any(m in text for m in _BLOCK_MARKERS):
                return text

            # 命中反爬：逐步加大间隔再试
            last_error = "反爬验证"
            if attempt < self.retries - 1:
                time.sleep(5.0 * (attempt + 1))

        raise FetchError(
            f"公开索引连续 {self.retries} 次被反爬拦截（{last_error}）。"
            "请稍后再试，或加大 --sleep 降低请求频率。"
        )


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(text).strip()


def parse_search_page(html: str) -> list[SogouArticle]:
    """从搜狗搜索结果页解析文章线索。"""
    items: list[SogouArticle] = []

    # 每条结果是一个 <li id="sogou_vr_..._box_N"> 块
    for block in re.split(r'<li[^>]+id="sogou_vr_[^"]*_box_\d+"', html)[1:]:
        m = re.search(r'<h3>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        link, title = m.group(1), _clean(m.group(2))
        if not title:
            continue

        acc = ""
        m_acc = re.search(r'class="all-time-y2"[^>]*>(.*?)</span>', block, re.S)
        if m_acc:
            acc = _clean(m_acc.group(1))

        digest = ""
        m_dig = re.search(r'class="txt-info"[^>]*>(.*?)</p>', block, re.S)
        if m_dig:
            digest = _clean(m_dig.group(1))

        ts = 0
        m_ts = re.search(r"timeConvert\('(\d+)'\)", block)
        if m_ts:
            ts = int(m_ts.group(1))

        items.append(SogouArticle(
            title=title,
            account=acc,
            sogou_link=SOGOU_BASE + html_mod.unescape(link) if link.startswith("/") else link,
            publish_ts=ts,
            digest=digest,
        ))

    return items


def resolve_link(session: SogouSession, sogou_link: str) -> str:
    """把搜狗跳转链接解析成真实的 mp.weixin.qq.com 链接。

    搜狗不返回 302，而是在页面里用 JS 拼字符串，需要自己拼回来。
    """
    page = session.get(sogou_link, referer=SOGOU_BASE + "/")
    parts = re.findall(r"url \+= '([^']*)'", page)
    if not parts:
        return ""
    return "".join(parts).replace("@", "")


def search_articles(keyword: str, account: str = "", pages: int = 1,
                    resolve: bool = True, session: SogouSession | None = None,
                    sleep: float = 2.0) -> list[SogouArticle]:
    """按关键词搜索公开文章。

    keyword: 搜索词（公众号名或话题）
    account: 只保留该公众号的文章（精确匹配公众号名）；留空则不过滤
    pages:   翻几页（每页约 10 条）。翻页越多越容易被反爬。
    resolve: 是否解析出真实链接（需要额外一次请求/条）
    """
    sess = session or SogouSession()
    if sleep and sleep != sess.min_interval:
        sess.min_interval = sleep

    collected: list[SogouArticle] = []
    seen_titles: set[str] = set()

    for page in range(1, max(1, pages) + 1):
        params = {"type": 2, "query": keyword, "page": page, "ie": "utf8"}
        url = _SEARCH_URL + "?" + urllib.parse.urlencode(params)
        html = sess.get(url, referer=SOGOU_BASE + "/")

        batch = parse_search_page(html)
        if not batch:
            break

        for art in batch:
            key = art.title
            if key in seen_titles:
                continue
            seen_titles.add(key)
            if account and account not in art.account:
                continue
            collected.append(art)

    if resolve:
        for art in collected:
            if not art.sogou_link:
                continue
            try:
                real = resolve_link(sess, art.sogou_link)
            except FetchError:
                continue
            if real:
                art.url = real
                art.resolved = True

    return collected


def list_account_articles(account: str, pages: int = 3,
                          resolve: bool = True, sleep: float = 2.0) -> list[dict[str, Any]]:
    """按公众号名抓取公开文章列表，返回与后台接口一致的字段形状。

    这是 200013 之后的替代路径。注意覆盖范围是「近期公开文章」，
    不是完整历史。
    """
    arts = search_articles(account, account=account, pages=pages,
                           resolve=resolve, sleep=sleep)
    # 时间倒序（拿不到时间的排最后）
    arts.sort(key=lambda a: a.publish_ts or 0, reverse=True)
    return [a.to_dict() for a in arts if a.url]
