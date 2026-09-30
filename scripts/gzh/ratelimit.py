"""全局限流器 —— 进程间共享，任何出站请求都必须先过它。

## 为什么需要「进程间共享」

原来的限流只做在 `crawl()` 的翻页循环里：`--interval` 控制每页间隔。
**但今天两次风控都不是翻页造成的，而是循环之外的临时调用：**

| 事故 | 触发方式 | 结果 |
|---|---|---|
| ① | 手写脚本连测 offset=0/200/400/800…，间隔 3 秒 | 7 个请求 / 18 秒 → `-2041`，25 分钟才恢复 |
| ② | 连续测多个号的可用性 | 全站 `-2012`，20 分钟自愈 |

这两次都是**独立进程**发起的 —— 进程内的计时器对它们完全无效。

所以要一个**写在磁盘上的账本**：不管谁发请求、从哪个脚本发，
都先读它、改成它、再发。这样「临时测一下」也绕不过去。

## 用法

    from .ratelimit import acquire

    acquire()                 # 必要时自动 sleep，然后记账
    page = fetch_page(...)    # 记账之后才发请求

`acquire()` 会在需要等待时打印剩余秒数，不会静默卡住。

## 账本

默认 `~/.config/gzh/ratelimit.json`：

    {
      "events": [
        {"t": 1790675521.5, "kind": "list", "label": "offset=0"},
        ...
      ]
    }

只保留最近 48 小时的事件，避免无限增长。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LEDGER = Path.home() / ".config" / "gzh" / "ratelimit.json"

# ---------------------------------------------------------------- 安全频率
#
# 这些数字来自今天的**实测事故**，不是拍脑袋：
#
#   · 25 秒间隔 / 25 页连续翻页  → 0 次风控（成功抓完 501 篇）
#   · 3 秒间隔 / 7 个请求       → 立即 -2041
#   · 无间隔连测多个号          → 全站 -2012
#
# 工具的原始作者也观察到「一天 30 多个请求触发过反爬」。
# 所以下面取的是**比已知安全值更保守**的一档。

SAFE_MIN_INTERVAL = 25.0      # 任意两个列表请求之间的最小间隔（秒）
SAFE_PER_HOUR = 40            # 每小时上限（25 秒间隔理论值 144，取更保守的）
SAFE_PER_DAY = 200            # 每天上限（25 秒连续 2 小时 ≈ 288，取更保守的）
COOLDOWN_AFTER_ERROR = 900    # 命中风控后强制静默（秒）


@dataclass
class Decision:
    wait: float          # 需要先等多少秒
    reason: str          # 为什么要等（空 = 不用等）
    allowed: bool        # 是否允许发送


def _load(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
        events = data.get("events") or []
        return [e for e in events if isinstance(e, dict) and "t" in e]
    except (OSError, json.JSONDecodeError):
        return []


def _save(ledger: Path, events: list[dict]) -> None:
    """只留最近 48 小时；原子写入，被 kill 不会留半个文件。"""
    cutoff = time.time() - 48 * 3600
    events = [e for e in events if float(e.get("t", 0)) >= cutoff]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger.with_suffix(".tmp")
    payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "events": events[-2000:]}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ledger)


def check(ledger: Path | None = None, min_interval: float = SAFE_MIN_INTERVAL,
          per_hour: int = SAFE_PER_HOUR, per_day: int = SAFE_PER_DAY) -> Decision:
    """判断「现在能不能发请求」，不记账、不等待。"""
    ledger = Path(ledger).expanduser() if ledger else DEFAULT_LEDGER
    now = time.time()
    events = _load(ledger)

    # ① 风控后的强制静默：最近一条是 error 事件
    #
    # ⚠️ 这里也必须是 allowed=False。理由和下面的小时/日上限相同：
    #    静默期长达 15 分钟，如果只是「等一下」，acquire() 会闷头睡过去，
    #    用户看到的就是「程序卡死了」。
    #    正确做法：明确拒绝 + 告知还要等多久，由调用方决定是等还是收工。
    for e in reversed(events):
        if e.get("kind") == "error":
            elapsed = now - float(e["t"])
            if elapsed < COOLDOWN_AFTER_ERROR:
                left = COOLDOWN_AFTER_ERROR - elapsed
                return Decision(
                    left,
                    f"上次命中风控，强制静默中，还需 {left / 60:.0f} 分钟",
                    False,
                )
            break
        if e.get("kind") == "list":
            break

    # ② 最短间隔
    last = next((float(e["t"]) for e in reversed(events) if e.get("kind") == "list"), None)
    if last is not None:
        elapsed = now - last
        if elapsed < min_interval:
            return Decision(min_interval - elapsed, f"距上次请求仅 {elapsed:.0f} 秒", True)

    # ③ 每小时 / 每天上限
    #
    # ⚠️ 这两条必须是 allowed=False，不能只是「等一会儿」。
    #    踩过的坑：早期小时上限返回 allowed=True + wait=3160 秒，
    #    于是 acquire() 会真的闷头等 53 分钟 —— 用户看到的就是「卡住不动」。
    #    正确语义：**这一轮到此为止**，告诉用户什么时候重置，让他自己决定。
    #    （只有短间隔那种 wait 才是「等一下就好」，所以只有那种 allowed=True。）
    hour = [e for e in events if e.get("kind") == "list" and now - float(e["t"]) < 3600]
    if len(hour) >= per_hour:
        oldest = min(float(e["t"]) for e in hour)
        wait = max(0.0, 3600 - (now - oldest))
        return Decision(
            wait,
            f"本小时已发 {len(hour)} 个请求（上限 {per_hour}），约 {wait / 60:.0f} 分钟后重置",
            False,
        )

    day = [e for e in events if e.get("kind") == "list" and now - float(e["t"]) < 86400]
    if len(day) >= per_day:
        oldest = min(float(e["t"]) for e in day)
        wait = max(0.0, 86400 - (now - oldest))
        return Decision(
            wait,
            f"24 小时内已发 {len(day)} 个请求（上限 {per_day}），约 {wait / 3600:.1f} 小时后重置",
            False,
        )

    return Decision(0.0, "", True)


def acquire(kind: str = "list", label: str = "", ledger: Path | None = None,
            min_interval: float = SAFE_MIN_INTERVAL, per_hour: int = SAFE_PER_HOUR,
            per_day: int = SAFE_PER_DAY, max_wait: float | None = None,
            verbose: bool = True) -> Decision:
    """取得发送许可。必要时 sleep，然后记账。

    `max_wait` 不为 None 时，需要等待超过这个秒数就**放弃**（不 sleep），
    返回 allowed=False —— 给「不想被卡住」的场景用。

    ⚠️ 记账发生在**等待之后、真正发送之前**，所以即使发送失败，
    这次请求也计入额度。宁可少发，不可多发。
    """
    ledger = Path(ledger).expanduser() if ledger else DEFAULT_LEDGER
    log = (lambda m: print(m, flush=True)) if verbose else (lambda m: None)

    while True:
        d = check(ledger, min_interval, per_hour, per_day)
        if not d.allowed:
            log(f"  ⛔ {d.reason} —— 已达上限，不再等待")
            return d
        if d.wait <= 0:
            break
        if max_wait is not None and d.wait > max_wait:
            log(f"  ⏸  {d.reason}，需等 {d.wait:.0f} 秒（超过上限 {max_wait:.0f} 秒，放弃）")
            return Decision(d.wait, d.reason, False)
        log(f"  ⏳ {d.reason}，等 {d.wait:.0f} 秒…")
        time.sleep(min(d.wait, 30))
        if max_wait is not None:
            max_wait -= min(d.wait, 30)

    events = _load(ledger)
    events.append({"t": time.time(), "kind": kind, "label": label})
    _save(ledger, events)
    return Decision(0.0, "", True)


def note_error(label: str = "", ledger: Path | None = None) -> None:
    """记一次风控事件 —— 之后的请求会被强制静默一段时间。"""
    ledger = Path(ledger).expanduser() if ledger else DEFAULT_LEDGER
    events = _load(ledger)
    events.append({"t": time.time(), "kind": "error", "label": label})
    _save(ledger, events)


def stats(ledger: Path | None = None) -> dict:
    """给用户看的当前节奏统计。"""
    ledger = Path(ledger).expanduser() if ledger else DEFAULT_LEDGER
    now = time.time()
    events = _load(ledger)
    lists = [e for e in events if e.get("kind") == "list"]
    errs = [e for e in events if e.get("kind") == "error"]
    last = max((float(e["t"]) for e in lists), default=None)
    return {
        "ledger": str(ledger),
        "total_recorded": len(lists),
        "last_hour": len([e for e in lists if now - float(e["t"]) < 3600]),
        "last_day": len([e for e in lists if now - float(e["t"]) < 86400]),
        "seconds_since_last": (now - last) if last else None,
        "errors_24h": len([e for e in errs if now - float(e["t"]) < 86400]),
        "limits": {"min_interval": SAFE_MIN_INTERVAL, "per_hour": SAFE_PER_HOUR,
                   "per_day": SAFE_PER_DAY},
    }
