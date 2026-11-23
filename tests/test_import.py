"""JSON 导入词书的单元测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from app.db import schema
from app.db.manager import DBManager
from app.services.wordbook_import import ImportError, import_container, parse_payload, uid_for


class UidTest(unittest.TestCase):
    def test_case_matters(self):
        self.assertNotEqual(uid_for("China"), uid_for("china"))

    def test_whitespace_ignored(self):
        self.assertEqual(uid_for(" China "), uid_for("China"))

    def test_stable(self):
        self.assertEqual(uid_for("ability"), uid_for("ability"))


class ParseTest(unittest.TestCase):
    def test_valid_with_data_key(self):
        rows = parse_payload(json.dumps({"data": [{"word": "I", "translation": "我"}]}))
        self.assertEqual(rows, [("I", "我")])

    def test_bare_array(self):
        rows = parse_payload(json.dumps([{"word": "one", "translation": "一"}]))
        self.assertEqual(rows, [("one", "一")])

    def test_missing_translation_becomes_empty(self):
        rows = parse_payload(json.dumps({"data": [{"word": "hi"}]}))
        self.assertEqual(rows, [("hi", "")])

    def test_invalid_json(self):
        with self.assertRaises(ImportError) as ctx:
            parse_payload('{"data": [}')
        self.assertIn("JSON 格式错误", str(ctx.exception))

    def test_missing_data_key(self):
        with self.assertRaises(ImportError):
            parse_payload(json.dumps({"words": []}))

    def test_empty_array(self):
        with self.assertRaises(ImportError):
            parse_payload(json.dumps({"data": []}))

    def test_item_not_object(self):
        with self.assertRaises(ImportError):
            parse_payload(json.dumps({"data": ["abc"]}))

    def test_missing_word(self):
        with self.assertRaises(ImportError) as ctx:
            parse_payload(json.dumps({"data": [{"translation": "无"}]}))
        self.assertIn("缺少 word", str(ctx.exception))

    def test_blank_text(self):
        with self.assertRaises(ImportError):
            parse_payload("   ")


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manager = DBManager(
            {
                "library": base / "library.db",
                "wordbooks": base / "wordbooks.db",
                "tasks": base / "tasks.db",
                "history": base / "user" / "history.db",
            }
        )

    def tearDown(self):
        self.manager.close_all()
        self.tmp.cleanup()

    def test_import_creates_table(self):
        rows = parse_payload(json.dumps({"data": [{"word": "I", "translation": "我"}, {"word": "one", "translation": "一"}]}))
        result = import_container(self.manager, "cet6", "book", rows)
        self.assertEqual(result["inserted"], 2)

        db = self.manager.get("library")
        self.assertIn("cet6", schema.list_containers(db))
        words = [row["word"] for row in db.query('SELECT word FROM "cet6" ORDER BY id')]
        self.assertEqual(words, ["I", "one"])
        first = dict(db.query('SELECT * FROM "cet6" LIMIT 1').next()) if False else list(
            db.query('SELECT * FROM "cet6" LIMIT 1')
        )[0]
        self.assertEqual(first["state"], "new")
        self.assertTrue(first["uid"])

    def test_only_exact_duplicates_are_skipped(self):
        """只跳过完全相同的词；大小写不同算不同的词。"""
        rows = parse_payload(json.dumps({"data": [{"word": "I"}, {"word": "I "}, {"word": "i"}]}))
        result = import_container(self.manager, "dup", "book", rows)
        self.assertEqual(result["inserted"], 2)   # I 和 i
        self.assertEqual(result["skipped"], 1)    # "I " 和 "I" 相同

    def test_case_sensitive_words_get_different_uid(self):
        rows = parse_payload(
            json.dumps({"data": [{"word": "China", "translation": "中国"}, {"word": "china", "translation": "瓷器"}]})
        )
        result = import_container(self.manager, "case", "book", rows)
        self.assertEqual(result["inserted"], 2)
        db = self.manager.get("library")
        uids = [row["uid"] for row in db.query('SELECT uid FROM "case" ORDER BY id')]
        self.assertEqual(len(set(uids)), 2)

    def test_duplicate_name_rejected(self):
        rows = parse_payload(json.dumps({"data": [{"word": "a"}]}))
        import_container(self.manager, "cet6", "book", rows)
        with self.assertRaises(ImportError):
            import_container(self.manager, "CET6", "book", rows)

    def test_same_word_same_uid(self):
        rows = parse_payload(json.dumps({"data": [{"word": "Ability"}]}))
        import_container(self.manager, "book_a", "book", rows)
        import_container(self.manager, "book_b", "book", rows)
        db = self.manager.get("library")
        uid_a = list(db.query('SELECT uid FROM "book_a"'))[0]["uid"]
        uid_b = list(db.query('SELECT uid FROM "book_b"'))[0]["uid"]
        self.assertEqual(uid_a, uid_b)

    def test_bad_name_rejected(self):
        rows = parse_payload(json.dumps({"data": [{"word": "a"}]}))
        with self.assertRaises(ValueError):
            import_container(self.manager, "bad__name", "book", rows)


if __name__ == "__main__":
    unittest.main()
