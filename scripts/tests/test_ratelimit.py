"""限流器的离线测试 —— 不依赖网络。

## 为什么要有这些测试

限流器的 bug 表现是「**静默卡住**」或「**该拦不拦**」，
两种都很难在现场发现：

- 早期小时上限返回 `allowed=True + wait=3160`，
  于是 `acquire()` 真的闷头等 53 分钟 —— 用户看到的是「程序不动了」。
- 反过来，如果该拦的时候放行，就是直接去撞微信的风控。

所以这里把三种决策语义钉死：

  · 短间隔      → allowed=True，等一下就好（正常限速）
  · 命中风控后  → allowed=False，强制静默
  · 小时/日超限 → allowed=False，**且要说清什么时候重置**
                  （不能是「等 53 分钟」那种傻等）
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gzh import ratelimit as rl  # noqa: E402


def ledger_with(n_requests: int, gap_sec: float = 10.0) -> Path:
    """造一个账本：最近有 n 个请求，间隔 gap_sec 秒。"""
    tmp = Path(tempfile.mkdtemp()) / "rl.json"
    now = time.time()
    events = [
        {"t": now - i * gap_sec, "kind": "list", "label": f"#{i}"}
        for i in range(n_requests)
    ]
    tmp.write_text(json.dumps({"events": events}), encoding="utf-8")
    return tmp


class TestDecisionSemantics(unittest.TestCase):
    """三种决策的语义必须分清。"""

    def test_empty_ledger_allows_immediately(self):
        tmp = Path(tempfile.mkdtemp()) / "rl.json"
        d = rl.check(tmp)
        self.assertTrue(d.allowed)
        self.assertEqual(d.wait, 0.0)

    def test_recent_request_waits_but_is_allowed(self):
        """刚发过请求 → 需要等，但这只是限速，不是拒绝。"""
        d = rl.check(ledger_with(1, gap_sec=1))
        self.assertTrue(d.allowed, "短间隔只需等待，不该判为拒绝")
        self.assertGreater(d.wait, 0)
        self.assertLessEqual(d.wait, rl.SAFE_MIN_INTERVAL)

    def test_hourly_cap_is_a_hard_stop(self):
        """小时超限 → 必须拒绝，且不能让人干等一小时。"""
        d = rl.check(ledger_with(rl.SAFE_PER_HOUR + 5, gap_sec=10))
        self.assertFalse(d.allowed,
                         "小时超限必须是拒绝 —— 否则 acquire() 会闷头等几十分钟")
        self.assertIn("重置", d.reason, "拒绝时必须告知什么时候重置")

    def test_daily_cap_is_a_hard_stop(self):
        """24 小时超限 → 同样拒绝。"""
        tmp = Path(tempfile.mkdtemp()) / "rl.json"
        now = time.time()
        events = [{"t": now - i * 400, "kind": "list", "label": ""}
                  for i in range(rl.SAFE_PER_DAY + 5)]
        tmp.write_text(json.dumps({"events": events}), encoding="utf-8")

        d = rl.check(tmp)
        self.assertFalse(d.allowed)
        self.assertIn("重置", d.reason)

    def test_error_forces_cooldown(self):
        """命中风控后 → 强制静默，且不允许发。"""
        tmp = Path(tempfile.mkdtemp()) / "rl.json"
        rl.note_error("test", ledger=tmp)
        d = rl.check(tmp)
        self.assertFalse(d.allowed)
        self.assertGreater(d.wait, rl.COOLDOWN_AFTER_ERROR - 60)


class TestRemainingQuota(unittest.TestCase):
    """余量不能是负数 —— 显示用。"""

    def test_remaining_never_negative(self):
        tmp = ledger_with(rl.SAFE_PER_HOUR + 20, gap_sec=5)
        s = rl.stats(tmp)
        left_hour = max(0, rl.SAFE_PER_HOUR - s["last_hour"])
        left_day = max(0, rl.SAFE_PER_DAY - s["last_day"])
        self.assertGreaterEqual(left_hour, 0)
        self.assertGreaterEqual(left_day, 0)

    def test_stats_counts_only_list_events(self):
        """风控事件不该被算成「发了一个请求」。"""
        tmp = Path(tempfile.mkdtemp()) / "rl.json"
        now = time.time()
        events = [
            {"t": now, "kind": "list", "label": "a"},
            {"t": now, "kind": "error", "label": "b"},
        ]
        tmp.write_text(json.dumps({"events": events}), encoding="utf-8")

        s = rl.stats(tmp)
        self.assertEqual(s["last_hour"], 1, "只有 list 事件算请求数")
        self.assertEqual(s["errors_24h"], 1)


class TestAcquire(unittest.TestCase):
    """acquire() 的行为。"""

    def test_blocked_does_not_sleep(self):
        """被拒绝时不该 sleep —— 应该立刻返回，让调用方知道。"""
        tmp = ledger_with(rl.SAFE_PER_HOUR + 5, gap_sec=10)
        t0 = time.time()
        d = rl.acquire(kind="list", ledger=tmp, verbose=False)
        elapsed = time.time() - t0

        self.assertFalse(d.allowed)
        self.assertLess(elapsed, 2, "被拒绝时必须立刻返回，不能真的睡下去")

    def test_acquire_records_event(self):
        tmp = Path(tempfile.mkdtemp()) / "rl.json"
        rl.acquire(kind="list", label="测试", ledger=tmp, verbose=False)
        s = rl.stats(tmp)
        self.assertEqual(s["total_recorded"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
