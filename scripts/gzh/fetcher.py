"""微信公众号文章抓取核心。

三条抓取通道：
1. direct  —— 直接请求 mp.weixin.qq.com 的规范链接（__biz/mid/idx/sn）。
   实测可用、无频率限制、并发安全，是单篇抓取的首选。
2. api     —— 走 down.mptext.top 公共导出服务的 /api/public/v1/download。
   游客 1 次/分钟，仅 html 格式免费；作为 direct 失败时的兜底。
3. browser —— 用专用 Chrome 渲染页面再取正文（见 fetch_via_browser）。
   微信正在把文章页改成 JS 动态渲染，静态 HTML 只有壳，前两条通道都会
   得到「正文为空」。这时必须靠真浏览器渲染。最慢，所以放最后。

历史文章列表（history）走微信读书（见 gzh/weread.py 与 gzh/crawl.py），
公众号后台那条路已被平台封禁（ret=200013）。
"""

from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

# 微信公众号文章的桌面 UA。移动端 UA 会拿到被重定向的「未知错误」页。
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DIRECT_HEADERS = {
    "User-Agent": DESKTOP_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

PUBLIC_API_BASE = "https://down.mptext.top"

# 微信侧返回的典型错误页标记，用于判定抓取是否真的拿到正文。
_ERROR_MARKERS = (
    "未知错误",
    "该内容已被发布者删除",
    "此内容因违规无法查看",
    "该公众号已迁移",
    "参数错误",
    "环境异常",
    "请在微信客户端打开链接",
)

_BIZ_RE = re.compile(r"__biz=([A-Za-z0-9=+/]+)")
_MID_RE = re.compile(r"mid=(\d+)")
_IDX_RE = re.compile(r"idx=(\d+)")
_SN_RE = re.compile(r"sn=([0-9a-fA-F]+)")


class FetchError(RuntimeError):
    """抓取失败，附带可读原因。"""


class FreqControlError(FetchError):
    """微信后台跨公众号列表接口被平台限制（200013 freq control）。

    单独成类，便于上层自动切换到公开索引兜底，而不是当成普通网络错误重试。
    """


@dataclass
class ArticleMeta:
    """一篇文章的元数据。"""

    title: str = ""
    account: str = ""          # 公众号名称
    account_id: str = ""       # 公众号原始 id (user_name / wxid)
    author: str = ""           # 文章署名作者
    publish_ts: int = 0        # 发布时间戳（秒）
    url: str = ""
    biz: str = ""
    mid: str = ""
    idx: str = ""
    sn: str = ""
    digest: str = ""
    cover: str = ""
    album_id: str = ""
    copyright_stat: int = 0
    is_deleted: bool = False

    @property
    def publish_date(self) -> str:
        """YYYY-MM-DD；时间戳缺失时返回空串。"""
        if not self.publish_ts:
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(self.publish_ts))

    @property
    def publish_datetime(self) -> str:
        if not self.publish_ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.publish_ts))

    @property
    def is_original(self) -> bool:
        """原创标记：copyright_stat == 1。"""
        return self.copyright_stat == 1


@dataclass
class FetchResult:
    """一次抓取的结果。"""

    meta: ArticleMeta
    html: str = ""
    content_html: str = ""
    images: list[str] = field(default_factory=list)
    channel: str = ""   # direct | api


def canonicalize_url(url: str) -> str:
    """把各种形态的公众号链接规范成可稳定请求的形式。

    - 去掉 #wechat_redirect 之类片段
    - http -> https
    - 保留 __biz/mid/idx/sn 等查询参数
    """
    url = (url or "").strip()
    if not url:
        raise FetchError("链接为空")
    url = url.split("#", 1)[0]
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not url.startswith("https://mp.weixin.qq.com/"):
        raise FetchError(f"不是微信公众号文章链接：{url}")
    return url


def parse_ids(url: str) -> dict[str, str]:
    """从链接里取出 __biz / mid / idx / sn。"""
    out: dict[str, str] = {}
    if m := _BIZ_RE.search(url):
        out["biz"] = m.group(1)
    if m := _MID_RE.search(url):
        out["mid"] = m.group(1)
    if m := _IDX_RE.search(url):
        out["idx"] = m.group(1)
    if m := _SN_RE.search(url):
        out["sn"] = m.group(1)
    return out


def is_mp_article_url(url: str) -> bool:
    return bool(re.match(r"https?://mp\.weixin\.qq\.com/s", (url or "").strip()))


# --------------------------------------------------------------------------
# 元数据解析
# --------------------------------------------------------------------------

def _var(html: str, name: str) -> str:
    """读取页面里的 var name = "..." 形式变量。"""
    m = re.search(rf"var\s+{name}\s*=\s*[\"']([^\"']*)[\"']", html)
    if m:
        return html_mod.unescape(m.group(1)).strip()
    m = re.search(rf"var\s+{name}\s*=\s*htmlDecode\(\"([^\"]*)\"\)", html)
    if m:
        return html_mod.unescape(m.group(1)).strip()
    return ""


def _meta_content(html: str, prop: str) -> str:
    m = re.search(
        rf"<meta[^>]*(?:property|name)=[\"']{re.escape(prop)}[\"'][^>]*content=[\"']([^\"']*)[\"']",
        html,
        re.I,
    )
    if not m:
        m = re.search(
            rf"<meta[^>]*content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']{re.escape(prop)}[\"']",
            html,
            re.I,
        )
    return html_mod.unescape(m.group(1)).strip() if m else ""


def parse_meta(html: str, url: str = "") -> ArticleMeta:
    """从文章页面 HTML 中解析元数据。"""
    ids = parse_ids(url or html)
    meta = ArticleMeta(url=url, **{k: v for k, v in ids.items()})

    # 标题：优先 activity-name（正文标题），退化到 og:title
    title = ""
    m = re.search(r'id=["\']activity-name["\'][^>]*>(.*?)</h1>', html, re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if not title:
        title = _var(html, "msg_title") or _meta_content(html, "og:title")
    meta.title = html_mod.unescape(title).strip()

    # 公众号名称
    m = re.search(r'id=["\']js_name["\'][^>]*>(.*?)</', html, re.S)
    account = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    if not account:
        account = _var(html, "nickname")
    meta.account = html_mod.unescape(account).strip()

    meta.account_id = _var(html, "user_name") or _var(html, "appuin")
    meta.author = _var(html, "author") or _meta_content(html, "author")

    # 发布时间：create_time / ct，退化到页面上的 publish_time 文本
    for key in ("create_time", "ct"):
        raw = _var(html, key)
        if raw.isdigit() and int(raw) > 0:
            meta.publish_ts = int(raw)
            break
    if not meta.publish_ts:
        m = re.search(r'id=["\']publish_time["\'][^>]*>([^<]*)<', html)
        if m:
            txt = m.group(1).strip()
            if re.match(r"\d{4}-\d{2}-\d{2}", txt):
                try:
                    meta.publish_ts = int(time.mktime(time.strptime(txt[:19], "%Y-%m-%d %H:%M:%S")))
                except ValueError:
                    try:
                        meta.publish_ts = int(time.mktime(time.strptime(txt[:10], "%Y-%m-%d")))
                    except ValueError:
                        pass

    meta.digest = _meta_content(html, "og:description") or _meta_content(html, "description")
    meta.cover = _meta_content(html, "og:image")

    # 原创标记
    cs = _var(html, "copyright_stat")
    if cs.isdigit():
        meta.copyright_stat = int(cs)

    return meta


def looks_like_error_page(html: str) -> str | None:
    """判断页面是否是微信的错误/拦截页；是则返回原因。"""
    if not html or len(html) < 500:
        return "页面内容过短，可能是错误页"
    if "js_content" in html or "rich_media_content" in html:
        return None
    for marker in _ERROR_MARKERS:
        if marker in html:
            return marker
    return "未找到正文容器 js_content"


# 公众号文章链接有两种合法形态，各自需要不同的参数组合：
#
# 1. 规范形态（__biz/mid/idx/sn/chksm）—— 浏览器地址栏、「复制链接」拿到的。
#    缺 chksm 时微信会 302 到一个 2KB 错误页，页面既没有正文也没有报错文案，
#    极易被误判成「反爬」，所以必须提前拦下。
# 2. 签名形态（src/timestamp/signature）—— 搜狗微信等公开索引跳转产生的临时链接。
#    这类链接参数完全不同，不能按形态 1 去要求 __biz/mid/idx/sn/chksm。
_CANONICAL_PARAMS = ("__biz", "mid", "idx", "sn", "chksm")
_SIGNED_PARAMS = ("src", "timestamp", "signature")


def diagnose_url(url: str) -> str | None:
    """检查链接是否完整；不完整时返回可操作的原因，完整返回 None。

    识别两种合法形态（规范链接 / 签名链接），只拦真正残缺的链接。
    """
    if not url:
        return "链接为空"

    # 短链形态（/s/xxxxxxxx）本身不带查询参数，是合法的
    if "/s/" in url and "?" not in url:
        return None

    # 签名形态：只要 src/timestamp/signature 齐了就放行
    if all(p in url for p in _SIGNED_PARAMS):
        return None

    missing = [p for p in _CANONICAL_PARAMS if p not in url]
    if not missing:
        return None

    # 有部分规范参数但又不全 —— 说明是被人手动删过参数的规范链接
    if any(p in url for p in _CANONICAL_PARAMS):
        return (
            f"链接不完整，缺少参数：{', '.join(missing)}。"
            "公众号文章链接必须带完整查询参数（__biz/mid/idx/sn/chksm）——"
            "请从浏览器地址栏或微信「复制链接」取完整链接，不要手动删参数。"
        )

    return (
        "无法识别的公众号文章链接。"
        "支持两种形态：① 规范链接（含 __biz/mid/idx/sn/chksm）"
        "② 签名链接（含 src/timestamp/signature）。"
        "请从浏览器地址栏、微信「复制链接」或公开索引跳转处取链接。"
    )


# --------------------------------------------------------------------------
# 正文抽取
# --------------------------------------------------------------------------

def extract_content_html(html: str) -> str:
    """抽出 js_content 正文容器的 HTML（保留内部标签与图片）。"""
    m = re.search(r'<div[^>]*id=["\']js_content["\'][^>]*>', html)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    token = re.compile(r"<(/?)div\b[^>]*>", re.I)
    while True:
        t = token.search(html, i)
        if not t:
            break
        depth += -1 if t.group(1) else 1
        i = t.end()
        if depth == 0:
            return html[start:t.start()]
    return html[start:]


def extract_images(content_html: str) -> list[str]:
    """取出正文里所有图片的真实地址（data-src 优先，微信懒加载）。"""
    urls: list[str] = []
    for tag in re.findall(r"<img\b[^>]*>", content_html, re.I):
        m = re.search(r'\bdata-src=["\']([^"\']+)["\']', tag)
        if not m:
            m = re.search(r'\bsrc=["\']([^"\']+)["\']', tag)
        if not m:
            continue
        u = html_mod.unescape(m.group(1)).strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and u not in urls:
            urls.append(u)
    return urls


# --------------------------------------------------------------------------
# 抓取通道
# --------------------------------------------------------------------------

def fetch_direct(url: str, timeout: int = 30, session: requests.Session | None = None) -> str:
    """直连抓取文章 HTML。"""
    target = canonicalize_url(url)
    getter = (session or requests).get
    resp = getter(target, headers=DIRECT_HEADERS, timeout=timeout)
    if resp.status_code != 200:
        raise FetchError(f"直连返回 HTTP {resp.status_code}")
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def fetch_via_api(url: str, timeout: int = 60, api_base: str = PUBLIC_API_BASE,
                  auth_key: str | None = None) -> str:
    """走公共导出服务抓取 HTML。auth_key 可换取 markdown/json 等会员格式。"""
    target = canonicalize_url(url)
    headers = {"User-Agent": DESKTOP_UA, "Referer": api_base + "/"}
    if auth_key:
        headers["X-Auth-Key"] = auth_key
    resp = requests.get(
        f"{api_base.rstrip('/')}/api/public/v1/download",
        params={"url": target, "format": "html"},
        headers=headers,
        timeout=timeout,
    )
    if resp.status_code == 429:
        raise FetchError("公共导出服务限流（游客 1 次/分钟）")
    if resp.status_code != 200:
        raise FetchError(f"导出服务返回 HTTP {resp.status_code}")
    return resp.text


def fetch_via_browser(url: str, timeout: int = 90) -> FetchResult:
    """用专用 Chrome 渲染文章页再取正文（第三通道）。

    ## 为什么需要它

    微信正在把文章页改成 **JS 动态渲染**：静态 HTML 里只有壳
    （「微信扫一扫 使用小程序」之类），正文靠 JS 加载后才出现 ——
    抓静态 HTML 会得到「正文为空」。

    实测判据：静态 HTML 里找不到真正的 `id="js_content"` 元素，
    该字符串只出现在 JS 源码里。

    这类页面只能靠真浏览器渲染。复用微信读书那边的调试端口，
    所以不需要额外开浏览器。
    """
    script = Path(__file__).resolve().parent.parent / "tools" / "render_article.mjs"
    if not script.exists():
        raise FetchError(f"找不到渲染脚本：{script}")

    try:
        proc = subprocess.run(["node", str(script), url],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"浏览器渲染超时（{timeout}s）") from exc
    except FileNotFoundError as exc:
        raise FetchError("找不到 node，无法用浏览器渲染") from exc

    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        raise FetchError(f"浏览器渲染无输出：{(proc.stderr or '')[:150]}")

    try:
        d = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise FetchError(f"浏览器渲染输出无法解析：{lines[-1][:150]}") from exc

    if not d.get("ok"):
        raise FetchError(f"浏览器渲染失败：{d.get('error', '')} {d.get('hint', '')}".strip())

    content_html = d.get("contentHtml") or ""
    if len(content_html) < 80:
        raise FetchError("浏览器渲染后正文仍为空")

    publish_ts = 0
    if d.get("publishTime"):
        publish_ts = _parse_cn_time(d["publishTime"])

    meta = ArticleMeta(
        title=(d.get("title") or "").strip(),
        account=(d.get("account") or "").strip(),
        author=(d.get("author") or "").strip(),
        url=url,
        publish_ts=publish_ts,
    )

    return FetchResult(
        meta=meta,
        html=content_html,
        content_html=content_html,
        images=list(d.get("images") or []),
        channel="browser",
    )


_CN_TIME_RE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s*(\d{1,2})?:?(\d{2})?")


def _parse_cn_time(text: str) -> int:
    """把「8月28日 19:18」或「2026年8月28日 19:18」解析成时间戳。

    没写年份时按「不晚于现在」推断：如果这个月日比今天晚，就算去年。
    """
    m = _CN_TIME_RE.search(text or "")
    if not m:
        return 0
    year_s, mon_s, day_s, hh_s, mm_s = m.groups()
    now = time.localtime()
    year = int(year_s) if year_s else now.tm_year
    try:
        ts = time.mktime((year, int(mon_s), int(day_s),
                          int(hh_s or 0), int(mm_s or 0), 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return 0
    if not year_s and ts > time.time() + 86400:
        # 推成了未来 → 说明是去年的同一天
        try:
            ts = time.mktime((year - 1, int(mon_s), int(day_s),
                              int(hh_s or 0), int(mm_s or 0), 0, 0, 0, -1))
        except (ValueError, OverflowError):
            return 0
    return int(ts)


def fetch_article(url: str, timeout: int = 30, prefer: str = "direct",
                  api_base: str = PUBLIC_API_BASE, auth_key: str | None = None,
                  session: requests.Session | None = None,
                  use_browser: bool = True) -> FetchResult:
    """抓取一篇文章：直连 → 公共导出服务 → 浏览器渲染。

    prefer="api" 时前两条顺序相反。浏览器渲染始终最后（慢，但能救回 JS 渲染页）。
    """
    if url.startswith("weread:"):
        from .reader_content import fetch_reader_content
        return fetch_reader_content(url[len("weread:"):])
    # 链接不完整是最常见的失败原因，且微信的报错页毫无提示，所以先自查。
    if problem := diagnose_url(url):
        raise FetchError(problem)

    channels = ["direct", "api"] if prefer == "direct" else ["api", "direct"]
    if use_browser:
        channels.append("browser")
    errors: list[str] = []

    for channel in channels:
        if channel == "browser":
            try:
                return fetch_via_browser(url, timeout=max(timeout * 3, 90))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"browser: {exc}")
                continue

        try:
            html = (
                fetch_direct(url, timeout=timeout, session=session)
                if channel == "direct"
                else fetch_via_api(url, timeout=max(timeout, 60), api_base=api_base, auth_key=auth_key)
            )
        except Exception as exc:  # noqa: BLE001 - 逐通道兜底
            errors.append(f"{channel}: {exc}")
            continue

        reason = looks_like_error_page(html)
        if reason:
            errors.append(f"{channel}: {reason}")
            continue

        content_html = extract_content_html(html)
        if not content_html.strip():
            errors.append(f"{channel}: 正文为空")
            continue

        meta = parse_meta(html, url=url)
        return FetchResult(
            meta=meta,
            html=html,
            content_html=content_html,
            images=extract_images(content_html),
            channel=channel,
        )

    raise FetchError("抓取失败 —— " + "；".join(errors))


# --------------------------------------------------------------------------
# 历史文章列表（需要 auth-key）
# --------------------------------------------------------------------------

def _api_get(path: str, auth_key: str, api_base: str, params: dict,
             timeout: int) -> requests.Response:
    """带错误归一化的 GET。

    公共导出服务偶发 SSL EOF / 连接重置，统一转成 FetchError，
    让上层用一致方式处理，而不是漏出 requests 的异常类型。
    """
    try:
        return requests.get(
            f"{api_base.rstrip('/')}{path}",
            params=params,
            headers={"X-Auth-Key": auth_key, "User-Agent": DESKTOP_UA, "Referer": api_base + "/"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise FetchError(f"连接导出服务失败：{type(exc).__name__}: {exc}") from exc


def search_account(keyword: str, auth_key: str, api_base: str = PUBLIC_API_BASE,
                   begin: int = 0, size: int = 5, timeout: int = 30) -> list[dict[str, Any]]:
    """按名称搜索公众号，返回 [{fakeid, nickname, alias, signature}, ...]。"""
    resp = _api_get("/api/public/v1/account", auth_key, api_base,
                    {"keyword": keyword, "begin": begin, "size": size}, timeout)
    data = _json_or_error(resp)
    base = data.get("base_resp") or {}
    if base.get("ret") != 0:
        raise FetchError(f"搜索公众号失败：{base.get('err_msg') or resp.status_code}")
    return data.get("list") or []


def list_articles(fakeid: str, auth_key: str, api_base: str = PUBLIC_API_BASE,
                  begin: int = 0, size: int = 20, timeout: int = 30) -> list[dict[str, Any]]:
    """拉取一页历史文章（微信接口每页最多 20 条）。"""
    resp = _api_get("/api/public/v1/article", auth_key, api_base,
                    {"fakeid": fakeid, "begin": begin, "size": size}, timeout)
    data = _json_or_error(resp)
    base = data.get("base_resp") or {}
    if base.get("ret") != 0:
        msg = base.get("err_msg") or f"HTTP {resp.status_code}"
        ret = base.get("ret")
        if "认证" in str(msg) or "登录" in str(msg):
            raise FetchError(
                f"auth-key 无效或已过期（{msg}）。"
                "请重新扫码登录 down.mptext.top 获取新的 auth-key。"
            )
        # 200013 freq control：2026-07-30 起微信对「后台跨公众号文章列表」能力收紧，
        # 第一页零结果就直接返回该码（不是翻页累积限频）。搜索与正文抓取仍正常。
        # 这是平台级变更，重试和等待都不会恢复 —— 必须换用公开索引。
        if ret == 200013 or "freq control" in str(msg):
            raise FreqControlError(
                "微信后台的「跨公众号文章列表」接口已被平台限制（200013 freq control）。"
                "自 2026-07-30 起该能力对第三方收紧，第一页即失败，等待/重试不会恢复。"
                "改用公开索引（搜狗微信）获取文章列表。"
            )
        raise FetchError(f"拉取文章列表失败：{msg}")
    return data.get("articles") or []


def list_all_articles(fakeid: str, auth_key: str, api_base: str = PUBLIC_API_BASE,
                      page_size: int = 20, max_pages: int = 500,
                      sleep: float = 0.5, on_page=None) -> list[dict[str, Any]]:
    """翻页拉取全部历史文章，按 msgid 去重。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for page in range(max_pages):
        batch = list_articles(
            fakeid, auth_key, api_base=api_base, begin=page * page_size, size=page_size
        )
        if not batch:
            break
        added = 0
        for item in batch:
            key = str(item.get("appmsgid") or item.get("aid") or item.get("link") or "")
            if key and key not in seen:
                seen.add(key)
                out.append(item)
                added += 1
        if on_page:
            on_page(page + 1, len(out))
        if len(batch) < page_size or added == 0:
            break
        if sleep:
            time.sleep(sleep)
    return out


def _json_or_error(resp: requests.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as exc:
        raise FetchError(f"接口返回非 JSON（HTTP {resp.status_code}）") from exc


def verify_auth_key(auth_key: str, api_base: str = PUBLIC_API_BASE, timeout: int = 20) -> bool:
    """校验 auth-key 是否仍然有效。"""
    try:
        resp = requests.get(
            f"{api_base.rstrip('/')}/api/public/v1/authkey",
            headers={"X-Auth-Key": auth_key, "User-Agent": DESKTOP_UA},
            timeout=timeout,
        )
        return (resp.json() or {}).get("code") == 0
    except Exception:  # noqa: BLE001
        return False
