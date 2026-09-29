"""微信读书全量抓取器 —— 可中断、可续跑、按天限频。

## 设计目标

用户的原话：「宁可摘取的时间长一点」也要**全量**。

所以这个模块的取向和 `weread.py` 不同：

| | `weread.py`（快抓） | 本模块（全量） |
|---|---|---|
| 目标 | 尽快抓最近 N 页 | 从最新一路翻到**最早** |
| 中断 | 从头再来 | **断点续跑** |
| 限频 | 单次运行内 | **跨天预算** |
| 节奏 | 3 秒/请求 | 可调到 30-60 秒/请求 |

## 为什么必须能续跑

全量可能几十页。若每次中断都从头翻：
- 浪费时间（前面的页重复抓）
- 浪费额度（额度是每天最稀缺的资源）
- 永远抓不完（每天都被同一个断点卡住）

## 断点状态

存在 `<kb>/.weread-crawl.json`：

    {
      "bookId": "MP_WXS_xxx",
      "account": "某公众号",
      "next_offset": 120,          # 下次从这里继续
      "pages_done": 6,
      "seen": {"<url>": {...}},     # 累计拿到的文章（去重）
      "oldest_ts": 1234567890,      # 已翻到的最早时间
      "finished": false,            # 翻到尽头了吗
      "days": {"2026-09-29": {"requests": 12}},   # 按天记账
      "history": []                 # 每次运行的摘要
    }

## 什么时候算「翻到尽头」

三种信号，任一成立即停：
1. 接口返回 `reviews: 0`（该 offset 之后没有群发了）
2. 返回的 `oldest` 不再变小（说明在原地打转）
3. 连续两页拿到的 URL 全都在 `seen` 里（去重后零新增）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    # 每个请求之间等多久（秒）。原作者一天 30+ 请求就触发过反爬，
    # 这里取「宁可慢」的取向：默认 20 秒。
    "request_interval_sec": 20,
    # 每天最多发多少个「列表」请求。默认 30 —— 低于作者的 40，
    # 因为他是「选定的保守值」，我们比他再保守一档。
    "requests_per_day": 30,
    # 单次运行最多翻几页（一次运行里连续跑，之后等下次）
    "pages_per_run": 5,
    # 遇到 -2041（风控）时等多久再重试（秒）
    "cooldown_on_2041_sec": 900,
    # 遇到风控时，单次运行最多等几轮
    "max_cooldowns_per_run": 2,
    # 翻到尽头后，是否再往前探一页确认（防止提前停）
    "confirm_tail": True,
}


@dataclass
class CrawlState:
    """断点状态。"""

    bookId: str
    account: str
    next_offset: int = 0
    pages_done: int = 0
    seen: dict = field(default_factory=dict)      # url -> item
    oldest_ts: int = 0
    newest_ts: int = 0
    finished: bool = False
    days: dict = field(default_factory=dict)      # date -> {requests: n}
    history: list = field(default_factory=list)

    # -------------------------------------------------- 持久化

    @staticmethod
    def load(path: Path) -> "CrawlState | None":
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return CrawlState(
            bookId=d.get("bookId", ""),
            account=d.get("account", ""),
            next_offset=int(d.get("next_offset", 0)),
            pages_done=int(d.get("pages_done", 0)),
            seen=d.get("seen") or {},
            oldest_ts=int(d.get("oldest_ts", 0)),
            newest_ts=int(d.get("newest_ts", 0)),
            finished=bool(d.get("finished")),
            days=d.get("days") or {},
            history=d.get("history") or [],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bookId": self.bookId,
            "account": self.account,
            "next_offset": self.next_offset,
            "pages_done": self.pages_done,
            "oldest_ts": self.oldest_ts,
            "newest_ts": self.newest_ts,
            "finished": self.finished,
            "days": self.days,
            "history": self.history[-30:],
            "seen": self.seen,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)   # 原子替换：被 kill 时不会留下半个文件

    # -------------------------------------------------- 记账

    def used_today(self, today: str | None = None) -> int:
        today = today or time.strftime("%Y-%m-%d")
        return int((self.days.get(today) or {}).get("requests", 0))

    def charge(self, n: int = 1, today: str | None = None) -> None:
        today = today or time.strftime("%Y-%m-%d")
        slot = self.days.setdefault(today, {"requests": 0})
        slot["requests"] = int(slot.get("requests", 0)) + n


class CrawlError(RuntimeError):
    pass


class RateLimited(CrawlError):
    """命中风控（-2041）。"""


class LoginExpired(CrawlError):
    """登录失效（-2010）。"""


# ---------------------------------------------------------------- 取一页

TOOL_PAGE = Path(__file__).resolve().parent.parent / "tools" / "weread_page.mjs"


def fetch_page(book_id: str, offset: int, timeout: int = 60,
               respect_ratelimit: bool = True) -> dict:
    """取某一页。返回 {items, reviews, oldest, ...}。

    ## 所有取页请求都必须经过这里

    这是**唯一的取页出口** —— 限流也钉在这一层，所以不管谁调用
    （正式抓取、临时测试、将来新写的脚本）都会先过闸，绕不过去。

    之前两次风控就是因为循环之外的临时调用没有闸：
    手写脚本连测 7 个 offset（间隔 3 秒）直接触发 -2041。

    任何失败都抛 CrawlError 的子类，调用方决定怎么处理。
    """
    if not TOOL_PAGE.exists():
        raise CrawlError(f"找不到取页脚本：{TOOL_PAGE}")

    # ---- 限流闸：进程间共享的账本，跨脚本也生效
    if respect_ratelimit:
        from .ratelimit import acquire, note_error

        d = acquire(kind="list", label=f"{book_id}@{offset}")
        if not d.allowed:
            raise CrawlError(f"被限流器拦下：{d.reason}")

    try:
        proc = subprocess.run(
            ["node", str(TOOL_PAGE), book_id, str(offset)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CrawlError(f"取页超时（offset={offset}）") from exc

    line = (proc.stdout or "").strip().splitlines()
    if not line:
        raise CrawlError(f"取页无输出（offset={offset}）：{(proc.stderr or '')[:200]}")

    try:
        d = json.loads(line[-1])
    except json.JSONDecodeError as exc:
        raise CrawlError(f"取页输出无法解析：{line[-1][:200]}") from exc

    if d.get("ok"):
        return d

    err = str(d.get("error") or "")
    hint = str(d.get("hint") or "")

    # 命中风控就记账 —— 后续请求会被强制静默一段时间，
    # 免得「失败后立刻重试」把封禁时间越拖越长。
    if any(code in err for code in ("-2041", "-2012")):
        if respect_ratelimit:
            from .ratelimit import note_error
            note_error(f"{book_id}@{offset}: {err[:60]}")

    if "-2041" in err:
        raise RateLimited(hint or "触发风控")
    if "-2012" in err:
        raise RateLimited(hint or "短时风控")
    if "-2010" in err:
        raise LoginExpired(hint or "登录失效")
    raise CrawlError(f"{err} {hint}".strip())


# ---------------------------------------------------------------- 增量导出

def export_new_items(state: CrawlState, kb_root: str, items: list[dict],
                     concurrency: int = 2, no_images: bool = False,
                     sleep: float = 1.5) -> tuple[int, int, list[dict]]:
    """把新文章写进知识库。返回 (成功数, 失败数, 失败明细)。

    ## 两条独立的状态

    - `seen`（断点文件里）：**列表层面见过** —— 用于「翻页去重」，判断是否到底
    - `existing`（知识库索引里）：**正文已落盘** —— 用于「决定要不要抓正文」

    两者刻意分开：`seen` 记的是「这个 URL 出现在列表里了」，
    `existing` 记的是「这篇正文已经存下来了」。

    ⚠️ 踩过的坑：早期版本把「记 seen」写在「抓正文」之前，且用 seen 判断是否跳过。
    结果中途被杀时，文章进了 seen 但没落盘，续跑又从更晚的 offset 开始 ——
    这批文章**永远不会再被回头抓**，静默丢失 28 篇。

    正确做法：seen 可以早写（它是列表事实），但**是否要抓正文只看 existing**。
    这样即使中断，下次扫到同一页仍会把缺失的正文补回来。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from gzh.exporter import KnowledgeBase, rebuild_index_from_files, write_manifest
    from gzh.fetcher import fetch_article

    kb = KnowledgeBase(kb_root, account=state.account)

    # seen 早写是安全的：它是「列表事实」，不影响是否抓正文
    for it in items:
        if it.get("url") and it["url"] not in state.seen:
            state.seen[it["url"]] = it

    # 是否抓正文，只由「知识库索引里有没有」决定
    existing = kb.load_index()
    todo = [it for it in items if it.get("url") and it["url"] not in existing]

    ok_count = 0
    failures: list[dict] = []

    if todo:
        def work(it: dict):
            res = fetch_article(it["url"], timeout=30)
            if it.get("t"):
                res.meta.publish_ts = int(it["t"])
            if it.get("title"):
                res.meta.title = it["title"]
            if not res.meta.account:
                res.meta.account = state.account
            art = kb.export_article(
                res.meta, res.content_html, res.images,
                download_image_files=not no_images,
            )
            art.channel = f"weread+{res.channel}"
            return art

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(work, it): it for it in todo}
            for fut in as_completed(futures):
                it = futures[fut]
                try:
                    fut.result()
                    ok_count += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append({"url": it["url"], "title": it.get("title", ""),
                                     "error": str(exc)[:200]})
                if sleep:
                    time.sleep(sleep / max(1, concurrency))

    # 每页之后都重写索引 —— 这样任何时刻中断，索引都是最新的
    from gzh.exporter import rebuild_index_from_files
    arts = rebuild_index_from_files(kb)
    if arts:
        kb.save_index(arts)
        kb.write_index_md(arts, "全量抓取（微信读书）")
        write_manifest(kb, arts, "全量抓取", extra={
            "source": "weread-full",
            "pages_done": state.pages_done,
            "next_offset": state.next_offset,
            "finished": state.finished,
            "failures": failures[:50],
        })

    return ok_count, len(failures), failures


# ---------------------------------------------------------------- 补齐

def backfill_missing(account: str, kb_root: str, concurrency: int = 2,
                     no_images: bool = False, sleep: float = 1.5,
                     limit: int | None = None, verbose: bool = True) -> dict:
    """把「列表里见过、但正文没落盘」的文章补回来。

    ## 为什么需要这个

    断点文件里的 `seen` 记录的是「这个 URL 在列表里出现过」，
    而知识库里只有「正文已成功抓下」的那些。

    如果曾在导出途中中断（限流、Ctrl-C、进程被杀），就会出现
    `seen` 有、知识库没有的文章。由于续跑是从**更晚的 offset** 继续，
    这些文章再也不会被回头扫到 —— 必须显式补齐。

    这个函数只读断点文件的 URL 清单，**不再请求列表接口**，
    所以不消耗微信读书的额度，只受单篇抓取（mp.weixin.qq.com）影响。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from gzh.exporter import KnowledgeBase, rebuild_index_from_files, write_manifest
    from gzh.fetcher import fetch_article

    log = (lambda m: print(m, flush=True)) if verbose else (lambda m: None)

    root = Path(kb_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()

    kb = KnowledgeBase(str(root), account=account)
    state = CrawlState.load(kb.dir / ".weread-crawl.json")
    if state is None:
        raise CrawlError(f"找不到断点文件：{kb.dir / '.weread-crawl.json'}")

    existing = kb.load_index()
    missing = [v for k, v in state.seen.items() if k not in existing]
    missing.sort(key=lambda x: x.get("t", 0), reverse=True)
    if limit:
        missing = missing[:limit]

    result = {
        "account": account,
        "missing": len(missing),
        "exported": 0,
        "failed": 0,
        "failures": [],
    }
    if not missing:
        log("✅ 没有缺失的文章 —— 列表和知识库一致")
        return result

    log(f"→ 补齐【{account}】")
    log(f"  列表里见过 {len(state.seen)} 篇，知识库 {len(existing)} 篇，缺 {len(missing)} 篇")
    log("")

    failures: list[dict] = []
    ok_count = 0

    def work(it: dict):
        res = fetch_article(it["url"], timeout=30)
        if it.get("t"):
            res.meta.publish_ts = int(it["t"])
        if it.get("title"):
            res.meta.title = it["title"]
        if not res.meta.account:
            res.meta.account = account
        art = kb.export_article(res.meta, res.content_html, res.images,
                                download_image_files=not no_images)
        art.channel = f"weread-backfill+{res.channel}"
        return art

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(work, it): it for it in missing}
        done = 0
        for fut in as_completed(futures):
            it = futures[fut]
            done += 1
            try:
                fut.result()
                ok_count += 1
                log(f"  [{done}/{len(missing)}] ✅ {it.get('title', '')[:44]}")
            except Exception as exc:  # noqa: BLE001
                failures.append({"url": it["url"], "title": it.get("title", ""),
                                 "error": str(exc)[:200]})
                log(f"  [{done}/{len(missing)}] ❌ {it.get('title', '')[:44]} — {str(exc)[:50]}")
            if sleep:
                time.sleep(sleep / max(1, concurrency))

    arts = rebuild_index_from_files(kb)
    if arts:
        kb.save_index(arts)
        kb.write_index_md(arts, "全量抓取（微信读书）")
        write_manifest(kb, arts, "全量抓取", extra={
            "source": "weread-backfill",
            "failures": failures[:50],
        })

    result["exported"] = ok_count
    result["failed"] = len(failures)
    result["failures"] = failures
    result["total_in_kb"] = len(arts)

    log("")
    if failures:
        log(f"⚠️  补齐完成：成功 {ok_count}，失败 {len(failures)}")
    else:
        log(f"✅ 补齐完成：{ok_count} 篇全部成功")
    return result


# ---------------------------------------------------------------- 主循环

def crawl(book_id: str, account: str, kb_root: str, config: dict | None = None,
          reset: bool = False, verbose: bool = True) -> dict:
    """全量抓取一个公众号。

    可反复调用：每次从上一次停下的 offset 继续，直到 `finished`。
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    root = Path(kb_root).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    account_dir = root / account
    account_dir.mkdir(parents=True, exist_ok=True)
    state_path = account_dir / ".weread-crawl.json"

    state = None if reset else CrawlState.load(state_path)
    if state is None:
        state = CrawlState(bookId=book_id, account=account)
    state.bookId = book_id
    state.account = account

    today = time.strftime("%Y-%m-%d")
    budget = int(cfg["requests_per_day"])
    per_run = int(cfg["pages_per_run"])
    interval = float(cfg["request_interval_sec"])

    result = {
        "account": account,
        "start_offset": state.next_offset,
        "pages_this_run": 0,
        "new_articles": 0,
        "exported": 0,
        "failed": 0,
        "finished": state.finished,
        "total_seen": len(state.seen),
        "stopped_reason": "",
        "oldest": "",
        "failures": [],
    }

    if state.finished:
        result["stopped_reason"] = "已抓完（上次运行翻到了尽头）"
        return result

    log = (lambda m: print(m, flush=True)) if verbose else (lambda m: None)

    log(f"→ 全量抓取【{account}】")
    log(f"   bookId      {book_id}")
    log(f"  从 offset   {state.next_offset} 继续（已完成 {state.pages_done} 页）")
    log(f"  累计文章    {len(state.seen)} 篇")
    log(f"  今日额度    {state.used_today(today)}/{budget} 请求")
    log(f"  本次计划    {per_run} 页，间隔 {interval:.0f} 秒")
    log("")

    consecutive_empty = 0
    cooldowns = 0
    prev_oldest = state.oldest_ts

    for page_i in range(per_run):
        # ---- 额度检查
        if state.used_today(today) >= budget:
            result["stopped_reason"] = f"今日额度用尽（{budget} 请求）"
            break

        # ---- 限频：请求之间等待
        #
        # 两道闸取「更严的那个」，但**不叠加等待**：
        #   ① 本命令的 --interval（用户显式指定）
        #   ② 全局限流器的最小间隔（默认 25 秒，跨脚本共享）
        # 若各等一遍，同一份等待会被算两次，白白变慢。
        # 做法：算出「还要等多久才够 max(interval, 全局最小间隔)」，只睡这个差额。
        # 这样睡完再进 fetch_page 时，限流器会发现已经不欠了，不会再等。
        if page_i > 0 or state.pages_done > 0:
            from .ratelimit import SAFE_MIN_INTERVAL, check as rl_check

            target = max(interval, SAFE_MIN_INTERVAL)
            d = rl_check()
            # d.wait = 距上次请求还差多久才满全局最小间隔
            # 我们想距上次满 target 秒，所以还需等 target - (SAFE_MIN_INTERVAL - d.wait)
            remaining = max(0.0, target - (SAFE_MIN_INTERVAL - d.wait)) if d.wait > 0 else target
            if remaining > 0:
                log(f"  等待 {remaining:.0f} 秒…")
                time.sleep(remaining)

        offset = state.next_offset
        log(f"  [{page_i + 1}/{per_run}] offset={offset} …", )

        # ---- 取页（含风控重试）
        while True:
            try:
                state.charge(1, today)
                page = fetch_page(book_id, offset)
                break
            except RateLimited as exc:
                cooldowns += 1
                if cooldowns > int(cfg["max_cooldowns_per_run"]):
                    result["stopped_reason"] = f"连续命中风控，本次放弃：{exc}"
                    state.save(state_path)
                    return _finish(result, state, kb_root, cfg)
                wait = int(cfg["cooldown_on_2041_sec"])
                log(f"      ⚠️ 触发风控，等 {wait // 60} 分钟再试（第 {cooldowns} 次）")
                state.save(state_path)
                time.sleep(wait)
                continue
            except LoginExpired as exc:
                result["stopped_reason"] = f"登录失效：{exc}"
                state.save(state_path)
                return _finish(result, state, kb_root, cfg)
            except CrawlError as exc:
                result["stopped_reason"] = f"取页失败：{exc}"
                state.save(state_path)
                return _finish(result, state, kb_root, cfg)

        items = page.get("items") or []
        reviews = int(page.get("reviews") or 0)
        oldest = int(page.get("oldest") or 0)

        log(f"           群发={reviews} 篇数={len(items)} "
            f"最早={time.strftime('%Y-%m-%d', time.localtime(oldest)) if oldest else '—'}")

        # ---- 终止判据 1：该 offset 之后没有群发了
        if reviews == 0:
            if cfg.get("confirm_tail") and not state.finished:
                # 再往前探一页确认（防止微信读书分页偶发返回空）
                log("           返回 0 群发 —— 再探一页确认是否真的到底")
                state.finished = True
                state.save(state_path)
                continue
            state.finished = True
            result["stopped_reason"] = "翻到尽头（reviews=0）"
            break

        # ---- 终止判据 2：oldest 不再变小（原地打转）
        if prev_oldest and oldest and oldest >= prev_oldest:
            log("           最早时间没有继续变小 —— 可能已到底，停止")
            state.finished = True
            result["stopped_reason"] = "翻到尽头（最早时间不再前移）"
            break

        # ---- 进度
        state.pages_done += 1
        state.next_offset += reviews
        if oldest:
            state.oldest_ts = oldest if not state.oldest_ts else min(state.oldest_ts, oldest)
        ts = [i["t"] for i in items if i.get("t")]
        if ts:
            state.newest_ts = max(state.newest_ts, max(ts))
        prev_oldest = oldest or prev_oldest
        result["pages_this_run"] += 1

        # ---- 导出这一页的新文章
        before = len(state.seen)
        ok_n, fail_n, fails = export_new_items(
            state, kb_root, items,
            concurrency=int(cfg.get("concurrency", 2)),
            no_images=bool(cfg.get("no_images")),
            sleep=float(cfg.get("export_sleep", 1.5)),
        )
        newly = len(state.seen) - before
        result["new_articles"] += newly
        result["exported"] += ok_n
        result["failed"] += fail_n
        result["failures"].extend(fails)
        log(f"           新增 {newly} 篇；导出 {ok_n} 成功 / {fail_n} 失败")

        # ---- 终止判据 3：整页都是旧的（去重后零新增）
        if newly == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                state.finished = True
                result["stopped_reason"] = "翻到尽头（连续两页无新文章）"
                break
        else:
            consecutive_empty = 0

        state.save(state_path)

    if not result["stopped_reason"]:
        result["stopped_reason"] = "本次运行页数用完，下次继续"

    state.save(state_path)
    return _finish(result, state, kb_root, cfg)


def _finish(result: dict, state: CrawlState, kb_root: str, cfg: dict) -> dict:
    """补全收尾统计。"""
    result["finished"] = state.finished
    result["next_offset"] = state.next_offset
    result["total_seen"] = len(state.seen)
    result["pages_done"] = state.pages_done
    if state.oldest_ts:
        result["oldest"] = time.strftime("%Y-%m-%d", time.localtime(state.oldest_ts))
    if state.newest_ts:
        result["newest"] = time.strftime("%Y-%m-%d", time.localtime(state.newest_ts))
    return result
