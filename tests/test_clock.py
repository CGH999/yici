"""clock 单元测试：天边界、熟练判定。"""

import unittest
from datetime import datetime

from app.services import clock

TZ = "Asia/Shanghai"


def ts(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=clock.tzinfo(TZ)).timestamp()


class BoundaryTest(unittest.TestCase):
    def test_end_of_today(self):
        now = ts(2026, 11, 22, 10, 30)
        self.assertEqual(clock.end_of_today_ts(TZ, now), ts(2026, 11, 22, 23, 59, 59))

    def test_end_of_today_late(self):
        now = ts(2026, 11, 22, 23, 59, 58)
        self.assertEqual(clock.end_of_today_ts(TZ, now), ts(2026, 11, 22, 23, 59, 59))

    def test_start_of_today(self):
        self.assertEqual(clock.start_of_today_ts(TZ, ts(2026, 11, 22, 10, 30)), ts(2026, 11, 22, 0, 0, 0))

    def test_day_start_of_arbitrary_ts(self):
        self.assertEqual(clock.day_start_ts(ts(2026, 11, 22, 10, 30), TZ), ts(2026, 11, 22, 0, 0, 0))


class MasteryTest(unittest.TestCase):
    def test_is_mastered(self):
        now = ts(2026, 11, 22, 10, 30)
        self.assertTrue(clock.is_mastered(now + 400 * 86400, now, 365))
        self.assertFalse(clock.is_mastered(now + 300 * 86400, now, 365))
        self.assertFalse(clock.is_mastered(None, now, 365))

    def test_learn_state(self):
        now = ts(2026, 11, 22, 10, 30)
        self.assertEqual(clock.learn_state("new", None, now), "learning")
        self.assertEqual(clock.learn_state("review", now + 86400, now), "reviewing")
        self.assertEqual(clock.learn_state("review", now + 400 * 86400, now), "mastered")

    def test_humanize(self):
        now = ts(2026, 11, 22, 10, 30)
        self.assertEqual(clock.humanize_due(None, now), "待学习")
        self.assertEqual(clock.humanize_due(now - 10, now), "已到期")
        self.assertEqual(clock.humanize_due(now + 3 * 86400, now), "3 天后")


if __name__ == "__main__":
    unittest.main()
