"""词本编辑（重命名 / JSON 追加 / 从词库选词）的接口测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from app import create_app
from app.config import Config


class WordbookEditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name))
        self.config.bootstrap()
        self.config.update(
            "app.json",
            {
                "current_container": "cet4",
                "current_container_type": "book",
                "pass_rule": {"mode": "and", "sentence_min": 0.6, "keyword_min": 0.8, "total_min": 0.6},
            },
        )
        self.app = create_app(self.config, allow_lan=False)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.tm = self.app.extensions["tm"]

        # 一本词书 + 一个空词本
        self.tm.create_container("book", "cet4")
        self.tm.create_container("wordbook", "生词本")
        rows = [
            {"uid": "u1", "word": "abandon", "translation": "放弃", "state": "new", "favorite": False, "reps": 0, "lapses": 0},
            {"uid": "u2", "word": "abbey", "translation": "修道院", "state": "new", "favorite": False, "reps": 0, "lapses": 0},
            {"uid": "u3", "word": "ability", "translation": "能力", "state": "new", "favorite": False, "reps": 0, "lapses": 0},
        ]
        db = self.app.extensions["db"].get("library")
        for row in rows:
            db["cet4"].insert(row)
        db.commit()

    def tearDown(self):
        self.app.extensions["db"].close_all()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 重命名
    def test_rename_wordbook(self):
        response = self.client.post(
            "/api/wordbook/rename", json={"name": "生词本", "new_name": "高频词", "ctype": "wordbook"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["name"], "高频词")
        self.assertIn("高频词", self.tm.containers("wordbook"))
        self.assertNotIn("生词本", self.tm.containers("wordbook"))
        # 任务表也改名了
        self.assertIn("高频词__learning", self.app.extensions["db"].get("tasks").tables)

    def test_rename_to_existing_name_rejected(self):
        response = self.client.post(
            "/api/wordbook/rename", json={"name": "生词本", "new_name": "cet4", "ctype": "wordbook"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("生词本", self.tm.containers("wordbook"))

    def test_cannot_create_wordbook_with_book_name(self):
        """词书和词本共用任务表命名空间，同名会撞车。"""
        response = self.client.post("/api/container/create", json={"name": "cet4", "ctype": "wordbook"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("cet4", self.tm.containers("wordbook"))

    def test_rename_keeps_words(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1"]})
        self.client.post(
            "/api/wordbook/rename", json={"name": "生词本", "new_name": "备份", "ctype": "wordbook"}
        )
        db = self.app.extensions["db"].get("wordbooks")
        self.assertEqual(list(db.query('SELECT COUNT(*) AS c FROM "备份"'))[0]["c"], 1)

    # ---------------------------------------------------------------- JSON 追加
    def test_append_json(self):
        text = json.dumps({"data": [{"word": "apple", "translation": "苹果"}, {"word": "banana", "translation": "香蕉"}]})
        response = self.client.post("/api/wordbook/append", json={"name": "生词本", "text": text, "ctype": "wordbook"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["inserted"], 2)
        self.assertEqual(data["already"], 0)

        # 再追加一次：两个都算"已存在"
        response = self.client.post("/api/wordbook/append", json={"name": "生词本", "text": text, "ctype": "wordbook"})
        data = response.get_json()
        self.assertEqual(data["inserted"], 0)
        self.assertEqual(data["already"], 2)

    def test_append_does_not_touch_existing_rows(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1"]})
        db = self.app.extensions["db"].get("wordbooks")
        before = list(db.query('SELECT * FROM "生词本" WHERE uid = :u', u="u1"))[0]

        text = json.dumps({"data": [{"word": "abandon", "translation": "换个释义"}]})
        self.client.post("/api/wordbook/append", json={"name": "生词本", "text": text, "ctype": "wordbook"})
        after = list(db.query('SELECT * FROM "生词本" WHERE uid = :u', u="u1"))[0]
        self.assertEqual(before["translation"], after["translation"])

    def test_append_to_missing_wordbook(self):
        response = self.client.post(
            "/api/wordbook/append", json={"name": "不存在", "text": '{"data":[{"word":"x"}]}', "ctype": "wordbook"}
        )
        self.assertEqual(response.status_code, 400)

    # ---------------------------------------------------------------- 选词
    def test_wordbank_words_search(self):
        response = self.client.get("/api/wordbank/words?container=cet4&ctype=book&target=生词本&q=abb")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([item["word"] for item in data["items"]], ["abbey"])

        response = self.client.get("/api/wordbank/words?container=cet4&ctype=book&q=ab")
        self.assertEqual(len(response.get_json()["items"]), 3)

    def test_wordbank_words_marks_existing(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u2"]})
        response = self.client.get("/api/wordbank/words?container=cet4&ctype=book&target=生词本")
        items = {item["uid"]: item for item in response.get_json()["items"]}
        self.assertTrue(items["u2"]["in_target"])
        self.assertFalse(items["u1"]["in_target"])

    def test_add_words_from_book(self):
        response = self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1", "u3"]})
        self.assertEqual(response.get_json()["added"], 2)
        db = self.app.extensions["db"].get("wordbooks")
        self.assertEqual(list(db.query('SELECT COUNT(*) AS c FROM "生词本"'))[0]["c"], 2)

    def test_add_words_ignores_duplicates(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1"]})
        response = self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1", "u2"]})
        self.assertEqual(response.get_json()["added"], 1)

    # ---------------------------------------------------------------- 删词
    def test_remove_words_from_wordbook(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1", "u2"]})
        db = self.app.extensions["db"].get("wordbooks")
        before = list(db.query('SELECT COUNT(*) AS c FROM "生词本"'))[0]["c"]
        self.assertEqual(before, 2)

        response = self.client.post("/api/wordbook/remove", json={"name": "生词本", "ctype": "wordbook", "uids": ["u1"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], 1)
        after = list(db.query('SELECT COUNT(*) AS c FROM "生词本"'))[0]["c"]
        self.assertEqual(after, 1)

    def test_remove_words_clears_task_rows(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1"]})
        # 造一条任务行
        tasks = self.app.extensions["db"].get("tasks")
        tasks["生词本__learning"].insert(
            {"uid": "u1", "word": "abandon", "translation": "放弃", "problem_state": "ready", "task_state": 0}
        )
        tasks.commit()
        self.assertEqual(list(tasks.query('SELECT COUNT(*) AS c FROM "生词本__learning"'))[0]["c"], 1)

        self.client.post("/api/wordbook/remove", json={"name": "生词本", "ctype": "wordbook", "uids": ["u1"]})
        self.assertEqual(list(tasks.query('SELECT COUNT(*) AS c FROM "生词本__learning"'))[0]["c"], 0)

    def test_remove_words_keeps_history(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1"]})
        history = self.app.extensions["db"].get("history")
        history["history"].insert(
            {"uid": "u1", "container": "生词本", "container_type": "wordbook", "answer_id": "a1", "kind": "learning"}
        )
        history.commit()
        self.client.post("/api/wordbook/remove", json={"name": "生词本", "ctype": "wordbook", "uids": ["u1"]})
        self.assertEqual(list(history.query("SELECT COUNT(*) AS c FROM history"))[0]["c"], 1)

    def test_remove_unknown_container(self):
        response = self.client.post("/api/wordbook/remove", json={"name": "不存在", "ctype": "wordbook", "uids": ["x"]})
        self.assertEqual(response.status_code, 400)

    # ---------------------------------------------------------------- 收藏
    def test_favorites_include_wordbooks(self):
        self.client.post("/api/wordbook/add", json={"target": "生词本", "uids": ["u1"]})
        self.client.post("/api/favorite", json={"uid": "u1", "container": "生词本", "ctype": "wordbook"})
        html = self.client.get("/me/favorites").get_data(as_text=True)
        self.assertIn("abandon", html)

    def test_favorite_word_only_in_wordbook_can_be_copied(self):
        """词本独有的词被收藏后，也能再加到另一个词本。"""
        self.client.post("/api/wordbook/append", json={
            "name": "生词本",
            "text": json.dumps({"data": [{"word": "zebra", "translation": "斑马"}]}),
            "ctype": "wordbook",
        })
        db = self.app.extensions["db"].get("wordbooks")
        zebra_uid = list(db.query('SELECT uid FROM "生词本" WHERE word = :w', w="zebra"))[0]["uid"]

        self.tm.create_container("wordbook", "第二个")
        response = self.client.post("/api/wordbook/add", json={"target": "第二个", "uids": [zebra_uid]})
        self.assertEqual(response.get_json()["added"], 1)
        self.assertEqual(
            [row["word"] for row in db.query('SELECT word FROM "第二个"')],
            ["zebra"],
        )

    # ---------------------------------------------------------------- 改名时的游标
    def test_rename_moves_preview_cursor(self):
        self.config.update("app.json", {"preview": {"生词本": {"index": 3}}})
        self.client.post("/api/wordbook/rename", json={"name": "生词本", "new_name": "改名了", "ctype": "wordbook"})
        preview = self.config.app.get("preview") or {}
        self.assertIn("改名了", preview)
        self.assertNotIn("生词本", preview)


if __name__ == "__main__":
    unittest.main()
