"""从 JSON 文本导入词书。

约定的格式（也接受最外层直接是数组）::

    {"data": [{"word": "I", "translation": "我"}, ...]}

解析和写库分开：解析失败不碰数据库；写库失败会把建出来的表删掉回滚。
"""

from __future__ import annotations

import json
import time
import uuid

from app.db import schema

MAX_TEXT_BYTES = 8 * 1024 * 1024      # 8 MB，约 5 万词
MAX_ITEMS = 50_000
MAX_WORD_LENGTH = 80
MAX_TRANSLATION_LENGTH = 500


class ImportError(ValueError):
    """导入数据不合法（消息直接给用户看）。"""


def uid_for(word: str) -> str:
    """词的唯一 id。**区分大小写**：China 和 china 是两个不同的词。

    只去掉首尾空白，不做大小写折叠。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "word:" + word.strip()))


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def parse_payload(text: str) -> list[tuple[str, str]]:
    """把 JSON 文本解析成 [(word, translation), ...]。"""
    if not text or not text.strip():
        raise ImportError("内容为空，请粘贴 JSON")

    size = len(text.encode("utf-8"))
    if size > MAX_TEXT_BYTES:
        raise ImportError(f"内容太大（{size / 1024 / 1024:.1f} MB），上限 8 MB")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError(f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列附近 — {exc.msg}") from None

    if isinstance(data, dict):
        if "data" not in data:
            raise ImportError('JSON 里缺少 "data" 字段')
        items = data["data"]
    elif isinstance(data, list):
        items = data
    else:
        raise ImportError('最外层必须是 {"data": [...]} 这样的对象，或者直接是数组')

    if not isinstance(items, list):
        raise ImportError('"data" 必须是一个数组')
    if not items:
        raise ImportError("数组是空的，没有词可以导入")
    if len(items) > MAX_ITEMS:
        raise ImportError(f"条目太多（{len(items)}），一次最多 {MAX_ITEMS} 条")

    rows: list[tuple[str, str]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ImportError(f"第 {index} 条不是对象，应该是 {{\"word\": ..., \"translation\": ...}}")
        word = _as_text(item.get("word"))
        if not word:
            raise ImportError(f"第 {index} 条缺少 word")
        if len(word) > MAX_WORD_LENGTH:
            raise ImportError(f"第 {index} 条的 word 太长（超过 {MAX_WORD_LENGTH} 字符）")
        translation = _as_text(item.get("translation"))[:MAX_TRANSLATION_LENGTH]
        rows.append((word, translation))

    return rows


def import_container(
    manager, name: str, ctype: str, rows: list[tuple[str, str]], append: bool = False
) -> dict:
    """建表并写入词条；失败会回滚（把建的表删掉）。

    ``append=True`` 时是"往已有词本里追加"：不新建表，已存在的词（按 uid）跳过。
    """
    if ctype not in ("book", "wordbook"):
        raise ImportError("未知的容器类型")
    name = schema.validate_name(name)

    db = manager.get("library" if ctype == "book" else "wordbooks")
    other_db = manager.get("wordbooks" if ctype == "book" else "library")
    existing_containers = {existing.lower() for existing in schema.list_containers(db)}
    # 任务表按容器名命名，词书和词本共用，所以名字要全局唯一
    other_names = {existing.lower() for existing in schema.list_containers(other_db)}
    is_existing = name.lower() in existing_containers
    if append and not is_existing:
        raise ImportError(f"找不到这个词本：{name}")
    if not append and (is_existing or name.lower() in other_names):
        raise ImportError(f"已经存在同名容器（词书或词本）：{name}")

    table = schema.ensure_container_table(db, name)
    existing_uids: set[str] = set()
    if is_existing:
        existing_uids = {row["uid"] for row in db.query(f"SELECT uid FROM {schema.quote(name)}")}

    now = int(time.time())
    inserted = 0
    skipped = 0
    already = 0
    seen: set[str] = set()

    try:
        with manager.write_lock:
            for word, translation in rows:
                # 只跳过"完全一样"的词；大小写不同视为不同的词（China / china）
                if word in seen:
                    skipped += 1
                    continue
                seen.add(word)
                uid = uid_for(word)
                if uid in existing_uids:
                    already += 1
                    continue
                table.insert(
                    {
                        "uid": uid,
                        "word": word,
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
                existing_uids.add(uid)
                inserted += 1
        schema.ensure_indexes(db, name)
    except Exception:
        if not is_existing:
            try:
                schema.drop_container(db, name)
            except Exception:
                pass
        raise

    return {
        "name": name,
        "ctype": ctype,
        "inserted": inserted,
        "skipped": skipped,
        "already": already,
    }
