"""时间边界工具。

约定：所有落库时间戳一律为 UTC epoch 秒（`int` 或 `float`），不存字符串；
程序内部时区由配置 `app.json.timezone` 决定，默认 Asia/Shanghai。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo as _TzInfo
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"
DAY_SECONDS = 86400

# Windows 默认不带 IANA 时区库（tzdata 缺失时 ZoneInfo 会抛错）。
# 这些时区固定偏移，兜底用；装了 tzdata 之后会自动优先走 ZoneInfo。
_FIXED_OFFSET_HOURS = {
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "UTC": 0,
    "Etc/UTC": 0,
}

_TZ_CACHE: dict[str, _TzInfo] = {}


def tzinfo(name: str | None = None) -> _TzInfo:
    """取时区对象；找不到 IANA 数据时退化为固定偏移。"""
    key = name or DEFAULT_TIMEZONE
    cached = _TZ_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        tz: _TzInfo = ZoneInfo(key)
    except Exception:  # ZoneInfoNotFoundError / OSError，取决于平台
        hours = _FIXED_OFFSET_HOURS.get(key)
        if hours is None:
            raise
        tz = timezone(timedelta(hours=hours), key)
    _TZ_CACHE[key] = tz
    return tz


def now_ts(tz_name: str | None = None) -> float:
    """当前时间戳（秒）。"""
    return datetime.now(tzinfo(tz_name)).timestamp()


def end_of_today_ts(tz_name: str | None = None, now: float | None = None) -> int:
    """今天 23:59:59 的时间戳。

    复习窗口用 ``due <= end_of_today_ts()``：一个词的复习日就是它 due 落在的那一天，
    那天任何时刻进来都能做。
    """
    tz = tzinfo(tz_name)
    current = datetime.fromtimestamp(now, tz) if now is not None else datetime.now(tz)
    return int(current.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())


def start_of_today_ts(tz_name: str | None = None, now: float | None = None) -> int:
    """今天 00:00:00 的时间戳，仪表盘按天分桶时用。"""
    tz = tzinfo(tz_name)
    current = datetime.fromtimestamp(now, tz) if now is not None else datetime.now(tz)
    return int(current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def day_start_ts(ts: float, tz_name: str | None = None) -> int:
    """把任意时间戳归到它所在自然日的 00:00:00。"""
    tz = tzinfo(tz_name)
    return int(datetime.fromtimestamp(ts, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def is_mastered(due: int | float | None, now: float, proficiency_days: int = 365) -> bool:
    """熟练判定：下次复习时间超过阈值天数。不落字段，实时推导。"""
    if not due:
        return False
    return (float(due) - float(now)) > proficiency_days * DAY_SECONDS


def learn_state(state: str | None, due: int | float | None, now: float, proficiency_days: int = 365) -> str:
    """把 state + due 归结为界面上的三态：learning / reviewing / mastered。"""
    if (state or "new") == "new":
        return "learning"
    return "mastered" if is_mastered(due, now, proficiency_days) else "reviewing"


def humanize_due(due: int | float | None, now: float, mastery_note: str = "待学习") -> str:
    """把 due 变成人类可读的相对时间，界面用。"""
    if not due:
        return mastery_note
    delta = float(due) - float(now)
    if delta <= 0:
        return "已到期"
    if delta < 3600:
        return f"{max(1, int(delta // 60))} 分钟后"
    if delta < DAY_SECONDS:
        return f"{int(delta // 3600)} 小时后"
    days = delta / DAY_SECONDS
    if days < 30:
        return f"{days:.0f} 天后"
    if days < 365:
        return f"{days / 30:.1f} 个月后"
    return f"{days / 365:.1f} 年后"
