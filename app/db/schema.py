"""建表 / 补列 / 容器命名。

结构要点（与 DESIGN.md §4 对应）：

* 词库、词本库都是「单文件多表」，**表名就是容器名**（词书名 / 词本名）
* 任务库是「单文件多表」，表名 ``<容器名>__<类型>``，类型为 learning / review / spelling
* 建表与补列必须幂等：``dataset`` 的 ``create_column`` 会对比现有结构，只补缺失列
* 给容器加字段时**必须遍历所有容器表**逐张补——这是单文件多表的固定成本
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import dataset

# (列名, dataset 类型名)
CONTAINER_COLUMNS: list[tuple[str, str]] = [
    ("uid", "text"),
    ("word", "text"),
    ("translation", "text"),
    ("phonetic", "text"),
    ("state", "text"),
    ("difficulty", "float"),
    ("stability", "float"),
    ("last_review", "float"),
    ("due", "integer"),
    ("reps", "integer"),
    ("lapses", "integer"),
    ("note", "text"),
    ("favorite", "boolean"),
    ("previewed_at", "integer"),
    ("last_answer_id", "text"),
    ("sentences", "text"),
    ("created_at", "integer"),
    ("updated_at", "integer"),
]

TASK_COLUMNS: list[tuple[str, str]] = [
    ("uid", "text"),
    ("word", "text"),
    ("translation", "text"),
    ("phonetic", "text"),
    ("difficulty", "float"),
    ("stability", "float"),
    ("state", "text"),
    ("problem", "text"),
    ("problem_state", "text"),
    ("problem_error", "text"),
    ("task_state", "integer"),
    ("first_pass", "boolean"),
    ("error_count", "integer"),
    ("served_seq", "integer"),
    ("answer", "text"),
    ("recent_sentences", "text"),
    ("sentence_score", "float"),
    ("keyword_score", "float"),
    ("note", "text"),
    ("applied", "boolean"),
    ("batch_id", "text"),
    ("created_at", "integer"),
    ("updated_at", "integer"),
]

SPELLING_COLUMNS: list[tuple[str, str]] = [
    ("uid", "text"),
    ("word", "text"),
    ("translation", "text"),
    ("phonetic", "text"),
    ("task_state", "integer"),
    ("first_pass", "boolean"),
    ("error_count", "integer"),
    ("revealed", "boolean"),
    ("batch_id", "text"),
    ("served_seq", "integer"),
    ("created_at", "integer"),
    ("updated_at", "integer"),
]

HISTORY_COLUMNS: list[tuple[str, str]] = [
    ("answer_id", "text"),
    ("uid", "text"),
    ("container", "text"),
    ("container_type", "text"),
    ("task_type", "text"),
    ("time", "integer"),
    ("first_pass", "boolean"),
    ("error_count", "integer"),
    ("sentence_score", "float"),
    ("keyword_score", "float"),
]

TASK_KINDS = ("learning", "review", "spelling")
SOURCE_KINDS = ("learning", "review")

_FORBIDDEN_CHARS = set('"\'\\/:*?<>|.\n\r\t')
MAX_NAME_LENGTH = 40


def quote(identifier: str) -> str:
    """把标识符包成 SQL 双引号形式。表名允许中文和空格。"""
    return '"' + identifier.replace('"', '""') + '"'


def validate_name(name: str) -> str:
    """校验容器名，返回清洗后的名字；不合法抛 ValueError。"""
    if not isinstance(name, str):
        raise ValueError("名字必须是字符串")
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("名字不能为空")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValueError(f"名字最长 {MAX_NAME_LENGTH} 个字符")
    bad = sorted({ch for ch in cleaned if ch in _FORBIDDEN_CHARS})
    if bad:
        raise ValueError(f"名字不能包含这些字符：{' '.join(bad)}")
    if cleaned.startswith("sqlite_"):
        raise ValueError("名字不能以 sqlite_ 开头")
    if "__" in cleaned:
        raise ValueError("名字不能包含连续两个下划线（会与任务表命名冲突）")
    return cleaned


def _create_columns(table: dataset.Table, columns: Iterable[tuple[str, str]], types: Any) -> None:
    for name, type_name in columns:
        table.create_column(name, getattr(types, type_name))


def ensure_container_table(db: dataset.Database, name: str) -> dataset.Table:
    name = validate_name(name)
    table = db.create_table(name, primary_id="id", primary_type=db.types.integer)
    _create_columns(table, CONTAINER_COLUMNS, db.types)
    return table


def ensure_task_table(tasks: dataset.Database, name: str, kind: str) -> dataset.Table:
    if kind not in TASK_KINDS:
        raise ValueError(f"未知任务类型 {kind!r}")
    table = tasks.create_table(task_table_name(name, kind), primary_id="id", primary_type=tasks.types.integer)
    if kind == "spelling":
        _create_columns(table, SPELLING_COLUMNS, tasks.types)
    else:
        _create_columns(table, TASK_COLUMNS, tasks.types)
    return table


def ensure_history_table(history: dataset.Database) -> dataset.Table:
    table = history.create_table("history", primary_id="id", primary_type=history.types.integer)
    _create_columns(table, HISTORY_COLUMNS, history.types)
    return table


def ensure_indexes(db: dataset.Database, table_name: str) -> None:
    quoted = quote(table_name)
    short = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5]", "_", table_name)
    columns = [row["name"] for row in db.query(f"PRAGMA table_info({quoted})")]
    for suffix, cols in (("uid", ["uid"]), ("state_due", ["state", "due"]), ("favorite", ["favorite"])):
        if not all(col in columns for col in cols):
            continue
        index_name = quote(f"idx_{short}_{suffix}")
        col_sql = ", ".join(quote(col) for col in cols)
        db.query(f"CREATE INDEX IF NOT EXISTS {index_name} ON {quoted} ({col_sql})")
    # dataset 的 db.query 不会自动提交，裸 DML/DDL 必须显式 commit
    db.commit()


def ensure_history_index(history: dataset.Database) -> None:
    history.query("CREATE UNIQUE INDEX IF NOT EXISTS idx_history_answer_id ON history (answer_id)")
    history.commit()


def list_containers(db: dataset.Database) -> list[str]:
    """库里所有容器名（过滤 SQLite 内部表）。"""
    names = []
    for name in db.tables:
        if name.startswith("sqlite_"):
            continue
        names.append(name)
    return sorted(names)


def task_table_name(container: str, kind: str) -> str:
    return f"{container}__{kind}"


def drop_container(db: dataset.Database, name: str) -> None:
    db.query(f"DROP TABLE IF EXISTS {quote(name)}")
    db.commit()


def rename_container(db: dataset.Database, old: str, new: str) -> None:
    new = validate_name(new)
    if new == old:
        return
    if new in set(list_containers(db)):
        raise ValueError(f"已经存在同名表 {new!r}")
    db.query(f'ALTER TABLE {quote(old)} RENAME TO {quote(new)}')
    db.commit()


def ensure_library(manager, config) -> None:
    """启动时把四个库的结构补齐（幂等）。"""
    library = manager.get("library")
    wordbooks = manager.get("wordbooks")
    tasks = manager.get("tasks")
    history = manager.get("history")

    ensure_history_table(history)
    ensure_history_index(history)

    containers: list[tuple[dataset.Database, str]] = []
    for db in (library, wordbooks):
        for name in list_containers(db):
            ensure_container_table(db, name)
            ensure_indexes(db, name)
            containers.append((db, name))

    for _, name in containers:
        for kind in TASK_KINDS:
            ensure_task_table(tasks, name, kind)
