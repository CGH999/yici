"""数据层测试：建表幂等、中文表名、改名、任务表命名。"""

import tempfile
import unittest
from pathlib import Path

from app.db import schema
from app.db.manager import DBManager


class SchemaTest(unittest.TestCase):
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

    def test_create_and_idempotent(self):
        db = self.manager.get("library")
        schema.ensure_container_table(db, "cet4")
        schema.ensure_container_table(db, "cet4")  # 再跑一次不应报错、不重复加列
        columns = [row["name"] for row in db.query('PRAGMA table_info("cet4")')]
        self.assertEqual(columns.count("uid"), 1)
        self.assertIn("last_answer_id", columns)
        self.assertIn("previewed_at", columns)

    def test_chinese_and_space_names(self):
        db = self.manager.get("library")
        for name in ("考研词汇", "考研 词汇", "cet-4", "四级2025"):
            schema.ensure_container_table(db, name)
        self.assertEqual(sorted(schema.list_containers(db)), sorted(["考研词汇", "考研 词汇", "cet-4", "四级2025"]))

    def test_ordered_select(self):
        db = self.manager.get("library")
        table = schema.ensure_container_table(db, "cet4")
        for word in ("able", "abandon", "ability"):
            table.insert({"uid": word, "word": word, "state": "new"})
        rows = list(db.query('SELECT word FROM "cet4" WHERE state = :s ORDER BY id ASC LIMIT 2', s="new"))
        self.assertEqual([row["word"] for row in rows], ["able", "abandon"])

    def test_rename_and_drop(self):
        db = self.manager.get("library")
        schema.ensure_container_table(db, "cet4")
        schema.rename_container(db, "cet4", "CET4-old")
        self.assertIn("CET4-old", schema.list_containers(db))
        schema.drop_container(db, "CET4-old")
        self.assertNotIn("CET4-old", schema.list_containers(db))

    def test_name_validation(self):
        for bad in ("", "  ", 'a"b', "a/b", "a__b", "sqlite_x", "x" * 41):
            with self.assertRaises(ValueError, msg=bad):
                schema.validate_name(bad)
        self.assertEqual(schema.validate_name("  cet4  "), "cet4")

    def test_task_and_history_tables(self):
        tasks = self.manager.get("tasks")
        schema.ensure_container_table(self.manager.get("library"), "cet4")
        for kind in schema.TASK_KINDS:
            schema.ensure_task_table(tasks, "cet4", kind)
        self.assertEqual(
            sorted(tasks.tables),
            sorted(["cet4__learning", "cet4__review", "cet4__spelling"]),
        )
        spelling_cols = [row["name"] for row in tasks.query('PRAGMA table_info("cet4__spelling")')]
        self.assertIn("revealed", spelling_cols)
        self.assertNotIn("problem", spelling_cols)

        history = self.manager.get("history")
        schema.ensure_history_table(history)
        history_cols = [row["name"] for row in history.query('PRAGMA table_info("history")')]
        self.assertIn("container_type", history_cols)

    def test_indexes_created(self):
        db = self.manager.get("library")
        schema.ensure_container_table(db, "cet4")
        schema.ensure_indexes(db, "cet4")
        names = [row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='index'")]
        self.assertTrue(any("cet4" in name for name in names))


if __name__ == "__main__":
    unittest.main()

