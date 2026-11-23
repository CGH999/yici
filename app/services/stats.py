"""统计：仪表盘和主页要用的聚合查询。"""

from __future__ import annotations

from datetime import datetime

from app.services import clock


class Stats:
    def __init__(self, manager, config, task_manager) -> None:
        self.manager = manager
        self.config = config
        self.tm = task_manager

    # ---------------------------------------------------------------- 基础
    def _history(self):
        return self.manager.get("history")

    def _tz(self) -> str:
        return self.config.timezone()

    def daily(self, days: int = 30, container: str | None = None) -> list[dict]:
        """最近 N 天每天的学习 / 复习 / 拼写数量。"""
        tz = self._tz()
        today = clock.start_of_today_ts(tz)
        buckets: dict[int, dict] = {}
        for offset in range(days - 1, -1, -1):
            day = today - offset * clock.DAY_SECONDS
            buckets[day] = {
                "date": datetime.fromtimestamp(day, clock.tzinfo(tz)).strftime("%m-%d"),
                "learning": 0,
                "review": 0,
                "spelling": 0,
                "total": 0,
            }
        start = today - (days - 1) * clock.DAY_SECONDS
        sql = "SELECT time, task_type FROM history WHERE time >= :start"
        params = {"start": start}
        if container:
            sql += " AND container = :container"
            params["container"] = container
        for row in self._history().query(sql, **params):
            day = clock.day_start_ts(float(row["time"]), tz)
            bucket = buckets.get(day)
            if bucket is None:
                continue
            kind = row["task_type"] or "learning"
            if kind in bucket:
                bucket[kind] += 1
                bucket["total"] += 1
        return list(buckets.values())

    def today_counts(self, container: str | None = None) -> dict:
        tz = self._tz()
        today = clock.start_of_today_ts(tz)
        sql = "SELECT task_type, COUNT(*) c FROM history WHERE time >= :start"
        params = {"start": today}
        if container:
            sql += " AND container = :container"
            params["container"] = container
        sql += " GROUP BY task_type"
        result = {"learning": 0, "review": 0, "spelling": 0}
        for row in self._history().query(sql, **params):
            result[row["task_type"] or "learning"] = int(row["c"])
        result["total"] = sum(result.values())
        return result

    def streak(self, container: str | None = None) -> int:
        """连续打卡天数（今天没学则从昨天开始算）。"""
        tz = self._tz()
        today = clock.start_of_today_ts(tz)
        days = 400
        sql = "SELECT DISTINCT time FROM history WHERE time >= :start"
        params = {"start": today - days * clock.DAY_SECONDS}
        if container:
            sql += " AND container = :container"
            params["container"] = container
        active = {clock.day_start_ts(float(row["time"]), tz) for row in self._history().query(sql, **params)}
        count = 0
        cursor = today
        if cursor not in active:
            cursor -= clock.DAY_SECONDS
        while cursor in active:
            count += 1
            cursor -= clock.DAY_SECONDS
        return count

    def due_forecast(self, days: int = 7, container: str | None = None, ctype: str = "book") -> list[dict]:
        """未来 N 天每天到期的词数（含今天）。"""
        tz = self._tz()
        today = clock.start_of_today_ts(tz)
        containers = [container] if container else self.tm.containers(ctype)
        db = self.tm._db(ctype)
        result = []
        for offset in range(days):
            start = today + offset * clock.DAY_SECONDS
            end = start + clock.DAY_SECONDS
            total = 0
            for name in containers:
                if name not in self.tm.containers(ctype):
                    continue
                from app.db.schema import quote

                total += int(
                    list(
                        db.query(
                            f"SELECT COUNT(*) c FROM {quote(name)} "
                            "WHERE state = 'review' AND due IS NOT NULL AND due >= :s AND due < :e",
                            s=start,
                            e=end,
                        )
                    )[0]["c"]
                )
            result.append(
                {
                    "date": datetime.fromtimestamp(start, clock.tzinfo(tz)).strftime("%m-%d"),
                    "count": total,
                }
            )
        return result

    def totals(self, container: str, ctype: str = "book") -> dict:
        history = self._history()
        params = {"container": container}
        learned = int(
            list(history.query("SELECT COUNT(*) c FROM history WHERE container = :container", **params))[0]["c"]
        )
        reviewed = int(
            list(
                history.query(
                    "SELECT COUNT(*) c FROM history WHERE container = :container AND task_type = :t",
                    t="review",
                    **params,
                )
            )[0]["c"]
        )
        first_pass = int(
            list(
                history.query(
                    "SELECT COUNT(*) c FROM history WHERE container = :container AND task_type != :t AND first_pass = 1",
                    t="spelling",
                    **params,
                )
            )[0]["c"]
        )
        graded = int(
            list(
                history.query(
                    "SELECT COUNT(*) c FROM history WHERE container = :container AND task_type != :t",
                    t="spelling",
                    **params,
                )
            )[0]["c"]
        )
        return {
            "events": learned,
            "reviewed": reviewed,
            "first_pass_rate": (first_pass / graded) if graded else 0.0,
        }
