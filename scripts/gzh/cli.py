"""gzh —— 公众号文章获取插件 命令行入口。

三个核心能力：
1. 链接抓取：抓单篇文章正文
2. 期限筛选：1m / 3m / 6m / 1y / all
3. 本地导出：写成知识库 Markdown

用法示例：
    python -m gzh article "https://mp.weixin.qq.com/s/xxxx"
    python -m gzh account "深圳楼市" --period 3m --export
    python -m gzh sync --config accounts.yaml
    python -m gzh login-check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import Config
from .exporter import KnowledgeBase, ExportedArticle, write_manifest
from .fetcher import (
    FetchError,
    FreqControlError,
    fetch_article,
    is_mp_article_url,
    list_all_articles,
    search_account,
    verify_auth_key,
)
from .period import PERIOD_LABELS, PeriodError, filter_by_period, parse_period
from .sogou import list_account_articles
from .weread import WereadError, export_from_weread, list_shelf, probe, tool_available

# ---------------------------------------------------------------- 输出辅助

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def ok(msg: str) -> None:
    print(_c("✅ " + msg, "32"))


def warn(msg: str) -> None:
    print(_c("⚠️  " + msg, "33"))


def err(msg: str) -> None:
    print(_c("❌ " + msg, "31"), file=sys.stderr)


def info(msg: str) -> None:
    print("   " + msg)


# ---------------------------------------------------------------- 子命令

def cmd_article(args: argparse.Namespace) -> int:
    """抓取单篇文章并导出 Markdown。"""
    cfg = Config.load(args.config)
    url = args.url

    if not is_mp_article_url(url):
        err(f"不是微信公众号文章链接：{url}")
        return 2

    print(f"→ 抓取：{url}")
    try:
        result = fetch_article(
            url,
            timeout=cfg.timeout,
            prefer=args.channel,
            api_base=cfg.api_base,
            auth_key=cfg.auth_key,
        )
    except FetchError as exc:
        err(str(exc))
        info("提示：确认链接是 https://mp.weixin.qq.com/s?... 形式的文章链接，且文章未被删除。")
        return 1

    meta = result.meta
    ok(f"抓到正文（通道：{result.channel}，{len(result.content_html)} 字节 HTML，{len(result.images)} 张图）")
    info(f"标题：{meta.title or '(未解析到)'}")
    info(f"公众号：{meta.account or '(未解析到)'}")
    info(f"发布：{meta.publish_datetime or '(未解析到)'}")
    if meta.is_original:
        info("原创：是")

    if args.dry_run:
        warn("--dry-run：不写文件")
        return 0

    kb = KnowledgeBase(args.kb or cfg.kb_root, account=meta.account or "未命名公众号")
    art = kb.export_article(
        meta,
        result.content_html,
        result.images,
        tags=cfg.tags,
        download_image_files=not args.no_images,
    )
    art.channel = result.channel

    existing = kb.load_index()
    existing[meta.url] = art.to_dict()
    all_arts = [ExportedArticle(**v) for v in existing.values()]
    kb.save_index(all_arts)
    kb.write_index_md(all_arts, period_label="单篇")

    ok(f"已导出：{kb.root / art.path}")
    info(f"知识库目录：{kb.dir}")
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    """按公众号名称抓取历史文章，按期限筛选后导出。"""
    cfg = Config.load(args.config)

    if not cfg.auth_key:
        err("缺少 auth-key —— 抓取历史文章列表必须携带 auth-key。")
        print()
        info("获取方式：")
        info("  1. 打开 https://down.mptext.top 并用微信扫码登录（选你自己的公众号/服务号）")
        info("  2. 登录后在「API」页面复制 auth-key（4 天有效）")
        info("  3. 保存：python -m gzh config --set-auth-key <你的key>")
        print()
        info("说明：auth-key 是你自己账号的登录态，用来搜索并抓取【任意其他博主】的文章。")
        info("      如果你只想抓单篇文章，用：python -m gzh article <链接>（不需要 auth-key）")
        return 2

    if not verify_auth_key(cfg.auth_key, api_base=cfg.api_base):
        err("auth-key 无效或已过期（有效期 4 天）。请重新扫码获取。")
        return 2

    try:
        period = parse_period(args.period)
    except PeriodError as exc:
        err(str(exc))
        return 2

    print(f"→ 搜索公众号：{args.name}")
    try:
        accounts = search_account(args.name, cfg.auth_key, api_base=cfg.api_base, size=args.search_size)
    except FetchError as exc:
        err(str(exc))
        return 1

    if not accounts:
        err(f"没搜到「{args.name}」。")
        info("可能原因：该公众号关闭了「允许通过名称搜索」（博主自己的隐私设置，无法绕过）。")
        info("替代方案：用 python -m gzh article <文章链接> 逐篇抓取。")
        return 1

    if len(accounts) > 1 and not args.pick:
        print()
        warn(f"搜到 {len(accounts)} 个公众号，请用 --pick <序号> 指定：")
        for i, acc in enumerate(accounts, 1):
            info(f"[{i}] {acc.get('nickname')}  (fakeid={acc.get('fakeid')})  "
                 f"{acc.get('signature', '')[:40]}")
        return 0

    account = accounts[(args.pick or 1) - 1]
    fakeid = account.get("fakeid", "")
    nickname = account.get("nickname", args.name)
    ok(f"目标公众号：{nickname}（fakeid={fakeid}）")

    print(f"→ 拉取历史文章列表（期限：{period.label}）…")

    def on_page(page: int, total: int) -> None:
        print(f"\r   已拉取 {total} 篇（第 {page} 页）…", end="", flush=True)

    raw: list[dict] = []
    source = "后台接口"
    try:
        raw = list_all_articles(
            fakeid, cfg.auth_key, api_base=cfg.api_base,
            page_size=args.page_size, max_pages=args.max_pages, on_page=on_page,
        )
    except FreqControlError as exc:
        print()
        warn("微信后台的文章列表接口被平台限制（200013 freq control）。")
        info(str(exc))
        if args.no_fallback:
            return 1
        print()
        print(f"→ 自动切换到公开索引（搜狗微信），抓取「{nickname}」的近期文章…")
        try:
            raw = list_account_articles(
                nickname, pages=args.fallback_pages, sleep=args.sleep,
            )
        except FetchError as fb_exc:
            err(f"公开索引也失败了：{fb_exc}")
            info("稍后重试，或加大 --sleep 降低频率。")
            return 1
        source = "公开索引"
    except FetchError as exc:
        print()
        err(str(exc))
        return 1
    print()

    if not raw:
        warn("没有拉到任何文章（可能全部已删除，或该账号无发布权限）。")
        return 1

    ok(f"共拉到 {len(raw)} 篇（来源：{source}）")
    if source == "公开索引":
        warn("公开索引只覆盖近期公开文章，不是完整历史；深度翻页会被反爬拦截。")
    selected = filter_by_period(raw, period)
    ok(f"按「{period.label}」筛选后剩 {len(selected)} 篇")

    if args.limit:
        selected = selected[: args.limit]
        info(f"按 --limit 截取前 {len(selected)} 篇")

    if not selected:
        warn(f"该公众号在「{period.label}」内没有发文。")
        return 0

    if args.list_only:
        print()
        for item in selected:
            ts = item.get("create_time") or item.get("update_time") or 0
            date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "未知"
            print(f"  {date}  {item.get('title', '')}")
        return 0

    if args.dry_run:
        warn("--dry-run：不写文件")
        return 0

    # 增量：跳过已导出的
    kb = KnowledgeBase(args.kb or cfg.kb_root, account=nickname)
    existing = kb.load_index()
    todo = [it for it in selected if it.get("link") and it["link"] not in existing]
    skipped = len(selected) - len(todo)
    if skipped:
        info(f"增量跳过已导出 {skipped} 篇")

    if not todo:
        ok("没有新文章需要导出。")
        kb.write_index_md([ExportedArticle(**v) for v in existing.values()], period.label)
        return 0

    print(f"→ 导出 {len(todo)} 篇（并发 {args.concurrency}，图片{'不' if args.no_images else ''}下载）…")
    exported: list[ExportedArticle] = []
    failed: list[tuple[str, str]] = []

    def work(item: dict) -> ExportedArticle:
        url = item["link"]
        res = fetch_article(
            url, timeout=cfg.timeout, prefer=args.channel,
            api_base=cfg.api_base, auth_key=cfg.auth_key,
        )
        # 列表接口给的时间/标题比正文页更可靠，优先用列表的
        if item.get("create_time"):
            res.meta.publish_ts = int(item["create_time"])
        if item.get("title"):
            res.meta.title = item["title"]
        if not res.meta.account:
            res.meta.account = nickname
        art = kb.export_article(
            res.meta, res.content_html, res.images,
            tags=cfg.tags, download_image_files=not args.no_images,
        )
        art.channel = res.channel
        return art

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(work, it): it for it in todo}
        for fut in as_completed(futures):
            item = futures[fut]
            done += 1
            try:
                art = fut.result()
                exported.append(art)
                print(f"\r   [{done}/{len(todo)}] {art.title[:36]}", flush=True)
            except Exception as exc:  # noqa: BLE001
                failed.append((item.get("link", ""), str(exc)))
                print(f"\r   [{done}/{len(todo)}] 失败：{str(exc)[:50]}", flush=True)
    print()

    # 落盘索引与清单
    merged = {**existing, **{a.url: a.to_dict() for a in exported}}
    all_arts = [ExportedArticle(**v) for v in merged.values()]
    kb.save_index(all_arts)
    index_path = kb.write_index_md(all_arts, period.label)
    manifest = write_manifest(
        kb, all_arts, period.label,
        extra={"period_key": period.key, "total_in_period": len(selected),
               "failed": [{"url": u, "error": e} for u, e in failed]},
    )

    print()
    ok(f"导出完成：成功 {len(exported)} 篇，失败 {len(failed)} 篇")
    info(f"知识库目录：{kb.dir}")
    info(f"索引：{index_path}")
    info(f"清单：{manifest}")
    if failed:
        warn("失败清单：")
        for u, e in failed[:10]:
            info(f"  {u}  →  {e[:70]}")
    return 0 if not failed else 1


def cmd_urls(args: argparse.Namespace) -> int:
    """批量抓取一批文章链接（不需要 auth-key）。

    适合：从别处拿到一批链接，或配合 account --list-only 导出的链接清单。
    """
    cfg = Config.load(args.config)

    urls: list[str] = []
    for src in args.source:
        p = Path(src).expanduser()
        if p.exists() and p.is_file():
            text = p.read_text(encoding="utf-8")
            urls.extend(re.findall(r"https?://mp\.weixin\.qq\.com/[^\s\"'<>]+", text))
        elif is_mp_article_url(src):
            urls.append(src)
        else:
            err(f"既不是文件也不是公众号链接：{src}")
            return 2

    # 去重保序
    seen: set[str] = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    if not urls:
        err("没有找到任何公众号文章链接。")
        return 2

    ok(f"共 {len(urls)} 个链接（去重后）")

    if args.dry_run:
        for u in urls:
            print("  " + u)
        return 0

    kb_root = args.kb or cfg.kb_root
    exported: list[ExportedArticle] = []
    failed: list[tuple[str, str]] = []

    # 增量：扫描知识库根下各公众号的索引，跳过已导出的链接
    if not args.force:
        known: set[str] = set()
        root_path = Path(kb_root).expanduser()
        if root_path.is_dir():
            for idx in root_path.glob("*/.gzh-index.json"):
                try:
                    data = json.loads(idx.read_text(encoding="utf-8"))
                    known.update(a["url"] for a in data.get("articles", []) if a.get("url"))
                except (json.JSONDecodeError, OSError, KeyError):
                    continue
        if known:
            before = len(urls)
            urls = [u for u in urls if u not in known]
            if before != len(urls):
                info(f"增量跳过已导出 {before - len(urls)} 个链接")
            if not urls:
                ok("没有新链接需要抓取。")
                return 0

    def work(url: str):
        res = fetch_article(
            url, timeout=cfg.timeout, prefer=args.channel,
            api_base=cfg.api_base, auth_key=cfg.auth_key,
        )
        kb = KnowledgeBase(kb_root, account=res.meta.account or "未命名公众号")
        art = kb.export_article(
            res.meta, res.content_html, res.images,
            tags=cfg.tags, download_image_files=not args.no_images,
        )
        art.channel = res.channel
        return kb, art

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(work, u): u for u in urls}
        for fut in as_completed(futures):
            url = futures[fut]
            done += 1
            try:
                kb, art = fut.result()
                exported.append(art)
                print(f"\r   [{done}/{len(urls)}] {art.title[:36]}", flush=True)
            except Exception as exc:  # noqa: BLE001
                failed.append((url, str(exc)))
                print(f"\r   [{done}/{len(urls)}] 失败：{str(exc)[:50]}", flush=True)
    print()

    # 按公众号分组重建索引
    by_account: dict[str, list[ExportedArticle]] = {}
    for art in exported:
        by_account.setdefault(art.account, []).append(art)

    for account, arts in by_account.items():
        kb = KnowledgeBase(kb_root, account=account)
        existing = kb.load_index()
        merged = {**existing, **{a.url: a.to_dict() for a in arts}}
        all_arts = [ExportedArticle(**v) for v in merged.values()]
        kb.save_index(all_arts)
        kb.write_index_md(all_arts, period_label="批量链接")
        write_manifest(kb, all_arts, "批量链接",
                       extra={"failed": [{"url": u, "error": e} for u, e in failed]})

    print()
    ok(f"完成：成功 {len(exported)} 篇，失败 {len(failed)} 篇")
    info(f"知识库根目录：{Path(kb_root).expanduser().resolve()}")
    for account, arts in by_account.items():
        info(f"  {account}：{len(arts)} 篇")
    if failed:
        warn("失败清单：")
        for u, e in failed[:10]:
            info(f"  {u[:80]}  →  {e[:70]}")
    return 0 if not failed else 1


def cmd_weread(args: argparse.Namespace) -> int:
    """通过微信读书抓历史文章（200013 之后唯一能翻深的路径）。"""
    cfg = Config.load(args.config)

    if not tool_available():
        err("还没装微信读书抓取工具。")
        info("安装：")
        info("  mkdir -p ~/.weread-mp-fetcher && cd ~/.weread-mp-fetcher")
        info("  git clone https://github.com/Pengyf04/weread-mp-fetcher.git tool")
        return 2

    # --probe：只看状态，不消耗每日次数
    if args.probe:
        print("→ 检查专用 Chrome 与页面状态…")
        try:
            print(probe())
            return 0
        except WereadError as exc:
            err(str(exc))
            return 1

    # --shelf：列出书架里的公众号
    if args.shelf:
        print("→ 读取微信读书书架…")
        try:
            accounts = list_shelf()
        except WereadError as exc:
            err(str(exc))
            return 1
        if not accounts:
            warn("书架里没有公众号。")
            info("在专用 Chrome 里打开微信读书，订阅几个号，或跑 --add <文章链接>")
            return 0
        ok(f"书架里有 {len(accounts)} 个公众号：")
        for a in accounts:
            info(f"  {a['bookId']:24} {a['name']}")
        info("")
        info("把想抓的号填进 ~/.weread-mp-fetcher/tool/config.json 的 accounts")
        return 0

    # 默认：抓取并导出
    try:
        period = parse_period(args.period)
    except PeriodError as exc:
        err(str(exc))
        return 2

    kb_root = args.kb or cfg.kb_root
    print(f"→ 通过微信读书抓取（期限：{period.label}，每号 {args.pages} 页）…")
    info("提示：微信读书一页 ≈ 20 次群发 ≈ 70-80 篇文章")
    print()

    try:
        summary = export_from_weread(
            period=period.key,
            kb_root=kb_root,
            pages=args.pages,
            concurrency=args.concurrency,
            limit=args.limit,
            no_images=args.no_images,
            sleep=args.sleep,
        )
    except WereadError as exc:
        err(str(exc))
        return 1

    if not summary.get("ok") and summary.get("error"):
        err(summary["error"])
        if summary.get("hint"):
            info(summary["hint"])
        return 1

    print()
    for acc in summary["accounts"]:
        print(f"  {acc['account']}")
        info(f"    期限内：{acc['in_period']} 篇")
        info(f"    本次导出：{acc['exported']} 篇")
        if acc["skipped"]:
            info(f"    增量跳过：{acc['skipped']} 篇")
        if acc["failed"]:
            info(f"    失败：{acc['failed']} 篇")
        info(f"    知识库累计：{acc['total_in_kb']} 篇")
        info(f"    目录：{acc['kb_dir']}")
        for t in acc["titles"][:10]:
            info(f"      · {t[:50]}")
        if len(acc["titles"]) > 10:
            info(f"      …还有 {len(acc['titles']) - 10} 篇")
        for f in acc["failed_items"][:5]:
            info(f"    ❌ {f['url'][:70]} → {f['error'][:60]}")

    print()
    if summary["total_failed"]:
        warn(f"完成：导出 {summary['total_exported']} 篇，失败 {summary['total_failed']} 篇")
    else:
        ok(f"完成：导出 {summary['total_exported']} 篇，全部成功")
    return 0 if summary["ok"] else 1


def cmd_crawl(args: argparse.Namespace) -> int:
    """全量抓取一个公众号 —— 可中断、可续跑、按天限频。

    与 `weread` 的区别：`weread` 是「抓最近 N 页」，本命令是「一路翻到最早」。
    每次运行接着上次的断点跑，可以每天跑一次，跑够为止。
    """
    from .crawl import DEFAULT_CONFIG, CrawlError, crawl

    cfg = Config.load(args.config)
    kb_root = args.kb or cfg.kb_root

    book_id = args.book_id
    account = args.account

    # 三种定位目标号的方式，优先级从高到低
    if not book_id and args.from_url:
        # ① 给一篇文章链接（推荐：不依赖任何 key）
        from .weread import resolve_from_url
        book_id, account = resolve_from_url(args.from_url)
    elif not book_id and args.from_name:
        # ② 给公众号名字（走 searchbiz，需要 auth-key —— 但它至今仍可用）
        from .weread import WereadError, resolve_from_name
        try:
            book_id, account, candidates = resolve_from_name(
                args.from_name, cfg.auth_key, api_base=cfg.api_base)
        except WereadError as exc:
            err(str(exc))
            return 1
        if not cfg.auth_key:
            warn("没配 auth-key，按名字搜索用不了 —— 改用 --from-url 给一篇文章链接即可")
        if len(candidates) > 1:
            print(f"  「{args.from_name}」搜到 {len(candidates)} 个同名/近名号：")
            for i, a in enumerate(candidates[:6]):
                mark = " ← 选中的" if (a.get("nickname") or "") == account else ""
                info(f"    [{i}] {a.get('nickname')}{mark}")
            info("  想换一个就用 --from-url 给那个号的文章链接，或直接 --book-id")
        ok(f"解析到：{account}")
        info(f"  bookId = {book_id}")

    if not book_id:
        err("需要指定目标公众号：\n"
            "  --from-url <该号任意一篇文章链接>   （推荐，不需要 key）\n"
            "  --from-name <公众号名字>            （需要 auth-key）\n"
            "  --book-id MP_WXS_xxxxxxxxxx         （已知 bookId 时）")
        return 2
    if not account:
        account = book_id

    conf = dict(DEFAULT_CONFIG)
    if args.interval is not None:
        conf["request_interval_sec"] = args.interval
    if args.per_day is not None:
        conf["requests_per_day"] = args.per_day
    if args.pages is not None:
        conf["pages_per_run"] = args.pages
    conf["concurrency"] = args.concurrency
    conf["no_images"] = args.no_images
    conf["export_sleep"] = args.sleep

    print("━" * 66)
    print("  全量抓取（微信读书）")
    print("━" * 66)
    print(f"  公众号     {account}")
    print(f"  bookId     {book_id}")
    print(f"  知识库     {kb_root}")
    print(f"  限频       每页间隔 {conf['request_interval_sec']:.0f} 秒，"
          f"每天最多 {conf['requests_per_day']} 页")
    print(f"  本次运行   最多 {conf['pages_per_run']} 页")
    print("  提示       可随时 Ctrl-C；下次运行会从断点继续")
    print("━" * 66)

    try:
        result = crawl(book_id, account, kb_root, config=conf, reset=args.reset)
    except CrawlError as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        print()
        warn("已中断 —— 进度已保存，下次运行会从断点继续")
        return 130

    print()
    print("━" * 66)
    print("  本次结果")
    print("━" * 66)
    info(f"  本次翻页     {result['pages_this_run']} 页")
    info(f"  新增文章     {result['new_articles']} 篇")
    info(f"  导出成功     {result['exported']} 篇")
    if result["failed"]:
        warn(f"  导出失败     {result['failed']} 篇")
        for f in result["failures"][:5]:
            info(f"      ❌ {f.get('title', '')[:36]} → {f['error'][:50]}")
    print()
    info(f"  累计已抓     {result['total_seen']} 篇")
    info(f"  累计翻页     {result['pages_done']} 页")
    info(f"  下次 offset  {result['next_offset']}")
    if result.get("oldest"):
        info(f"  已抓时间范围 {result['oldest']} ~ {result.get('newest', '?')}")
    print()
    if result["finished"]:
        ok(f"✅ 已翻到尽头 —— 该号全量抓取完成（共 {result['total_seen']} 篇）")
    else:
        warn(f"⏸  未抓完 —— {result['stopped_reason']}")
        info("   再跑一次本命令即可继续（会自动从断点续跑）")
    print("━" * 66)
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """补齐列表里见过、但正文没落盘的文章。"""
    from .crawl import CrawlError, backfill_missing

    cfg = Config.load(args.config)
    kb_root = args.kb or cfg.kb_root

    try:
        result = backfill_missing(
            account=args.account,
            kb_root=kb_root,
            concurrency=args.concurrency,
            no_images=args.no_images,
            sleep=args.sleep,
            limit=args.limit,
        )
    except CrawlError as exc:
        err(str(exc))
        return 1

    print()
    info(f"  缺失       {result['missing']} 篇")
    info(f"  补齐成功   {result['exported']} 篇")
    if result["failed"]:
        warn(f"  补齐失败   {result['failed']} 篇")
        for f in result["failures"][:5]:
            info(f"      ❌ {f.get('title', '')[:40]} → {f['error'][:50]}")
    if result.get("total_in_kb"):
        info(f"  知识库现有 {result['total_in_kb']} 篇")
    return 0 if not result["failed"] else 1


def cmd_pace(args: argparse.Namespace) -> int:
    """用大白话报告当前抓取节奏。"""
    from .ratelimit import COOLDOWN_AFTER_ERROR, SAFE_MIN_INTERVAL, SAFE_PER_DAY, SAFE_PER_HOUR
    from .ratelimit import check as rl_check
    from .ratelimit import stats as rl_stats

    s = rl_stats()
    d = rl_check()

    print("━" * 62)
    print("  抓取节奏")
    print("━" * 62)
    print(f"  最近 1 小时   发了 {s['last_hour']} 个请求（上限 {SAFE_PER_HOUR}）")
    print(f"  最近 24 小时  发了 {s['last_day']} 个请求（上限 {SAFE_PER_DAY}）")

    if s["seconds_since_last"] is None:
        print("  上次请求      还没有记录")
    else:
        print(f"  上次请求      {s['seconds_since_last']:.0f} 秒前")

    print(f"  24h 内风控    {s['errors_24h']} 次")
    print()

    if not d.allowed:
        warn(f"已达上限：{d.reason}")
        print("  建议：明天再跑，或调大 --per-day")
    elif d.wait > 0:
        warn(f"现在发请求会被拦下，需要等 {d.wait:.0f} 秒")
        print(f"  原因：{d.reason}")
    else:
        ok("现在可以发请求")

    print()
    print("  安全节奏（默认值，刻意保守）：")
    print(f"    · 任何两个请求间隔 ≥ {SAFE_MIN_INTERVAL:.0f} 秒")
    print(f"    · 每小时 ≤ {SAFE_PER_HOUR} 个请求")
    print(f"    · 每 24 小时 ≤ {SAFE_PER_DAY} 个请求")
    print(f"    · 命中风控后强制静默 {COOLDOWN_AFTER_ERROR // 60} 分钟")
    print()
    print("  这些限制对所有调用都生效，包括临时测试 —— 绕不过去。")
    print("━" * 62)
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """从 articles/ 里的 Markdown 反推重建索引。

    用途：导出中途被打断（Ctrl-C / 限流 / 进程被杀）后，磁盘上文章是全的，
    但 index.md 与 manifest.json 还停在旧状态。跑这个就能补回来。
    """
    cfg = Config.load(args.config)
    kb_root = args.kb or cfg.kb_root

    from .exporter import KnowledgeBase, rebuild_index_from_files, write_manifest

    root = Path(kb_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()

    if args.account:
        accounts = [args.account]
    else:
        if not root.exists():
            err(f"知识库目录不存在：{root}")
            return 2
        accounts = [d.name for d in sorted(root.iterdir())
                    if d.is_dir() and (d / "articles").is_dir()]
        if not accounts:
            err(f"{root} 下没有找到任何公众号目录")
            return 1

    total = 0
    for name in accounts:
        kb = KnowledgeBase(str(root), name)
        if not kb.articles_dir.exists():
            warn(f"{name}：没有 articles/ 目录，跳过")
            continue
        articles = rebuild_index_from_files(kb)
        if not articles:
            warn(f"{name}：没解析到任何文章")
            continue
        kb.save_index(articles)
        index_path = kb.write_index_md(articles, "（索引重建）")
        write_manifest(kb, articles, "索引重建", extra={"source": "rebuild"})
        ok(f"{name}：重建 {len(articles)} 篇 → {index_path}")
        dates = [a.publish_date for a in articles if a.publish_date]
        if dates:
            info(f"    日期范围：{min(dates)} ~ {max(dates)}")
        total += len(articles)

    print()
    ok(f"合计重建 {total} 篇")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """读写配置。"""
    cfg = Config.load(args.config)

    if args.set_auth_key is not None:
        cfg.auth_key = args.set_auth_key.strip()
        cfg.save()
        ok(f"auth-key 已保存到 {cfg.path}")
        if verify_auth_key(cfg.auth_key, api_base=cfg.api_base):
            ok("校验通过，auth-key 有效。")
        else:
            warn("校验未通过：key 无效或已过期（有效期 4 天）。")
        return 0

    if args.set_kb:
        cfg.kb_root = args.set_kb
        cfg.save()
        ok(f"知识库目录已设为：{cfg.kb_root}")
        return 0

    if args.show or len(sys.argv) == 2:
        print(json.dumps(cfg.masked(), ensure_ascii=False, indent=2))
        return 0

    return 0


def cmd_login_check(args: argparse.Namespace) -> int:
    """检查 auth-key 状态。"""
    cfg = Config.load(args.config)
    if not cfg.auth_key:
        warn("尚未配置 auth-key。")
        info("配置：python -m gzh config --set-auth-key <key>")
        info("获取：https://down.mptext.top 扫码登录后，在 API 页面复制（4 天有效）")
        return 1

    if verify_auth_key(cfg.auth_key, api_base=cfg.api_base):
        ok("auth-key 有效，可以抓取历史文章列表。")
        return 0

    err("auth-key 已失效（有效期 4 天）。")
    info("重新扫码获取：https://down.mptext.top")
    info("然后：python -m gzh config --set-auth-key <新key>")
    return 1


def cmd_periods(args: argparse.Namespace) -> int:
    """列出可用期限。"""
    print("可用期限：")
    for key, label in PERIOD_LABELS.items():
        print(f"  {key:<5} {label}")
    return 0


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gzh",
        description="公众号文章获取插件 —— 抓正文 / 按期限筛选历史文章 / 导出本地 Markdown 知识库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 1. 抓单篇文章（不需要 auth-key）
  python -m gzh article "https://mp.weixin.qq.com/s/xxxx"

  # 2. 按公众号名抓最近 3 个月（需要 auth-key）
  python -m gzh account "深圳楼市" --period 3m

  # 3. 先看列表不下载
  python -m gzh account "深圳楼市" --period 1y --list-only

  # 4. 配置
  python -m gzh config --set-auth-key <key>
  python -m gzh config --set-kb "01_内容生产HQ/公众号素材库"
""",
    )
    parser.add_argument("--config", help="配置文件路径（默认 ~/.config/gzh/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_art = sub.add_parser("article", help="抓取单篇文章并导出 Markdown")
    p_art.add_argument("url", help="公众号文章链接")
    p_art.add_argument("--kb", help="知识库根目录")
    p_art.add_argument("--channel", choices=["direct", "api"], default="direct",
                       help="抓取通道（默认 direct 直连）")
    p_art.add_argument("--no-images", action="store_true", help="不下载图片")
    p_art.add_argument("--dry-run", action="store_true", help="只抓取不写文件")
    p_art.set_defaults(func=cmd_article)

    p_acc = sub.add_parser("account", help="按公众号名抓历史文章并导出")
    p_acc.add_argument("name", help="公众号名称")
    p_acc.add_argument("--period", default="all",
                       help="期限：1m/3m/6m/1y/all（默认 all）")
    p_acc.add_argument("--kb", help="知识库根目录")
    p_acc.add_argument("--pick", type=int, help="搜到多个公众号时指定第几个")
    p_acc.add_argument("--limit", type=int, help="最多导出多少篇")
    p_acc.add_argument("--list-only", action="store_true", help="只列标题不下载")
    p_acc.add_argument("--dry-run", action="store_true", help="不写文件")
    p_acc.add_argument("--no-images", action="store_true", help="不下载图片")
    p_acc.add_argument("--channel", choices=["direct", "api"], default="direct")
    p_acc.add_argument("--concurrency", type=int, default=3, help="并发数（默认 3）")
    p_acc.add_argument("--page-size", type=int, default=20, help="列表每页条数（最大 20）")
    p_acc.add_argument("--max-pages", type=int, default=500, help="最多翻页数")
    p_acc.add_argument("--search-size", type=int, default=5, help="搜索返回条数")
    p_acc.add_argument("--no-fallback", action="store_true",
                       help="后台接口被限频时，不自动切换到公开索引")
    p_acc.add_argument("--fallback-pages", type=int, default=3,
                       help="公开索引翻页数（默认 3，每页约 10 条）")
    p_acc.add_argument("--sleep", type=float, default=2.0,
                       help="公开索引请求间隔秒数（默认 2，太频会触发验证码）")
    p_acc.set_defaults(func=cmd_account)

    p_urls = sub.add_parser("urls", help="批量抓取一批文章链接（不需要 auth-key）")
    p_urls.add_argument("source", nargs="+", help="链接，或含链接的 txt/json/csv 文件")
    p_urls.add_argument("--kb", help="知识库根目录")
    p_urls.add_argument("--channel", choices=["direct", "api"], default="direct")
    p_urls.add_argument("--no-images", action="store_true", help="不下载图片")
    p_urls.add_argument("--dry-run", action="store_true", help="只列链接不下载")
    p_urls.add_argument("--concurrency", type=int, default=3, help="并发数（默认 3）")
    p_urls.add_argument("--force", action="store_true", help="忽略已导出记录，强制重新抓取")
    p_urls.set_defaults(func=cmd_urls)

    p_wr = sub.add_parser("weread", help="通过微信读书抓历史文章（唯一能翻深的路径）")
    p_wr.add_argument("--period", default="all", help="期限：1m/3m/6m/1y/all（默认 all）")
    p_wr.add_argument("--pages", type=int, default=3,
                      help="每个号翻几页（默认 3，一页≈70-80 篇；上限见 config.json）")
    p_wr.add_argument("--kb", help="知识库根目录")
    p_wr.add_argument("--limit", type=int, help="每个号最多导出多少篇")
    p_wr.add_argument("--concurrency", type=int, default=2, help="并发抓正文数（默认 2）")
    p_wr.add_argument("--sleep", type=float, default=2.0, help="每篇之间的间隔秒数")
    p_wr.add_argument("--no-images", action="store_true", help="不下载图片")
    p_wr.add_argument("--shelf", action="store_true", help="列出微信读书书架里的公众号")
    p_wr.add_argument("--probe", action="store_true", help="只检查 Chrome/页面状态，不抓取")
    p_wr.set_defaults(func=cmd_weread)

    p_cw = sub.add_parser("crawl", help="全量抓取一个公众号（可中断、可续跑、按天限频）")
    p_cw.add_argument("--book-id", help="公众号 bookId（形如 MP_WXS_1234567890）")
    p_cw.add_argument("--from-url", help="给该号一篇文章链接，自动解析 bookId（推荐）")
    p_cw.add_argument("--from-name", help="给公众号名字，自动搜索并解析 bookId（需 auth-key）")
    p_cw.add_argument("--account", help="公众号名（存知识库用；--from-url 时会自动识别）")
    p_cw.add_argument("--kb", help="知识库根目录")
    p_cw.add_argument("--pages", type=int, help="本次运行最多翻几页")
    p_cw.add_argument("--interval", type=float, help="每页之间的间隔秒数（默认 20）")
    p_cw.add_argument("--per-day", type=int, help="每天最多发几个列表请求（默认 30）")
    p_cw.add_argument("--concurrency", type=int, default=2, help="并发抓正文数（默认 2）")
    p_cw.add_argument("--sleep", type=float, default=1.5, help="每篇正文之间的间隔秒数")
    p_cw.add_argument("--no-images", action="store_true", help="不下载图片")
    p_cw.add_argument("--reset", action="store_true", help="丢弃断点，从头开始（慎用）")
    p_cw.set_defaults(func=cmd_crawl)

    p_bf = sub.add_parser("backfill", help="补齐「列表见过但正文没落盘」的文章")
    p_bf.add_argument("--account", required=True, help="公众号名（知识库子目录名）")
    p_bf.add_argument("--kb", help="知识库根目录")
    p_bf.add_argument("--limit", type=int, help="本次最多补几篇")
    p_bf.add_argument("--concurrency", type=int, default=2, help="并发数（默认 2）")
    p_bf.add_argument("--sleep", type=float, default=1.5, help="每篇间隔秒数")
    p_bf.add_argument("--no-images", action="store_true", help="不下载图片")
    p_bf.set_defaults(func=cmd_backfill)

    p_pace = sub.add_parser("pace", help="查看抓取节奏（今天发了多少请求、有没有被风控）")
    p_pace.set_defaults(func=cmd_pace)

    p_rb = sub.add_parser("rebuild", help="从磁盘上的 Markdown 重建索引（中断后用）")
    p_rb.add_argument("--kb", help="知识库根目录")
    p_rb.add_argument("--account", help="只重建指定公众号（默认扫全部）")
    p_rb.set_defaults(func=cmd_rebuild)

    p_cfg = sub.add_parser("config", help="读写配置")
    p_cfg.add_argument("--set-auth-key", help="设置 auth-key")
    p_cfg.add_argument("--set-kb", help="设置知识库根目录")
    p_cfg.add_argument("--show", action="store_true", help="显示当前配置")
    p_cfg.set_defaults(func=cmd_config)

    p_chk = sub.add_parser("login-check", help="检查 auth-key 是否有效")
    p_chk.set_defaults(func=cmd_login_check)

    p_per = sub.add_parser("periods", help="列出可用期限")
    p_per.set_defaults(func=cmd_periods)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        warn("已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
