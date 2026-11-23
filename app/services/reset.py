"""初始化：把用户数据清空，词书保留。

"用户数据"= 学习进度 / 任务 / 历史 / 笔记 / 收藏 / 预习记录 / AI 记忆。
词书本身（词条、释义）不动；API Key 与提示词属于配置，默认也不动，
需要彻底交给别人用时可以额外勾选清空 API Key。
"""

from __future__ import annotations

import json
import time

from app.db import schema

EMPTY_MEMORY = {"generate_sentence": {"history": []}, "check_answer": {"history": []}}

_WORD_RESET_SQL = """
UPDATE {table} SET
    state = 'new',
    difficulty = NULL,
    stability = NULL,
    last_review = NULL,
    due = NULL,
    reps = 0,
    lapses = 0,
    note = NULL,
    favorite = 0,
    previewed_at = NULL,
    last_answer_id = NULL,
    updated_at = :now
"""


def _count(db, table_name: str) -> int:
    rows = list(db.query(f"SELECT COUNT(*) AS c FROM {schema.quote(table_name)}"))
    return int(rows[0]["c"]) if rows else 0


def reset_user_data(manager, config, clear_api_key: bool = False) -> dict:
    counts = {"tasks": 0, "history": 0, "words": 0}
    tasks = manager.get("tasks")
    history = manager.get("history")
    now = int(time.time())

    with manager.write_lock:
        # 1. 任务表清空（表结构保留）
        for table_name in list(tasks.tables):
            if table_name.startswith("sqlite_"):
                continue
            counts["tasks"] += _count(tasks, table_name)
            tasks.query(f"DELETE FROM {schema.quote(table_name)}")
        tasks.commit()

        # 2. 历史清空
        counts["history"] = _count(history, "history")
        history.query("DELETE FROM history")
        history.commit()

        # 3. 词库/词本里每个词的进度清空
        for db_name in ("library", "wordbooks"):
            db = manager.get(db_name)
            for container in schema.list_containers(db):
                counts["words"] += 1
                db.query(_WORD_RESET_SQL.format(table=schema.quote(container)), now=now)
                db.commit()

    # 4. AI 记忆文件重置
    memory_path = config.memory_path
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    with open(memory_path, "w", encoding="utf-8") as handle:
        json.dump(EMPTY_MEMORY, handle, ensure_ascii=False, indent=2)

    # 5. 预习游标
    app_cfg = config.load("app.json")
    app_cfg["preview"] = {}
    config.save("app.json", app_cfg)

    # 6. 可选：清掉 API Key（分享给别人时用）
    if clear_api_key:
        ai_cfg = config.load("ai.json")
        ai_cfg["api_key"] = ""
        config.save("ai.json", ai_cfg)

    return counts
