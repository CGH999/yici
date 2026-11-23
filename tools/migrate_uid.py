"""一次性迁移：把 uid 从 uuid5(word.lower()) 改成 uuid5(word)（区分大小写）。

背景：原来的算法对词做了大小写折叠，导致 China 和 china 拿到同一个 uid。
改成区分大小写之后，历史上按旧算法生成的 uid 需要重算，同时要同步
任务库和 history 里引用这些 uid 的行。

no-op 重跑安全：已经正确的行不会被动。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.db import schema  # noqa: E402
from app.db.manager import DBManager  # noqa: E402


def uid_for(word: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "word:" + word.strip()))


def main() -> int:
    config = Config()
    manager = DBManager.from_config(config)
    library = manager.get("library")
    wordbooks = manager.get("wordbooks")
    tasks = manager.get("tasks")
    history = manager.get("history")

    # 每个容器里 旧uid -> 新uid
    container_maps: dict[str, dict[str, str]] = {}
    changed_total = 0

    for label, db in (("词库", library), ("词本", wordbooks)):
        for container in schema.list_containers(db):
            table = db[container]
            rows = list(db.query(f'SELECT id, uid, word FROM {schema.quote(container)}'))
            mapping: dict[str, str] = {}
            for row in rows:
                new_uid = uid_for(row["word"])
                if new_uid != row["uid"]:
                    mapping[row["uid"]] = new_uid
                    table.update({"id": row["id"], "uid": new_uid}, keys=["id"])
            db.commit()
            if mapping:
                container_maps[container] = mapping
                changed_total += len(mapping)
                print(f"{label} {container}: 更新 {len(mapping)} 条 uid")

    # 任务库
    task_updated = 0
    for table_name in list(tasks.tables):
        if table_name.startswith("sqlite_"):
            continue
        container, _, _kind = table_name.rpartition("__")
        mapping = container_maps.get(container)
        if not mapping:
            continue
        quoted = schema.quote(table_name)
        for old_uid, new_uid in mapping.items():
            rows = list(tasks.query(f"SELECT COUNT(*) AS c FROM {quoted} WHERE uid = :uid", uid=old_uid))
            count = rows[0]["c"] if rows else 0
            if count:
                tasks.query(f"UPDATE {quoted} SET uid = :new WHERE uid = :old", new=new_uid, old=old_uid)
                task_updated += count
        tasks.commit()

    # 历史
    history_updated = 0
    all_pairs = {old: new for mapping in container_maps.values() for old, new in mapping.items()}
    for old_uid, new_uid in all_pairs.items():
        rows = list(history.query("SELECT COUNT(*) AS c FROM history WHERE uid = :uid", uid=old_uid))
        count = rows[0]["c"] if rows else 0
        if count:
            history.query("UPDATE history SET uid = :new WHERE uid = :old", new=new_uid, old=old_uid)
            history_updated += count
    history.commit()

    print(f"完成：词条 {changed_total} 条，任务行 {task_updated} 条，历史 {history_updated} 条")
    manager.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
