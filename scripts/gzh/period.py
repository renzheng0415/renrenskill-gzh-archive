"""期限筛选：1 个月 / 3 个月 / 半年 / 1 年 / 全部历史。"""

from __future__ import annotations

import time
from dataclasses import dataclass

# 期限选项：中文名 → 天数（None = 不限制）
PERIOD_DAYS: dict[str, int | None] = {
    "1m": 30,
    "3m": 90,
    "6m": 182,
    "1y": 365,
    "all": None,
}

PERIOD_LABELS: dict[str, str] = {
    "1m": "1 个月以内",
    "3m": "3 个月以内",
    "6m": "半年以内",
    "1y": "1 年以内",
    "all": "全部历史文章",
}

# 用户可能输入的各种说法 → 标准 key
_ALIASES: dict[str, str] = {
    "1m": "1m", "1": "1m", "1月": "1m", "一个月": "1m", "1个月": "1m", "1个月以内": "1m",
    "30d": "1m", "30": "1m",
    "3m": "3m", "3": "3m", "3月": "3m", "三个月": "3m", "3个月": "3m", "3个月以内": "3m",
    "90d": "3m", "90": "3m",
    "6m": "6m", "6": "6m", "半年": "6m", "半年以内": "6m", "6个月": "6m", "180d": "6m",
    "1y": "1y", "y": "1y", "1年": "1y", "一年": "1y", "1年以内": "1y", "12个月": "1y",
    "365d": "1y",
    "all": "all", "全部": "all", "全部历史": "all", "全部历史文章": "all",
    "所有": "all", "历史": "all", "0": "all",
}


class PeriodError(ValueError):
    """期限参数无法识别。"""


@dataclass
class Period:
    """一个已解析的期限条件。"""

    key: str
    days: int | None

    @property
    def label(self) -> str:
        return PERIOD_LABELS.get(self.key, self.key)

    @property
    def is_all(self) -> bool:
        return self.days is None

    def cutoff_ts(self, now: float | None = None) -> int | None:
        """返回起始时间戳；全部历史返回 None。"""
        if self.days is None:
            return None
        now = time.time() if now is None else now
        return int(now - self.days * 86400)

    def contains(self, ts: int, now: float | None = None) -> bool:
        """判断某个发布时间戳是否落在期限内。"""
        if ts is None or ts <= 0:
            # 没有时间信息的条目：只在「全部」时保留，避免误判
            return self.is_all
        cutoff = self.cutoff_ts(now)
        return cutoff is None or ts >= cutoff


def parse_period(value: str | int | None) -> Period:
    """把用户输入解析成 Period。

    接受：1m / 3m / 6m / 1y / all，以及「1个月」「半年」「全部历史」等中文说法。
    缺省（None 或空）按全部历史处理。
    """
    if value is None:
        return Period("all", None)
    if isinstance(value, int):
        for key, days in PERIOD_DAYS.items():
            if days == value:
                return Period(key, days)
        return Period("custom", value)

    raw = str(value).strip().lower()
    if not raw:
        return Period("all", None)

    key = _ALIASES.get(raw)
    if key is None:
        # 容错：去掉空格和「以内」后再试一次
        cleaned = raw.replace(" ", "").replace("以内", "")
        key = _ALIASES.get(cleaned)
    if key is None:
        raise PeriodError(
            f"无法识别的期限：{value!r}。"
            f"可用值：1m / 3m / 6m / 1y / all（或 1个月 / 3个月 / 半年 / 1年 / 全部历史）"
        )

    return Period(key, PERIOD_DAYS[key])


def filter_by_period(items: list[dict], period: Period, ts_getter=None,
                     now: float | None = None) -> list[dict]:
    """按期限过滤文章列表。

    ts_getter: 从条目里取发布时间戳的函数，默认取 create_time / update_time / publish_time。
    """
    if period.is_all:
        return list(items)

    if ts_getter is None:
        def ts_getter(item: dict) -> int:
            for key in ("create_time", "update_time", "publish_time", "date", "ts"):
                val = item.get(key)
                if isinstance(val, (int, float)) and val > 0:
                    return int(val)
                if isinstance(val, str) and val.isdigit():
                    return int(val)
            return 0

    return [item for item in items if period.contains(ts_getter(item), now=now)]
