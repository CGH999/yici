"""学习 / 复习两轮互不干扰的测试。"""

import tempfile
import time
import unittest
from pathlib import Path

from app import create_app
from app.config import Config
from app.db import schema
from app.db.manager import DBManager
from app.db.schema import ensure_container_table, ensure_indexes


class RoundSplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.config = Config(base)
        self.config.bootstrap()
        self.config.update(
            "app.json",
            {
                "current_container": "cet4",
                "current_container_type": "book",
                "learning_per_round": 5,
                "review_per_round": 5,
                "pass_rule": {"mode": "and", "sentence_min": 0.6, "keyword_min": 0.8, "total_min": 0.6},
            },
        )

        # 先建库填词，再 create_app（create_app 启动时会校验当前词书）
        manager = DBManager.from_config(self.config)
        table = ensure_container_table(manager.get("library"), "cet4")
        ensure_indexes(manager.get("library"), "cet4")
        now = int(time.time())
        rows = [
            {"uid": "new-1", "word": "alpha", "translation": "阿尔法", "state": "new", "reps": 0, "lapses": 0},
            {"uid": "new-2", "word": "beta", "translation": "贝塔", "state": "new", "reps": 0, "lapses": 0},
            {"uid": "rev-1", "word": "gamma", "translation": "伽马", "state": "review", "due": now - 3600, "reps": 1, "lapses": 0},
            {"uid": "rev-2", "word": "delta", "translation": "德尔塔", "state": "review", "due": now - 1800, "reps": 1, "lapses": 0},
        ]
        for row in rows:
            table.insert(row)
        manager.get("library").commit()
        manager.close_all()

        self.app = create_app(self.config, allow_lan=False)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.tm = self.app.extensions["tm"]
        self.round = self.app.extensions["round"]

    def tearDown(self):
        self.app.extensions["db"].close_all()
        self.tmp.cleanup()

    def _start_both(self):
        self.client.post("/study/start", data={"kind": "learning"})
        self.client.post("/study/start", data={"kind": "review"})

    def test_rounds_are_separate(self):
        self._start_both()
        learning = self.round.get("learning")
        review = self.round.get("review")
        self.assertEqual(learning["kind"], "learning")
        self.assertEqual(review["kind"], "review")
        self.assertEqual(learning["container"], "cet4")
        self.assertEqual(review["container"], "cet4")

    def test_starting_review_does_not_overwrite_learning(self):
        self.client.post("/study/start", data={"kind": "learning"})
        before = dict(self.round.get("learning"))
        self.client.post("/study/start", data={"kind": "review"})
        after = self.round.get("learning")
        self.assertEqual(before["started_at"], after["started_at"])
        self.assertEqual(after["kind"], "learning")

    def test_session_kind_picks_the_right_round(self):
        self._start_both()
        self.tm.set_problem("cet4", "learning", "new-1", {"sentence": "Alpha is first.", "translation": "阿尔法是第一个。"})
        self.tm.set_problem("cet4", "review", "rev-1", {"sentence": "Gamma ray.", "translation": "伽马射线。"})

        learning_page = self.client.get("/study/session?kind=learning").get_data(as_text=True)
        self.assertIn("alpha", learning_page)
        self.assertNotIn("gamma", learning_page)

        review_page = self.client.get("/study/session?kind=review").get_data(as_text=True)
        self.assertIn("gamma", review_page)
        self.assertNotIn("alpha", review_page)

    def test_session_without_kind_falls_back(self):
        self._start_both()
        self.tm.set_problem("cet4", "review", "rev-1", {"sentence": "Gamma ray.", "translation": "伽马射线。"})
        # 最近启动的是复习，所以不带 kind 时应当进复习
        page = self.client.get("/study/session").get_data(as_text=True)
        self.assertIn("gamma", page)

    def test_conflict_page_links_the_right_kind(self):
        self.client.post("/study/start", data={"kind": "learning"})
        page = self.client.get("/study/conflict?kind=learning").get_data(as_text=True)
        self.assertIn("kind=learning", page)

        self.client.post("/study/start", data={"kind": "review"})
        page = self.client.get("/study/conflict?kind=review").get_data(as_text=True)
        self.assertIn("kind=review", page)

    def test_home_shows_both_unfinished(self):
        self._start_both()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("学习还没做完", html)
        self.assertIn("复习还没做完", html)

    def test_generation_flag_is_per_kind(self):
        self._start_both()
        self.assertTrue(self.round.begin_generation("learning"))
        self.assertFalse(self.round.get("learning")["generating"] is False)
        self.assertFalse(self.round.get("review")["generating"])
        self.round.end_generation("learning")
        self.assertFalse(self.round.get("learning")["generating"])


if __name__ == "__main__":
    unittest.main()
