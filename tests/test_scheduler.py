"""scheduler 单元测试：公式必须与给定的两个函数逐位一致（除失败封顶）。"""

import unittest

from app.services import scheduler

NOW = 1_700_000_000.0
# 这组用例校验的是纯公式，所以把「最短间隔」关掉；下限单独在 IntervalFloorTest 里测
CFG = {
    "initial_difficulty": 3.0,
    "initial_stability": 86400.0,
    "target_d": 3.0,
    "alpha": 0.8,
    "beta": 0.4,
    "damping": 0.02,
    "failure_stability_cap_seconds": 86400,
    "min_interval_seconds": 0,
}
FLOOR_CFG = dict(CFG, min_interval_seconds=86400)


class FirstLearningTest(unittest.TestCase):
    def test_first_pass_boosts_stability(self):
        r = scheduler.first_learning(True, 0, NOW, CFG)
        self.assertEqual(r["difficulty"], 2.2)
        self.assertEqual(r["stability"], 155520.0)
        self.assertEqual(r["next_review"], NOW + 155520.0)

    def test_first_pass_ignores_error_count(self):
        self.assertEqual(
            scheduler.first_learning(True, 0, NOW, CFG),
            scheduler.first_learning(True, 5, NOW, CFG),
        )

    def test_one_error(self):
        r = scheduler.first_learning(False, 1, NOW, CFG)
        self.assertEqual(r["difficulty"], 3.4)
        self.assertEqual(r["stability"], 60480.0)  # 86400 * 0.7
        self.assertEqual(r["next_review"], NOW + 60480.0)

    def test_failure_floor(self):
        r = scheduler.first_learning(False, 10, NOW, CFG)
        self.assertEqual(r["stability"], 25920.0)  # 86400 * 0.3
        self.assertEqual(r["difficulty"], 7.0)

    def test_difficulty_clamped_high(self):
        self.assertEqual(scheduler.first_learning(False, 100, NOW, CFG)["difficulty"], 10.0)

    def test_difficulty_clamped_low(self):
        cfg = dict(CFG, initial_difficulty=1.0)
        self.assertEqual(scheduler.first_learning(True, 0, NOW, cfg)["difficulty"], 1.0)

    def test_negative_error_count_rejected(self):
        with self.assertRaises(ValueError):
            scheduler.first_learning(False, -1, NOW, CFG)

    def test_defaults(self):
        self.assertEqual(scheduler.first_learning(True, 0, NOW)["stability"], 155520.0)

    def test_rounding(self):
        r = scheduler.first_learning(False, 3, NOW, CFG)
        self.assertEqual(round(r["difficulty"], 4), r["difficulty"])


class ReviewTest(unittest.TestCase):
    def test_success(self):
        r = scheduler.review_pass(True, 0, NOW, 5.0, 2592000.0, CFG)
        self.assertEqual(r["difficulty"], 4.16)
        self.assertEqual(r["stability"], 4665600.0)
        self.assertEqual(r["next_review"], NOW + 4665600.0)

    def test_failure_is_capped(self):
        r = scheduler.review_pass(False, 1, NOW, 5.0, 2592000.0, CFG)
        self.assertEqual(r["difficulty"], 5.36)
        self.assertEqual(r["stability"], 86400.0)  # 1814400 -> 封顶 1 天

    def test_cap_can_be_disabled(self):
        cfg = dict(CFG, failure_stability_cap_seconds=0)
        r = scheduler.review_pass(False, 1, NOW, 5.0, 2592000.0, cfg)
        self.assertEqual(r["stability"], 1814400.0)

    def test_missing_old_values(self):
        r = scheduler.review_pass(True, 0, NOW, None, None, CFG)
        self.assertEqual(r["difficulty"], 2.2)
        self.assertEqual(r["stability"], 155520.0)


class IntervalFloorTest(unittest.TestCase):
    """最短间隔：到期时间不早于「完成时间 + min_interval」，但 S 不变。"""

    def test_failure_is_floored_to_one_day(self):
        r = scheduler.first_learning(False, 1, NOW, FLOOR_CFG)
        self.assertEqual(r["next_review"], NOW + 86400)
        self.assertEqual(r["stability"], 60480.0)  # S 没有被抬高

    def test_multi_error_still_floored(self):
        r = scheduler.first_learning(False, 5, NOW, FLOOR_CFG)
        self.assertEqual(r["next_review"], NOW + 86400)
        self.assertLess(r["stability"], 86400)

    def test_review_failure_floored(self):
        r = scheduler.review_pass(False, 3, NOW, 5.0, 2592000.0, FLOOR_CFG)
        self.assertEqual(r["next_review"], NOW + 86400)

    def test_long_interval_is_not_shortened(self):
        r = scheduler.review_pass(True, 0, NOW, 5.0, 2592000.0, FLOOR_CFG)
        self.assertEqual(r["next_review"], NOW + 4665600.0)

    def test_floor_can_be_disabled(self):
        r = scheduler.first_learning(False, 1, NOW, CFG)
        self.assertEqual(r["next_review"], NOW + 60480.0)

    def test_default_is_one_day(self):
        # 不传配置 -> DEFAULT_CONFIG 的 min_interval_seconds=86400
        r = scheduler.first_learning(False, 1, NOW)
        self.assertEqual(r["next_review"], NOW + 86400)

    def test_negative_floor_treated_as_off(self):
        cfg = dict(CFG, min_interval_seconds=-100)
        self.assertEqual(scheduler.first_learning(False, 1, NOW, cfg)["next_review"], NOW + 60480.0)


class IntervalFloorOrderingTest(unittest.TestCase):
    def test_due_is_monotonic_in_completion_time(self):
        """完成时间越晚，下一次到期也越晚（不会因为取整倒退）。"""
        first = scheduler.first_learning(False, 2, NOW, FLOOR_CFG)["next_review"]
        second = scheduler.first_learning(False, 2, NOW + 3600, FLOOR_CFG)["next_review"]
        self.assertGreater(second, first)


if __name__ == "__main__":
    unittest.main()
