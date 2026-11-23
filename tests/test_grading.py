"""grading 单元测试。"""

import unittest

from app.services import grading

AND = {"mode": "and", "sentence_min": 0.8, "keyword_min": 0.8}
WEIGHTED = {"mode": "weighted", "sentence_weight": 0.5, "keyword_weight": 0.5, "total_min": 0.8}


class AndModeTest(unittest.TestCase):
    def test_boundary_passes(self):
        self.assertTrue(grading.judge(0.8, 0.8, AND))

    def test_weak_keyword_rejected(self):
        self.assertFalse(grading.judge(0.85, 0.2, AND))

    def test_weak_sentence_rejected(self):
        self.assertFalse(grading.judge(0.3, 0.9, AND))

    def test_slightly_low_sentence_rejected(self):
        self.assertFalse(grading.judge(0.75, 0.9, AND))


class WeightedModeTest(unittest.TestCase):
    def test_strong_keyword_compensates(self):
        self.assertTrue(grading.judge(0.7, 1.0, WEIGHTED))

    def test_weak_keyword_still_rejected(self):
        self.assertFalse(grading.judge(0.85, 0.2, WEIGHTED))

    def test_boundary(self):
        self.assertTrue(grading.judge(0.8, 0.8, WEIGHTED))
        self.assertFalse(grading.judge(0.79, 0.8, WEIGHTED))

    def test_total_score(self):
        self.assertAlmostEqual(grading.total_score(0.7, 1.0, WEIGHTED), 0.85)


class SanityTest(unittest.TestCase):
    def test_scores_clamped(self):
        self.assertTrue(grading.judge(1.5, 1.5, AND))
        self.assertFalse(grading.judge(-1.0, 0.9, AND))

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            grading.judge(0.9, 0.9, {"mode": "either"})

    def test_default_rule_is_and(self):
        self.assertEqual(grading.normalize_rule()["mode"], "and")
        self.assertTrue(grading.judge(0.81, 0.81))


if __name__ == "__main__":
    unittest.main()
