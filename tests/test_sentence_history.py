"""例句历史：记录、上限、注入提示词。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.db import schema
from app.db.manager import DBManager
from app.services.ai_client import AIClient
from app.services.task_manager import TaskManager, _parse_sentences


class ParseSentencesTest(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(_parse_sentences('["a","b"]'), ["a", "b"])

    def test_bad_json(self):
        self.assertEqual(_parse_sentences("{oops"), [])

    def test_not_list(self):
        self.assertEqual(_parse_sentences('{"a":1}'), [])

    def test_limit_keeps_tail(self):
        self.assertEqual(_parse_sentences('["1","2","3"]', limit=2), ["2", "3"])


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.config = Config(base)
        self.config.bootstrap()
        self.config.update("app.json", {"sentence_history": 2, "proficiency_days": 365})
        self.manager = DBManager.from_config(self.config)
        self.tm = TaskManager(self.manager, self.config)

        db = self.manager.get("library")
        schema.ensure_container_table(db, "cet4")
        schema.ensure_indexes(db, "cet4")
        db["cet4"].insert({"uid": "u1", "word": "abandon", "translation": "放弃", "state": "new", "reps": 0, "lapses": 0})
        db.commit()
        for kind in ("learning", "review"):
            schema.ensure_task_table(self.manager.get("tasks"), "cet4", kind)

    def tearDown(self):
        self.manager.close_all()
        self.tmp.cleanup()

    def test_set_problem_records_sentence(self):
        self.tm.set_problem("cet4", "learning", "u1", {"sentence": "They abandon the car.", "translation": "他们弃车。"})
        word = self.tm.get_word("cet4", "book", "u1")
        self.assertEqual(_parse_sentences(word["sentences"]), ["They abandon the car."])

    def test_history_is_capped(self):
        for sentence in ("One.", "Two.", "Three."):
            self.tm.set_problem("cet4", "learning", "u1", {"sentence": sentence, "translation": ""})
        word = self.tm.get_word("cet4", "book", "u1")
        self.assertEqual(_parse_sentences(word["sentences"]), ["Two.", "Three."])

    def test_duplicate_sentence_is_not_repeated(self):
        for _ in range(3):
            self.tm.set_problem("cet4", "learning", "u1", {"sentence": "Same.", "translation": ""})
        word = self.tm.get_word("cet4", "book", "u1")
        self.assertEqual(_parse_sentences(word["sentences"]), ["Same."])

    def test_zero_limit_disables_history(self):
        self.config.update("app.json", {"sentence_history": 0})
        self.tm.set_problem("cet4", "learning", "u1", {"sentence": "Nope.", "translation": ""})
        word = self.tm.get_word("cet4", "book", "u1")
        self.assertEqual(_parse_sentences(word["sentences"]), [])

    def test_pending_problems_carry_history(self):
        self.tm.set_problem("cet4", "learning", "u1", {"sentence": "Old one.", "translation": ""})
        self.manager.get("tasks")["cet4__learning"].insert(
            {"uid": "u1", "word": "abandon", "translation": "放弃", "problem_state": "pending", "task_state": 0}
        )
        self.manager.get("tasks").commit()
        items = self.tm.pending_problems("cet4", "learning", "book")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["recent_sentences"], ["Old one."])

    def test_task_row_stores_the_avoid_list(self):
        self.manager.get("tasks")["cet4__learning"].insert(
            {"uid": "u1", "word": "abandon", "translation": "放弃", "problem_state": "pending", "task_state": 0}
        )
        self.manager.get("tasks").commit()
        problem = {"sentence": "New one.", "translation": "", "avoid_sentences": ["Old one."]}
        self.tm.set_problem("cet4", "learning", "u1", problem, ctype="book")
        row = list(self.manager.get("tasks").query('SELECT * FROM "cet4__learning" WHERE uid = :u', u="u1"))[0]
        self.assertEqual(json.loads(row["recent_sentences"]), ["Old one."])
        self.assertNotIn("avoid_sentences", json.loads(row["problem"]))


class PromptPayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name))
        self.config.bootstrap()
        self.client = AIClient(self.config)
        self.captured = {}

        def fake_chat(section, payload):
            self.captured["section"] = section
            self.captured["payload"] = payload
            return (
                json.dumps({"sentence": "The crew abandoned the ship.", "translation": "船员弃船。", "keyword_form": "abandoned"}),
                {},
            )

        self.client._chat = fake_chat

    def tearDown(self):
        self.tmp.cleanup()

    def test_payload_contains_avoid_list(self):
        result = self.client.generate_sentence("abandon", "放弃", ["They abandon the car.", "Don't abandon it."])
        payload = json.loads(self.captured["payload"])
        self.assertEqual(payload["word"], "abandon")
        self.assertEqual(payload["translation"], "放弃")
        self.assertEqual(payload["avoid_sentences"], ["They abandon the car.", "Don't abandon it."])
        self.assertEqual(result["sentence"], "The crew abandoned the ship.")
        self.assertEqual(result["keyword_form"], "abandoned")
        self.assertEqual(result["avoid_sentences"], ["They abandon the car.", "Don't abandon it."])

    def test_payload_without_history(self):
        self.client.generate_sentence("county", None, None)
        payload = json.loads(self.captured["payload"])
        self.assertEqual(payload["avoid_sentences"], [])
        self.assertEqual(payload["translation"], "")


if __name__ == "__main__":
    unittest.main()
