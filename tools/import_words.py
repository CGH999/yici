"""词书导入：旧 cet4.db / CSV → 新词库。

旧库只取 ``word`` 与 ``translation`` 两列，其余字段全部丢弃（按你的要求重建结构），
新行统一 ``state='new'``、D/S/due 为空、reps/lapses 归零。

用法::

    python tools/import_words.py --source "D:/path/cet4.db" --name cet4
    python tools/import_words.py --source words.csv --name mywords
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import dataset  # noqa: E402

from app.config import Config  # noqa: E402
from app.db import schema  # noqa: E402
from app.db.manager import DBManager  # noqa: E402


def uid_for(word: str) -> str:
    """同一个词永远得到同一个 uid（跨词书、跨词本一致）。

    注意：**区分大小写**——China 和 china 是两个不同的词，uid 也不同。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "word:" + word.strip()))


def load_from_sqlite(path: Path, table: str = "word_bank") -> list[tuple[str, str]]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(f'SELECT word, translation FROM "{table}"').fetchall()
    finally:
        connection.close()
    return [(str(w or ""), str(t or "")) for w, t in rows if w]


def load_from_csv(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return rows
        fieldnames = {name.strip().lower(): name for name in reader.fieldnames}
        word_key = fieldnames.get("word") or fieldnames.get("单词")
        translation_key = fieldnames.get("translation") or fieldnames.get("释义")
        if not word_key or not translation_key:
            raise SystemExit("CSV 需要 word/单词 与 translation/释义 两列")
        for row in reader:
            word = (row.get(word_key) or "").strip()
            if word:
                rows.append((word, (row.get(translation_key) or "").strip()))
    return rows


def import_into(db: dataset.Database, name: str, rows: list[tuple[str, str]]) -> dict:
    table = schema.ensure_container_table(db, name)
    now = int(time.time())
    inserted = skipped = 0
    seen: set[str] = set()
    for word, translation in rows:
        key = word.strip().lower()
        if not key or key in seen:
            skipped += 1
            continue
        seen.add(key)
        table.insert(
            {
                "uid": uid_for(word),
                "word": word.strip(),
                "translation": translation,
                "phonetic": None,
                "state": "new",
                "difficulty": None,
                "stability": None,
                "last_review": None,
                "due": None,
                "reps": 0,
                "lapses": 0,
                "note": None,
                "favorite": False,
                "previewed_at": None,
                "last_answer_id": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        inserted += 1
    schema.ensure_indexes(db, name)
    return {"inserted": inserted, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入词书")
    parser.add_argument("--source", required=True, help="旧 .db 或 .csv 路径")
    parser.add_argument("--name", required=True, help="词书名（= 表名）")
    parser.add_argument("--table", default="word_bank", help="旧库中的表名")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        print(f"source not found: {source}")
        return 2

    rows = load_from_csv(source) if source.suffix.lower() == ".csv" else load_from_sqlite(source, args.table)

    config = Config(PROJECT_ROOT)
    config.ensure_dirs()
    manager = DBManager.from_config(config)
    db = manager.get("library")
    try:
        name = schema.validate_name(args.name)
        if name.lower() in {existing.lower() for existing in schema.list_containers(db)} and "--replace" not in (argv or []):
            print(f"table already exists: {name} (drop it first or use a new name)")
            return 3
        result = import_into(db, name, rows)
    finally:
        manager.close_all()

    print(f"table={name} inserted={result['inserted']} skipped={result['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
