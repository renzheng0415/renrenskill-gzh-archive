"""微信读书 → 知识库 衔接层。

## 为什么需要这一层

抓「历史文章列表」这件事现在有两个来源：

1. `gzh/fetcher.py` + 公开 API —— 200013 之后只能拿到最近一周（搜狗兜底）
2. `weread-mp-fetcher`（Node）—— 通过微信读书，**能翻很深的历史**

这个模块把来源 2 的输出（JSON）接进已经写好的导出逻辑（`gzh/exporter.py`），
避免重复实现 Markdown 转换、图片下载、frontmatter、索引生成。

## 数据流

    weread-mp-fetcher --format json
            ↓  {name, bookId, items:[{t, title, url}], pagesFetched}
    本模块解析 + 按期限筛选
            ↓  逐篇调用 gzh.fetch_article 抓正文
    gzh.exporter 写 Markdown 知识库（含图片、frontmatter、index.md）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 允许作为脚本运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gzh.exporter import ExportedArticle, KnowledgeBase, write_manifest  # noqa: E402
from gzh.fetcher import FetchError, fetch_article, is_mp_article_url  # noqa: E402
from gzh.period import filter_by_period, parse_period  # noqa: E402

# weread-mp-fetcher 的安装位置（可用环境变量覆盖）
TOOL_DIR = Path(os.environ.get("WEREAD_TOOL_DIR", "~/.weread-mp-fetcher/tool")).expanduser()
TOOL_ENTRY = TOOL_DIR / "bin" / "weread.mjs"


class WereadError(RuntimeError):
    """微信读书侧调用失败。"""


@dataclass
class WereadArticle:
    """微信读书返回的一条文章线索。"""

    title: str
    url: str
    ts: int
    account: str = ""

    @property
    def date(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.ts)) if self.ts else ""

    def to_backend_shape(self) -> dict:
        """转成 gzh 内部通用的字段形状，便于复用期限筛选。"""
        return {
            "title": self.title,
            "link": self.url,
            "create_time": self.ts,
            "update_time": self.ts,
            "author_name": "",
            "digest": "",
            "copyright_stat": 0,
            "_source": "weread",
            "_account": self.account,
        }


def tool_available() -> bool:
    return TOOL_ENTRY.exists()


def run_tool(args: list[str], timeout: int = 300) -> str:
    """调用 weread-mp-fetcher，返回 stdout。

    失败时抛出带可读原因的 WereadError（把工具的 stderr 一起带出来）。
    """
    if not tool_available():
        raise WereadError(
            f"找不到 weread-mp-fetcher：{TOOL_ENTRY}\n"
            "安装：git clone https://github.com/Pengyf04/weread-mp-fetcher.git "
            "~/.weread-mp-fetcher/tool"
        )

    try:
        proc = subprocess.run(
            ["node", str(TOOL_ENTRY), *args],
            cwd=str(TOOL_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WereadError(f"调用超时（{timeout}s）：{' '.join(args)}") from exc
    except FileNotFoundError as exc:
        raise WereadError("找不到 node，请确认 Node.js 已安装（需要 >= 18）") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WereadError(_humanize(detail))

    return proc.stdout


def _humanize(stderr: str) -> str:
    """把工具的报错翻译成可操作提示。"""
    text = stderr.strip()
    if "9333" in text or "WebSocket" in text or "json/version" in text:
        return (
            "连不上专用 Chrome（端口 9333）。\n"
            "请先启动它：bash ~/.weread-mp-fetcher/start-chrome.sh\n"
            "然后在打开的窗口里扫码登录微信读书。"
        )
    if "-2010" in text or "登录" in text:
        return (
            "微信读书登录态已失效。\n"
            "请在专用 Chrome 窗口里重新扫码登录 https://weread.qq.com/"
        )
    if "2041" in text:
        return (
            "微信读书返回 -2041（页面上下文校验失败）。\n"
            "通常是阅读器页没打开好 —— 重跑一次，或先跑 --probe 看页面状态。"
        )
    if "验证码" in text or "captcha" in text.lower():
        return (
            "撞上验证码了。请在专用 Chrome 窗口里手动过一下验证码，然后重跑。"
        )
    if "maxRunsPerDay" in text or "次数" in text:
        return f"今日抓取次数已达上限（防风控）。\n{text[:300]}"
    return text[:600] or "weread-mp-fetcher 执行失败（无输出）"


def probe() -> str:
    """检查专用 Chrome 和页面状态，不消耗每日次数。"""
    return run_tool(["--probe"], timeout=90)


def resolve_from_url(url: str) -> tuple[str, str]:
    """从一篇文章链接解析出 (bookId, 公众号名)。

    bookId 靠 `__biz` 换算（本地算，不联网）：
        bookId = MP_WXS_ + base64解码(__biz)
    公众号名靠抓一次文章页拿（顺带确认这篇能打开）。

    这条路径**不需要先把号加进书架** —— 实测确认过。
    """
    import base64
    import re as _re

    m = _re.search(r"__biz=([A-Za-z0-9+/=%]+)", url)
    if not m:
        raise WereadError(
            "这个链接里没有 __biz，认不出是哪个公众号。\n"
            "请给完整文章链接（形如 https://mp.weixin.qq.com/s/xxx 或带 __biz 的长链）。"
        )
    biz = m.group(1)
    try:
        decoded = base64.b64decode(biz + "=" * (-len(biz) % 4)).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise WereadError(f"__biz 解码失败：{biz}") from exc
    if not decoded.isdigit():
        raise WereadError(f"__biz 解出来不是数字（{decoded}），这个链接可能不完整")

    book_id = f"MP_WXS_{decoded}"

    # 抓一次文章页拿公众号名；失败也不影响 bookId
    name = ""
    try:
        import requests

        resp = requests.get(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        }, timeout=25)
        html = resp.text
        nm = (_re.search(r'nickname\s*=\s*htmlDecode\(\s*["\']([^"\']+)', html)
              or _re.search(r'id=["\']js_name["\'][^>]*>\s*([^<]+?)\s*<', html))
        if nm:
            name = nm.group(1).strip()
    except Exception:  # noqa: BLE001 - 名字拿不到不算错误
        pass

    return book_id, name


def biz_to_book_id(fakeid: str) -> str:
    """`fakeid`（就是 `__biz`）→ `bookId`。

    两者是同一串数字的两种编码：

        fakeid = base64(数字)           例如 MzA3OTI3NjQyMA==
        bookId = "MP_WXS_" + 数字       例如 MP_WXS_3079276420

    实测：同一公众号的 fakeid 换算结果，与该号文章链接里 `__biz` 的换算结果逐字符一致。
    """
    import base64

    try:
        decoded = base64.b64decode(fakeid + "=" * (-len(fakeid) % 4)).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise WereadError(f"fakeid 解码失败：{fakeid}") from exc
    if not decoded.isdigit():
        raise WereadError(f"fakeid 解出来不是数字（{decoded}），这个 fakeid 可能不对")
    return f"MP_WXS_{decoded}"


def resolve_from_name(name: str, auth_key: str, api_base: str = "https://down.mptext.top",
                      pick: int = 0) -> tuple[str, str, list[dict]]:
    """用「公众号名字」解析出 (bookId, 公众号名, 全部候选)。

    ## 为什么需要这条路径

    从文章链接拿 `bookId` 要求用户先有一篇文章。但用户常常只知道号名，
    手上没有链接（比如还没关注、或不想去微信里翻）。

    `searchbiz` 接口**在 200013 之后仍然可用**（被封的是文章列表，不是搜索）。
    它返回的 `fakeid` 就是 `__biz`，直接换算出 bookId —— 于是「给名字就能抓」。

    ## 重名问题

    同一个名字可能对应多个号（实测搜一个名字常返回若干近似号）。
    所以这里返回全部候选，由调用方决定：
      · 只有一个 → 直接用
      · 有多个   → 让用户确认，或用 `pick` 指定第几个

    `auth_key` 是 down.mptext.top 的 key（`~/.config/gzh/config.json`）。
    """
    from .fetcher import FetchError, search_account

    try:
        accounts = search_account(name, auth_key, api_base=api_base, size=10)
    except FetchError as exc:
        raise WereadError(
            f"搜索公众号失败：{exc}\n"
            "  如果提示 auth-key 无效，请去 https://down.mptext.top 重新扫码获取。"
        ) from exc

    if not accounts:
        raise WereadError(
            f"没搜到叫「{name}」的公众号。\n"
            "  可能原因：① 名字不准 ② 该号未开启名称搜索\n"
            "  换个准确的号名，或改用 --from-url 给一篇文章链接。"
        )

    # 精确匹配优先，避免「某某研究院」被「某某研究所」抢走
    exact = [a for a in accounts if (a.get("nickname") or "").strip() == name.strip()]
    chosen = exact[0] if exact else accounts[min(pick, len(accounts) - 1)]

    book_id = biz_to_book_id(chosen.get("fakeid") or "")
    return book_id, (chosen.get("nickname") or name), accounts


def list_shelf() -> list[dict]:
    """列出微信读书书架里的公众号。

    `--shelf` 输出是 JSON 数组，但末尾可能跟一行人类可读的汇总
    （「共 10 个公众号。…」），所以只取第一个 `[` 到最后一个 `]`。
    """
    out = run_tool(["--shelf"], timeout=120)

    start, end = out.find("["), out.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(out[start:end + 1])
            if isinstance(data, list):
                return [
                    {"bookId": a.get("bookId", ""), "name": a.get("name") or a.get("bookId", ""),
                     "readerUrl": a.get("readerUrl", "")}
                    for a in data
                    if isinstance(a, dict) and a.get("bookId")
                ]
        except json.JSONDecodeError:
            pass

    # 兜底：老版本或格式变化时按行解析（形如 "MP_WXS_123  某某号"）
    accounts: list[dict] = []
    for line in out.splitlines():
        parts = line.split()
        for i, tok in enumerate(parts):
            if tok.startswith("MP_WXS_"):
                accounts.append({
                    "bookId": tok,
                    "name": " ".join(parts[i + 1:]).strip() or tok,
                    "readerUrl": "",
                })
                break
    return accounts


def fetch_articles(pages: int = 3, timeout: int = 600) -> dict[str, list[WereadArticle]]:
    """抓取 config.json 里配置的公众号文章列表。

    返回 {公众号名: [WereadArticle, ...]}。
    """
    out = run_tool(["--format", "json", "--pages", str(pages)], timeout=timeout)

    # 工具正常时 stdout 是纯 JSON；但可能混有进度输出，取第一个 { 到最后一个 }
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end < 0:
        raise WereadError(f"没有解析到 JSON 输出：\n{out[:500]}")

    try:
        data = json.loads(out[start:end + 1])
    except json.JSONDecodeError as exc:
        raise WereadError(f"JSON 解析失败：{exc}\n原始输出：{out[:500]}") from exc

    # 兼容两种可能的顶层结构：{sources:[...]} 或 [...]
    sources = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(sources, list):
        raise WereadError(f"无法识别的输出结构：{str(data)[:300]}")

    result: dict[str, list[WereadArticle]] = {}
    for src in sources:
        name = src.get("name") or src.get("bookId") or "未命名公众号"
        articles: list[WereadArticle] = []
        for item in src.get("items") or []:
            url = item.get("url") or ""
            if not url or not is_mp_article_url(url):
                continue
            articles.append(WereadArticle(
                title=item.get("title") or "",
                url=url,
                ts=int(item.get("t") or 0),
                account=name,
            ))
        if articles:
            result[name] = articles

    return result


def export_from_weread(period: str, kb_root: str, pages: int = 3,
                       concurrency: int = 2, limit: int | None = None,
                       no_images: bool = False, sleep: float = 2.0) -> dict:
    """端到端：微信读书列表 → 期限筛选 → 抓正文 → 写知识库。

    返回一份结果摘要（可直接 json 序列化）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    per = parse_period(period)
    by_account = fetch_articles(pages=pages)

    if not by_account:
        return {
            "ok": False,
            "error": "微信读书没有返回任何文章。",
            "hint": "先在 config.json 里配置 accounts，或跑 --shelf 看有哪些号。",
        }

    summary = {
        "ok": True,
        "period": per.key,
        "period_label": per.label,
        "accounts": [],
        "total_exported": 0,
        "total_failed": 0,
    }

    for name, articles in by_account.items():
        shaped = [a.to_backend_shape() for a in articles]
        selected = filter_by_period(shaped, per)
        if limit:
            selected = selected[:limit]

        kb = KnowledgeBase(kb_root, account=name)
        existing = kb.load_index()
        todo = [a for a in selected if a["link"] not in existing]
        skipped = len(selected) - len(todo)

        exported: list[ExportedArticle] = []
        failed: list[dict] = []

        if todo:
            def work(item: dict) -> ExportedArticle:
                res = fetch_article(item["link"], timeout=30)
                if item.get("create_time"):
                    res.meta.publish_ts = int(item["create_time"])
                if item.get("title"):
                    res.meta.title = item["title"]
                if not res.meta.account:
                    res.meta.account = name
                art = kb.export_article(
                    res.meta, res.content_html, res.images,
                    download_image_files=not no_images,
                )
                art.channel = f"weread+{res.channel}"
                return art

            with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                futures = {pool.submit(work, it): it for it in todo}
                for fut in as_completed(futures):
                    item = futures[fut]
                    try:
                        exported.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        failed.append({"url": item["link"], "error": str(exc)[:200]})
                    if sleep:
                        time.sleep(sleep / max(1, concurrency))

        merged = {**existing, **{a.url: a.to_dict() for a in exported}}
        all_arts = [ExportedArticle(**v) for v in merged.values()]
        kb.save_index(all_arts)
        index_path = kb.write_index_md(all_arts, f"{per.label}（微信读书）")
        write_manifest(kb, all_arts, per.label, extra={
            "source": "weread",
            "in_period": len(selected),
            "failed": failed,
        })

        summary["accounts"].append({
            "account": name,
            "in_period": len(selected),
            "exported": len(exported),
            "skipped": skipped,
            "failed": len(failed),
            "total_in_kb": len(all_arts),
            "kb_dir": str(kb.dir),
            "index_path": str(index_path),
            "failed_items": failed[:10],
            "titles": [a.title for a in exported[:50]],
        })
        summary["total_exported"] += len(exported)
        summary["total_failed"] += len(failed)

    summary["ok"] = summary["total_failed"] == 0
    return summary
