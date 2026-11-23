"""任务模块测试：状态机、写回、幂等、拼写、未完成任务。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.db import schema
from app.db.manager import DBManager
from app.services.task_manager import TaskManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TaskManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "config").mkdir(parents=True)
        shutil.copy(PROJECT_ROOT / "config" / "fsrs.json", base / "config" / "fsrs.json")
        (base / "config" / "app.json").write_text(
            json.dumps(
                {
                    "timezone": "Asia/Shanghai",
                    "learning_per_round": 3,
                    "review_per_round": 3,
                    "preview_multiplier": 2,
                    "proficiency_days": 365,
                }
            ),
            encoding="utf-8",
        )
        self.config = Config(base)
        self.manager = DBManager(
            {
                "library": base / "data" / "library.db",
                "wordbooks": base / "data" / "wordbooks.db",
                "tasks": base / "data" / "tasks.db",
                "history": base / "data" / "user" / "history.db",
            }
        )
        schema.ensure_library(self.manager, self.config)
        self.tm = TaskManager(self.manager, self.config)
        self.tm.create_container("book", "cet4")
        self.table = self.manager.get("library")["cet4"]

    def tearDown(self):
        self.manager.close_all()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- helpers
    def add_word(self, word, **kwargs):
        row = {
            "uid": f"uid-{word}",
            "word": word,
            "translation": f"释义-{word}",
            "state": "new",
            "reps": 0,
            "lapses": 0,
        }
        row.update(kwargs)
        return self.table.insert(row)

    def history_rows(self):
        return [dict(r) for r in self.manager.get("history").query("SELECT * FROM history ORDER BY id")]

    def word(self, word):
        return dict(self.table.find_one(uid=f"uid-{word}"))

    # ---------------------------------------------------------------- 建任务
    def test_create_learning_task_in_id_order(self):
        for w in ("ability", "abandon", "able"):
            self.add_word(w)
        result = self.tm.create_task("cet4", "book", "learning")
        self.assertEqual(result["created"], 3)
        rows = [dict(r) for r in self.manager.get("tasks").query('SELECT * FROM "cet4__learning" ORDER BY id')]
        self.assertEqual([r["word"] for r in rows], ["ability", "abandon", "able"])
        self.assertTrue(all(r["problem_state"] == "pending" for r in rows))
        self.assertTrue(all(r["task_state"] == 0 for r in rows))

    def test_create_task_respects_limit(self):
        for w in ("a", "b", "c", "d"):
            self.add_word(w)
        self.assertEqual(self.tm.create_task("cet4", "book", "learning")["created"], 3)

    def test_create_task_with_no_new_words(self):
        self.add_word("done", state="review", due=0)
        self.assertEqual(self.tm.create_task("cet4", "book", "learning")["created"], 0)

    def test_review_task_only_takes_due_today(self):
        import time

        now = int(time.time())
        self.add_word("dueword", state="review", due=now - 100)
        self.add_word("future", state="review", due=now + 3 * 86400)
        self.add_word("newbie", state="new")
        result = self.tm.create_task("cet4", "book", "review")
        self.assertEqual(result["created"], 1)
        rows = list(self.manager.get("tasks").query('SELECT word FROM "cet4__review"'))
        self.assertEqual([r["word"] for r in rows], ["dueword"])

    def test_recreating_task_clears_previous(self):
        self.add_word("a")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.create_task("cet4", "book", "learning")
        count = list(self.manager.get("tasks").query('SELECT COUNT(*) c FROM "cet4__learning"'))[0]["c"]
        self.assertEqual(count, 1)

    # ---------------------------------------------------------------- 状态机
    def test_first_pass_true_finishes_immediately(self):
        self.add_word("able")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-able", {"sentence": "I am able.", "translation": "我有能力。"})
        task = self.tm.next_task("cet4", "learning")
        self.assertEqual(task["uid"], "uid-able")

        result = self.tm.submit("cet4", "book", "learning", "uid-able", passed=True, answer="我有能力。")
        self.assertTrue(result["finished"])
        self.assertEqual(result["task_state"], 1)
        self.assertTrue(result["first_pass"])

        word = self.word("able")
        self.assertEqual(word["state"], "review")
        self.assertEqual(word["stability"], 155520.0)
        self.assertEqual(word["reps"], 1)
        self.assertEqual(word["lapses"], 0)
        self.assertIsNotNone(word["due"])

        history = self.history_rows()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["task_type"], "learning")
        self.assertEqual(history[0]["first_pass"], 1)

    def test_wrong_answer_cycles_twice(self):
        self.add_word("county")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-county", {"sentence": "This county is quiet."})

        first = self.tm.submit("cet4", "book", "learning", "uid-county", passed=False, answer="国家")
        self.assertEqual(first["task_state"], -1)
        self.assertFalse(first["finished"])
        self.assertFalse(first["first_pass"])
        self.assertEqual(first["error_count"], 1)
        self.assertEqual(self.word("county")["state"], "new")  # 未完成不写词库

        second = self.tm.submit("cet4", "book", "learning", "uid-county", passed=True)
        self.assertEqual(second["task_state"], 0)
        self.assertFalse(second["finished"])
        self.assertEqual(self.word("county")["state"], "new")

        third = self.tm.submit("cet4", "book", "learning", "uid-county", passed=True)
        self.assertEqual(third["task_state"], 1)
        self.assertTrue(third["finished"])
        self.assertFalse(third["first_pass"])  # 一旦错过永久 False

        word = self.word("county")
        self.assertEqual(word["state"], "review")
        self.assertEqual(word["stability"], 60480.0)  # 86400 * 0.7，且被 1 天封顶
        self.assertEqual(word["reps"], 2)
        self.assertEqual(word["lapses"], 1)

    def test_repeated_failures_never_go_below_minus_one(self):
        self.add_word("x")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-x", {"sentence": "x"})
        for _ in range(4):
            result = self.tm.submit("cet4", "book", "learning", "uid-x", passed=False)
        self.assertEqual(result["task_state"], -1)
        self.assertEqual(result["error_count"], 4)

    def test_submit_after_finished_is_noop(self):
        self.add_word("y")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-y", {"sentence": "y"})
        self.tm.submit("cet4", "book", "learning", "uid-y", passed=True)
        stability = self.word("y")["stability"]
        again = self.tm.submit("cet4", "book", "learning", "uid-y", passed=False)
        self.assertTrue(again["already_finished"])
        self.assertEqual(self.word("y")["stability"], stability)
        self.assertEqual(len(self.history_rows()), 1)

    def test_apply_is_idempotent(self):
        self.add_word("z")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-z", {"sentence": "z"})
        self.tm.submit("cet4", "book", "learning", "uid-z", passed=True)
        stability = self.word("z")["stability"]
        reps = self.word("z")["reps"]

        # 模拟崩溃后重放：把 applied 抹掉再调一次
        self.manager.get("tasks")["cet4__learning"].update(
            {"uid": "uid-z", "applied": False}, keys=["uid"]
        )
        self.tm._apply("cet4", "book", "learning", "uid-z")

        self.assertEqual(self.word("z")["stability"], stability)
        self.assertEqual(self.word("z")["reps"], reps)
        self.assertEqual(len(self.history_rows()), 1)

    def test_review_flow_uses_old_stability(self):
        self.add_word("r", state="review", due=1, difficulty=5.0, stability=2592000.0)
        self.tm.create_task("cet4", "book", "review")
        self.tm.set_problem("cet4", "review", "uid-r", {"sentence": "r"})
        result = self.tm.submit("cet4", "book", "review", "uid-r", passed=True)
        self.assertTrue(result["finished"])
        word = self.word("r")
        self.assertEqual(word["difficulty"], 4.16)
        self.assertEqual(word["stability"], 4665600.0)

    # ---------------------------------------------------------------- 队列
    def test_next_task_skips_unready_problems(self):
        for w in ("p", "q"):
            self.add_word(w)
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-q", {"sentence": "q"})
        task = self.tm.next_task("cet4", "learning")
        self.assertEqual(task["uid"], "uid-q")
        self.assertEqual(task["problem_data"]["sentence"], "q")

    def test_skip_problem_removes_row(self):
        for w in ("m", "n"):
            self.add_word(w)
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-m", {"sentence": "m"})
        self.tm.set_problem("cet4", "learning", "uid-n", {"sentence": "n"})
        self.tm.skip_problem("cet4", "learning", "uid-m")
        self.assertEqual(self.tm.problems("cet4", "learning")["total"], 1)
        self.assertEqual(self.tm.progress("cet4", "learning")["total"], 1)

    def test_problems_progress(self):
        for w in ("a1", "a2"):
            self.add_word(w)
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-a1", {"sentence": "x"})
        self.tm.fail_problem("cet4", "learning", "uid-a2", "timeout")
        info = self.tm.problems("cet4", "learning")
        self.assertEqual((info["total"], info["ready"], info["failed"], info["pending"]), (2, 1, 1, 0))

    def test_wrong_word_comes_back_after_a_full_lap(self):
        """答错的词不能被立刻重问：要绕完一圈才轮到它。"""
        for word in ("w1", "w2", "w3"):
            self.add_word(word)
        self.tm.create_task("cet4", "book", "learning")
        for word in ("w1", "w2", "w3"):
            self.tm.set_problem("cet4", "learning", f"uid-{word}", {"sentence": word})

        first = self.tm.next_task("cet4", "learning")
        self.assertEqual(first["word"], "w1")
        self.tm.submit("cet4", "book", "learning", "uid-w1", passed=False)

        second = self.tm.next_task("cet4", "learning")
        self.assertEqual(second["word"], "w2", "答错后应该先出下一个词，而不是重复刚才的词")

        self.tm.submit("cet4", "book", "learning", "uid-w2", passed=True)
        self.assertEqual(self.tm.next_task("cet4", "learning")["word"], "w3")
        self.tm.submit("cet4", "book", "learning", "uid-w3", passed=True)

        back = self.tm.next_task("cet4", "learning")
        self.assertEqual(back["word"], "w1", "绕完一圈才回到错的词")

    def test_wrong_word_needs_two_more_correct_answers(self):
        self.add_word("only")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-only", {"sentence": "x"})
        self.tm.submit("cet4", "book", "learning", "uid-only", passed=False)
        self.assertEqual(self.tm.next_task("cet4", "learning")["word"], "only")
        self.tm.submit("cet4", "book", "learning", "uid-only", passed=True)
        self.assertEqual(self.tm.next_task("cet4", "learning")["word"], "only")
        result = self.tm.submit("cet4", "book", "learning", "uid-only", passed=True)
        self.assertTrue(result["finished"])
        self.assertEqual(self.tm.next_task("cet4", "learning"), None)

    def test_unfinished_detection(self):
        for w in ("u1", "u2", "u3"):
            self.add_word(w)
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-u1", {"sentence": "x"})
        self.assertTrue(self.tm.has_unfinished("cet4", "book", "learning"))
        self.tm.submit("cet4", "book", "learning", "uid-u1", passed=True)
        unfinished = self.tm.unfinished_tasks()
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(unfinished[0]["done"], 1)
        self.assertEqual(unfinished[0]["total"], 3)

    # ---------------------------------------------------------------- 拼写
    def test_spelling_state_machine(self):
        self.add_word("county")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-county", {"sentence": "x"})
        self.tm.submit("cet4", "book", "learning", "uid-county", passed=True)
        stability = self.word("county")["stability"]

        self.assertEqual(self.tm.create_spelling("cet4", "book", "learning"), 1)

        wrong = self.tm.submit_spelling("cet4", "book", "uid-county", "counti")
        self.assertFalse(wrong["correct"])
        self.assertEqual(wrong["task_state"], -1)
        self.assertTrue(wrong["revealed"])
        self.assertEqual(wrong["expected"], "county")
        self.assertEqual(wrong["error_count"] if "error_count" in wrong else 1, 1)

        copy = self.tm.submit_spelling("cet4", "book", "uid-county", "county")
        self.assertTrue(copy["correct"])
        self.assertEqual(copy["task_state"], 0)
        self.assertFalse(copy["finished"])  # 抄写正确只回到 0

        blind = self.tm.submit_spelling("cet4", "book", "uid-county", "County")
        self.assertTrue(blind["correct"])
        self.assertTrue(blind["finished"])
        self.assertTrue(blind["round_finished"])

        # 拼写不改词库
        self.assertEqual(self.word("county")["stability"], stability)

        history = [r for r in self.history_rows() if r["task_type"] == "spelling"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["first_pass"], 0)
        self.assertEqual(history[0]["error_count"], 1)

    def test_spelling_clean_pass(self):
        self.add_word("easy")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-easy", {"sentence": "x"})
        self.tm.submit("cet4", "book", "learning", "uid-easy", passed=True)
        self.tm.create_spelling("cet4", "book", "learning")
        result = self.tm.submit_spelling("cet4", "book", "uid-easy", "easy")
        self.assertTrue(result["finished"])
        history = [r for r in self.history_rows() if r["task_type"] == "spelling"]
        self.assertEqual(history[0]["first_pass"], 1)

    def test_abandon_spelling(self):
        self.add_word("q")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-q", {"sentence": "x"})
        self.tm.submit("cet4", "book", "learning", "uid-q", passed=True)
        self.tm.create_spelling("cet4", "book", "learning")
        self.tm.abandon_spelling("cet4")
        self.assertEqual(self.tm.spelling_progress("cet4")["total"], 0)

    # ---------------------------------------------------------------- 其它
    def test_preview_window(self):
        for i in range(5):
            self.add_word(f"w{i}")
        data = self.tm.preview("cet4", "book")
        self.assertEqual(len(data["words"]), 5)  # 3 * 2 = 6，只有 5 个新词
        self.assertEqual(data["limit"], 6)

    def test_container_stats(self):
        self.add_word("s1")
        self.add_word("s2", state="review", due=0, stability=86400.0, difficulty=3.0)
        stats = self.tm.container_stats("cet4", "book")
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["due_today"], 1)

    def test_round_summary(self):
        self.add_word("r1")
        self.tm.create_task("cet4", "book", "learning")
        self.tm.set_problem("cet4", "learning", "uid-r1", {"sentence": "x"})
        self.tm.submit("cet4", "book", "learning", "uid-r1", passed=True)
        summary = self.tm.round_summary("cet4", "book", "learning")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["learn_state"], "reviewing")
        self.assertTrue(summary[0]["due_text"].endswith("后"))


if __name__ == "__main__":
    unittest.main()
