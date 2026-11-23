"""回归：任务表清空后 rowid 会复用，两轮之间不能因此把作答丢掉。"""

import tempfile
import time
import unittest
from pathlib import Path

from app.config import Config
from app.db import schema
from app.db.manager import DBManager
from app.services.task_manager import TaskManager, _uid_answer_id


class AnswerIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name))
        self.config.bootstrap()
        self.config.update("app.json", {"sentence_history": 5})
        self.manager = DBManager.from_config(self.config)
        self.tm = TaskManager(self.manager, self.config)

        db = self.manager.get("library")
        schema.ensure_container_table(db, "cet4")
        schema.ensure_indexes(db, "cet4")
        schema.ensure_history_table(self.manager.get("history"))
        schema.ensure_history_index(self.manager.get("history"))
        db["cet4"].insert(
            {"uid": "u1", "word": "abandon", "translation": "放弃", "state": "review", "due": int(time.time()) - 100, "reps": 0, "lapses": 0}
        )
        db.commit()
        for kind in ("learning", "review", "spelling"):
            schema.ensure_task_table(self.manager.get("tasks"), "cet4", kind)

    def tearDown(self):
        self.manager.close_all()
        self.tmp.cleanup()

    def _run_round(self) -> float:
        """开一轮复习、把词做完，返回写回的 last_review。"""
        self.manager.get("library")["cet4"].update(
            {"uid": "u1", "due": int(time.time()) - 100, "state": "review"}, keys=["uid"]
        )
        self.manager.get("library").commit()
        self.tm.create_task("cet4", "book", "review")
        result = self.tm.submit("cet4", "book", "review", "u1", passed=True)
        self.assertTrue(result["finished"])
        return self.tm.get_word("cet4", "book", "u1")["last_review"]

    def test_second_round_is_not_dropped(self):
        first = self._run_round()
        time.sleep(0.02)
        second = self._run_round()
        self.assertGreater(second, first, "第二轮作答被当成重复而跳过了")

    def test_two_rounds_get_different_answer_ids(self):
        self.manager.get("library")["cet4"].update({"uid": "u1", "due": int(time.time()) - 100}, keys=["uid"])
        self.manager.get("library").commit()
        self.tm.create_task("cet4", "book", "review")
        row1 = dict(list(self.manager.get("tasks").query('SELECT * FROM "cet4__review"'))[0])

        self.tm.create_task("cet4", "book", "review")
        row2 = dict(list(self.manager.get("tasks").query('SELECT * FROM "cet4__review"'))[0])

        self.assertEqual(row1["id"], row2["id"])  # rowid 确实会复用
        self.assertNotEqual(row1["batch_id"], row2["batch_id"])
        self.assertNotEqual(
            _uid_answer_id("cet4", "book", "review", row1["id"], row1["batch_id"]),
            _uid_answer_id("cet4", "book", "review", row2["id"], row2["batch_id"]),
        )

    def test_history_not_deduplicated_across_rounds(self):
        self._run_round()
        time.sleep(0.02)
        self._run_round()
        total = list(self.manager.get("history").query("SELECT COUNT(*) AS c FROM history"))[0]["c"]
        self.assertEqual(total, 2)

    def test_same_batch_stays_idempotent(self):
        """同一轮里重复调用 _apply 不能写两次（重放安全）。"""
        self.manager.get("library")["cet4"].update({"uid": "u1", "due": int(time.time()) - 100}, keys=["uid"])
        self.manager.get("library").commit()
        self.tm.create_task("cet4", "book", "review")
        self.tm.submit("cet4", "book", "review", "u1", passed=True)
        self.tm._apply("cet4", "book", "review", "u1")
        total = list(self.manager.get("history").query("SELECT COUNT(*) AS c FROM history"))[0]["c"]
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()
