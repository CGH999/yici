"""任务模块：只做数据库，不碰 AI、不碰 Flask。

职责边界：

* 建任务时把词从容器表「快照」复制到任务表
* 维护 task_state 状态机（-1 / 0 / 1）
* 一个词完成时，把 D/S/due 写回容器表、写一条 history
* 拼写队列（不参与 FSRS）
* 中断恢复所需的「未完成任务」扫描

幂等：每个任务行的 ``answer_id`` 由 ``(容器, 类型, 任务行 id)`` 派生为 uuid5，
写回词库前比对词库的 ``last_answer_id``，history 以 ``answer_id`` 去重。
所以任何一步中断后重放都不会重复计 D/S 或重复写历史。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable

from app.db import schema
from app.services import clock, scheduler

CONTAINER_TYPES = ("book", "wordbook")
CONTAINER_LABEL = {"book": "词书", "wordbook": "词本"}
CONTAINER_TABLE_LABEL = {"book": "learning", "wordbook": "learning"}

_ANSWER_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


class TaskError(RuntimeError):
    """任务模块的基类异常。"""


class ContainerNotFound(TaskError):
    pass


class TaskRowNotFound(TaskError):
    pass


def _uid_answer_id(container: str, ctype: str, kind: str, row_id: Any, batch_id: Any = None) -> str:
    """同一个任务行永远得到同一个 id，重放时用它去重。

    **必须带 batch_id**：``create_task`` 每次都清空任务表，SQLite 会从 1 重新分配 rowid，
    只用 row_id 的话新一轮会和上一轮撞车——作答会被当成"已经结算过"直接丢掉。
    """
    batch = batch_id or "-"
    return str(uuid.uuid5(_ANSWER_NAMESPACE, f"{ctype}|{container}|{kind}|{batch}|{row_id}"))


def _int(value: Any, default: int = 0) -> int:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _parse_sentences(raw: Any, limit: int = 10) -> list[str]:
    """把词表里存的 JSON 数组解析成句子列表；坏数据一律当空。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    items = [str(item).strip() for item in data if isinstance(item, str) and str(item).strip()]
    return items[-limit:] if limit > 0 else []


class TaskManager:
    def __init__(self, manager, config) -> None:
        self.manager = manager
        self.config = config

    # ------------------------------------------------------------------ 基础
    @property
    def library(self):
        return self.manager.get("library")

    @property
    def wordbooks(self):
        return self.manager.get("wordbooks")

    @property
    def tasks(self):
        return self.manager.get("tasks")

    @property
    def history(self):
        return self.manager.get("history")

    def _db(self, ctype: str):
        if ctype == "book":
            return self.library
        if ctype == "wordbook":
            return self.wordbooks
        raise ValueError(f"未知容器类型 {ctype!r}")

    def _now(self) -> float:
        return clock.now_ts(self.config.timezone())

    def _proficiency_days(self) -> int:
        return self.config.intake_limits()["proficiency_days"]

    def _task_sql(self, container: str, kind: str) -> str:
        return schema.quote(schema.task_table_name(container, kind))

    def _container_sql(self, container: str) -> str:
        return schema.quote(container)

    def _scalar(self, db, sql: str, default: int = 0, **params) -> int:
        rows = list(db.query(sql, **params))
        if not rows:
            return default
        return _int(list(rows[0].values())[0], default)

    # ------------------------------------------------------------------ 容器
    def containers(self, ctype: str = "book") -> list[str]:
        return schema.list_containers(self._db(ctype))

    def container_exists(self, container: str, ctype: str = "book") -> bool:
        return container in self.containers(ctype)

    def _name_taken(self, name: str) -> bool:
        """词书和词本共用一个本次任务库，任务表按容器名命名，所以名字必须全局唯一。"""
        wanted = name.lower()
        return any(
            wanted == existing.lower()
            for ctype in ("book", "wordbook")
            for existing in self.containers(ctype)
        )

    def create_container(self, ctype: str, name: str) -> str:
        name = schema.validate_name(name)
        db = self._db(ctype)
        if self._name_taken(name):
            raise ValueError(f"已经存在同名容器（词书或词本）：{name}")
        schema.ensure_container_table(db, name)
        schema.ensure_indexes(db, name)
        for kind in schema.TASK_KINDS:
            schema.ensure_task_table(self.tasks, name, kind)
        return name

    def drop_container(self, ctype: str, name: str) -> None:
        with self.manager.write_lock:
            for kind in schema.TASK_KINDS:
                self.tasks.query(f"DROP TABLE IF EXISTS {schema.quote(schema.task_table_name(name, kind))}")
            self.tasks.commit()
            schema.drop_container(self._db(ctype), name)
            self.history.query(
                "DELETE FROM history WHERE container = :name AND container_type = :ctype",
                name=name,
                ctype=ctype,
            )
            self.history.commit()
            current, current_type = self.config.current_container()
            if current == name and current_type == ctype:
                self.config.update("app.json", {"current_container": "", "current_container_type": "book"})
            self._drop_preview_cursor(name)

    def rename_container(self, ctype: str, old: str, new: str) -> str:
        new = schema.validate_name(new)
        db = self._db(ctype)
        if new.lower() != old.lower() and self._name_taken(new):
            raise ValueError(f"已经存在同名容器（词书或词本）：{new}")
        with self.manager.write_lock:
            schema.rename_container(db, old, new)
            for kind in schema.TASK_KINDS:
                old_table = schema.quote(schema.task_table_name(old, kind))
                new_table = schema.quote(schema.task_table_name(new, kind))
                self.tasks.query(f"ALTER TABLE {old_table} RENAME TO {new_table}")
            self.tasks.commit()
            self.history.query(
                "UPDATE history SET container = :new WHERE container = :old AND container_type = :ctype",
                new=new,
                old=old,
                ctype=ctype,
            )
            self.history.commit()
            current, current_type = self.config.current_container()
            if current == old and current_type == ctype:
                self.config.update("app.json", {"current_container": new})
            self._move_preview_cursor(old, new)
        return new

    def _move_preview_cursor(self, old: str, new: str) -> None:
        """预习游标按容器名存，改名时一起搬过去（深度合并删不掉旧键，所以整份写回）。"""
        data = self.config.load("app.json")
        preview = dict(data.get("preview") or {})
        if old not in preview:
            return
        preview[new] = preview.pop(old)
        data["preview"] = preview
        self.config.save("app.json", data)

    def _drop_preview_cursor(self, container: str) -> None:
        data = self.config.load("app.json")
        preview = dict(data.get("preview") or {})
        if container not in preview:
            return
        preview.pop(container, None)
        data["preview"] = preview
        self.config.save("app.json", data)

    def container_stats(self, container: str, ctype: str = "book") -> dict:
        db = self._db(ctype)
        if not self.container_exists(container, ctype):
            raise ContainerNotFound(container)
        table = self._container_sql(container)
        now = self._now()
        eod = clock.end_of_today_ts(self.config.timezone(), now)
        mastered_before = now + self._proficiency_days() * clock.DAY_SECONDS
        total = self._scalar(db, f"SELECT COUNT(*) AS c FROM {table}")
        new = self._scalar(db, f"SELECT COUNT(*) AS c FROM {table} WHERE state = :s", s="new")
        review = self._scalar(db, f"SELECT COUNT(*) AS c FROM {table} WHERE state = :s", s="review")
        mastered = self._scalar(
            db,
            f"SELECT COUNT(*) AS c FROM {table} WHERE state = :s AND due > :t",
            s="review",
            t=mastered_before,
        )
        due_today = self._scalar(
            db,
            f"SELECT COUNT(*) AS c FROM {table} WHERE state = :s AND due IS NOT NULL AND due <= :eod",
            s="review",
            eod=eod,
        )
        previewed = self._scalar(db, f"SELECT COUNT(*) AS c FROM {table} WHERE previewed_at IS NOT NULL")
        favorited = self._scalar(db, f"SELECT COUNT(*) AS c FROM {table} WHERE favorite = 1")
        noted = self._scalar(
            db, f"SELECT COUNT(*) AS c FROM {table} WHERE note IS NOT NULL AND TRIM(note) != ''"
        )
        return {
            "container": container,
            "ctype": ctype,
            "total": total,
            "new": new,
            "review": max(0, review - mastered),
            "mastered": mastered,
            "due_today": due_today,
            "previewed": previewed,
            "favorited": favorited,
            "noted": noted,
        }

    def words(
        self,
        container: str,
        ctype: str = "book",
        state: str = "all",
        keyword: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        db = self._db(ctype)
        if not self.container_exists(container, ctype):
            raise ContainerNotFound(container)
        table = self._container_sql(container)
        now = self._now()
        mastered_before = now + self._proficiency_days() * clock.DAY_SECONDS
        eod = clock.end_of_today_ts(self.config.timezone(), now)
        where: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(1000, int(limit))), "offset": max(0, int(offset))}
        if state == "new":
            where.append("state = 'new'")
        elif state == "review":
            where.append("state = 'review' AND (due IS NULL OR due <= :t)")
            params["t"] = mastered_before
        elif state == "mastered":
            where.append("state = 'review' AND due > :t")
            params["t"] = mastered_before
        elif state == "due":
            where.append("state = 'review' AND due IS NOT NULL AND due <= :eod")
            params["eod"] = eod
        elif state == "favorite":
            where.append("favorite = 1")
        if keyword:
            where.append("(word LIKE :kw OR translation LIKE :kw)")
            params["kw"] = f"%{keyword}%"
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC LIMIT :limit OFFSET :offset"
        rows = [dict(row) for row in db.query(sql, **params)]
        for row in rows:
            row["learn_state"] = clock.learn_state(row.get("state"), row.get("due"), now, self._proficiency_days())
            row["due_text"] = clock.humanize_due(row.get("due"), now)
        return rows

    def get_word(self, container: str, ctype: str, uid: str) -> dict | None:
        row = self._db(ctype)[container].find_one(uid=uid)
        return dict(row) if row else None

    def set_note(self, container: str, ctype: str, uid: str, note: str) -> None:
        with self.manager.write_lock:
            self._db(ctype)[container].update(
                {"uid": uid, "note": note, "updated_at": int(self._now())}, keys=["uid"]
            )

    def toggle_favorite(self, container: str, ctype: str, uid: str) -> bool:
        word = self.get_word(container, ctype, uid)
        if word is None:
            raise TaskRowNotFound(uid)
        value = not bool(word.get("favorite"))
        with self.manager.write_lock:
            self._db(ctype)[container].update(
                {"uid": uid, "favorite": value, "updated_at": int(self._now())}, keys=["uid"]
            )
        return value

    def favorites(self, ctype: str = "book") -> list[dict]:
        """跨所有容器收集收藏，按 uid 去重。"""
        now = self._now()
        seen: set[str] = set()
        result: list[dict] = []
        for container in self.containers(ctype):
            table = self._container_sql(container)
            for row in self._db(ctype).query(f"SELECT * FROM {table} WHERE favorite = 1 ORDER BY id"):
                row = dict(row)
                if row["uid"] in seen:
                    continue
                seen.add(row["uid"])
                row["container"] = container
                row["ctype"] = ctype
                row["learn_state"] = clock.learn_state(
                    row.get("state"), row.get("due"), now, self._proficiency_days()
                )
                row["due_text"] = clock.humanize_due(row.get("due"), now)
                result.append(row)
        return result

    def copy_words_to_container(
        self, uids: Iterable[str], target: str, target_ctype: str = "wordbook", source_ctype: str = "book"
    ) -> int:
        """把词复制进词本（只复制词条，进度从零开始；uid 相同便于去重）。"""
        dest_db = self._db(target_ctype)
        table = schema.ensure_container_table(dest_db, target)
        schema.ensure_indexes(dest_db, target)
        quoted = self._container_sql(target)
        existing = {row["uid"] for row in dest_db.query(f"SELECT uid FROM {quoted}")}
        now = int(self._now())
        added = 0
        with self.manager.write_lock:
            for uid in uids:
                if uid in existing:
                    continue
                source = self._find_word_anywhere(uid, source_ctype)
                if source is None:
                    continue
                table.insert(
                    {
                        "uid": source["uid"],
                        "word": source.get("word"),
                        "translation": source.get("translation"),
                        "phonetic": source.get("phonetic"),
                        "state": "new",
                        "difficulty": None,
                        "stability": None,
                        "last_review": None,
                        "due": None,
                        "reps": 0,
                        "lapses": 0,
                        "note": source.get("note"),
                        "favorite": False,
                        "previewed_at": None,
                        "last_answer_id": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                existing.add(uid)
                added += 1
        return added

    def _find_word_anywhere(self, uid: str, ctype: str = "book") -> dict | None:
        """按 uid 找词：先找指定的类型，再找另一类（词本里的词也可能被加进来）。"""
        for candidate in (ctype, "wordbook" if ctype == "book" else "book"):
            db = self._db(candidate)
            for container in self.containers(candidate):
                row = db[container].find_one(uid=uid)
                if row:
                    return dict(row)
        return None

    def remove_words(self, container: str, ctype: str, uids: Iterable[str]) -> int:
        """从容器里删词，并清掉 3 张任务表里对应的行。

        历史（history）保留：那是日志，删了就丢证据。
        """
        db = self._db(ctype)
        if not self.container_exists(container, ctype):
            raise ContainerNotFound(container)
        wanted = [uid for uid in dict.fromkeys(uids or []) if uid]
        if not wanted:
            return 0

        quoted = self._container_sql(container)
        removed = 0
        with self.manager.write_lock:
            for kind in schema.TASK_KINDS:
                table = schema.quote(schema.task_table_name(container, kind))
                if table.replace('"', "") not in self.tasks.tables:
                    continue
                for uid in wanted:
                    self.tasks.query(f"DELETE FROM {table} WHERE uid = :uid", uid=uid)
            self.tasks.commit()

            for uid in wanted:
                rows = list(db.query(f"SELECT COUNT(*) AS c FROM {quoted} WHERE uid = :uid", uid=uid))
                if rows and rows[0]["c"]:
                    db.query(f"DELETE FROM {quoted} WHERE uid = :uid", uid=uid)
                    removed += rows[0]["c"]
            db.commit()
        return removed

    # ------------------------------------------------------------------ 预习
    def preview(self, container: str, ctype: str = "book") -> dict:
        if not self.container_exists(container, ctype):
            raise ContainerNotFound(container)
        limits = self.config.intake_limits()
        limit = limits["learning_per_round"] * limits["preview_multiplier"]
        rows = [
            dict(row)
            for row in self._db(ctype).query(
                f"SELECT * FROM {self._container_sql(container)} WHERE state = 'new' ORDER BY id ASC LIMIT :n",
                n=max(1, limit),
            )
        ]
        cursor_map = self.config.app.get("preview") or {}
        cursor = _int(cursor_map.get(container, {}).get("index"), 0)
        if not rows:
            cursor = 0
        cursor = max(0, min(cursor, len(rows) - 1))
        return {"container": container, "ctype": ctype, "words": rows, "cursor": cursor, "limit": limit}

    def mark_previewed(self, container: str, ctype: str, index: int, uids: Iterable[str]) -> None:
        now = int(self._now())
        uids = list(uids)
        with self.manager.write_lock:
            for uid in uids:
                self._db(ctype)[container].update(
                    {"uid": uid, "previewed_at": now, "updated_at": now}, keys=["uid"]
                )
            self.config.update(
                "app.json",
                {"preview": {container: {"index": max(0, int(index)), "at": now}}},
            )

    # ------------------------------------------------------------------ 建任务
    def has_unfinished(self, container: str, ctype: str, kind: str) -> bool:
        table = self._task_sql(container, kind)
        if schema.task_table_name(container, kind) not in self.tasks.tables:
            return False
        return self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE task_state IS NULL OR task_state != 1") > 0

    def unfinished_tasks(self) -> list[dict]:
        now = self._now()
        found: list[dict] = []
        for table_name in self.tasks.tables:
            if table_name.startswith("sqlite_"):
                continue
            container, _, kind = table_name.rpartition("__")
            if not container or kind not in schema.TASK_KINDS:
                continue
            table = schema.quote(table_name)
            total = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table}")
            if total == 0:
                continue
            done = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE task_state = 1")
            ready = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE problem_state = :s", s="ready")
            failed = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE problem_state = :s", s="failed")
            if done >= total:
                continue
            found.append(
                {
                    "container": container,
                    "kind": kind,
                    "total": total,
                    "done": done,
                    "ready": ready,
                    "failed": failed,
                    "updated_at": now,
                }
            )
        return found

    def create_task(self, container: str, ctype: str, kind: str, limit: int | None = None) -> dict:
        """建一轮任务：清空该容器的同类型任务表，再按 id 顺序快照取词。"""
        if kind not in schema.SOURCE_KINDS:
            raise ValueError(f"未知任务类型 {kind!r}")
        if not self.container_exists(container, ctype):
            raise ContainerNotFound(container)
        limits = self.config.intake_limits()
        if limit is None:
            limit = limits["learning_per_round"] if kind == "learning" else limits["review_per_round"]
        limit = max(0, int(limit))
        table = schema.ensure_task_table(self.tasks, container, kind)
        now = int(self._now())
        source = self._db(ctype)

        with self.manager.write_lock:
            self.tasks.query(f"DELETE FROM {schema.quote(schema.task_table_name(container, kind))}")
            self.tasks.commit()
            if limit == 0:
                return {"container": container, "ctype": ctype, "kind": kind, "created": 0, "limit": 0}
            if kind == "learning":
                rows = list(
                    source.query(
                        f"SELECT * FROM {self._container_sql(container)} WHERE state = :s ORDER BY id ASC LIMIT :n",
                        s="new",
                        n=limit,
                    )
                )
            else:
                eod = clock.end_of_today_ts(self.config.timezone(), now)
                rows = list(
                    source.query(
                        f"SELECT * FROM {self._container_sql(container)} "
                        "WHERE state = :s AND due IS NOT NULL AND due <= :eod ORDER BY id ASC LIMIT :n",
                        s="review",
                        eod=eod,
                        n=limit,
                    )
                )
            created = 0
            batch_id = uuid.uuid4().hex
            for row in rows:
                row = dict(row)
                table.insert(
                    {
                        "uid": row["uid"],
                        "batch_id": batch_id,
                        "word": row.get("word"),
                        "translation": row.get("translation"),
                        "phonetic": row.get("phonetic"),
                        "difficulty": _float(row.get("difficulty")),
                        "stability": _float(row.get("stability")),
                        "state": row.get("state"),
                        "problem": None,
                        "problem_state": "pending",
                        "problem_error": None,
                        "task_state": 0,
                        "first_pass": None,
                        "error_count": 0,
                        "served_seq": None,
                        "answer": None,
                        "sentence_score": None,
                        "keyword_score": None,
                        "note": None,
                        "applied": False,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                created += 1
        return {"container": container, "ctype": ctype, "kind": kind, "created": created, "limit": limit}

    def discard_task(self, container: str, kind: str) -> None:
        table_name = schema.task_table_name(container, kind)
        if table_name not in self.tasks.tables:
            return
        with self.manager.write_lock:
            self.tasks.query(f"DELETE FROM {schema.quote(table_name)}")
            self.tasks.commit()

    # ------------------------------------------------------------------ problem
    def problems(self, container: str, kind: str) -> dict:
        table = self._task_sql(container, kind)
        total = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table}")
        ready = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE problem_state = :s", s="ready")
        failed = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE problem_state = :s", s="failed")
        pending = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE problem_state = :s", s="pending")
        items = [
            dict(row)
            for row in self.tasks.query(
                f"SELECT uid, word, problem_state, problem_error FROM {table} ORDER BY id ASC"
            )
        ]
        return {"total": total, "ready": ready, "failed": failed, "pending": pending, "items": items}

    def pending_problems(self, container: str, kind: str, ctype: str = "book") -> list[dict]:
        """待出题的任务行，附带这个词的历史例句（作为"要避开"的清单）。"""
        rows = [
            dict(row)
            for row in self.tasks.query(
                f"SELECT * FROM {self._task_sql(container, kind)} WHERE problem_state = :s ORDER BY id ASC",
                s="pending",
            )
        ]
        for row in rows:
            word = self.get_word(container, ctype, row["uid"]) or {}
            row["translation"] = word.get("translation") or row.get("translation")
            row["recent_sentences"] = _parse_sentences(word.get("sentences"))
        return rows

    def failed_problems(self, container: str, kind: str) -> list[dict]:
        return [
            dict(row)
            for row in self.tasks.query(
                f"SELECT * FROM {self._task_sql(container, kind)} WHERE problem_state = :s ORDER BY id ASC",
                s="failed",
            )
        ]

    def set_problem(
        self,
        container: str,
        kind: str,
        uid: str,
        problem: dict,
        ctype: str = "book",
    ) -> None:
        # 生成时用过的历史例句随 problem 一起带回来，存进任务行便于回看
        avoid = problem.pop("avoid_sentences", None) or []
        with self.manager.write_lock:
            self.tasks[schema.task_table_name(container, kind)].update(
                {
                    "uid": uid,
                    "problem": json.dumps(problem, ensure_ascii=False),
                    "problem_state": "ready",
                    "problem_error": None,
                    "recent_sentences": json.dumps(list(avoid), ensure_ascii=False),
                    "updated_at": int(self._now()),
                },
                keys=["uid"],
            )
        self._record_sentence(container, ctype, uid, problem.get("sentence"))

    def _record_sentence(self, container: str, ctype: str, uid: str, sentence: str | None) -> None:
        """把新句子追加到词的历史里（去重、只留最近 N 条）。"""
        text = (sentence or "").strip()
        limit = self.config.sentence_history_size()
        if not text or limit <= 0:
            return
        db = self._db(ctype)
        try:
            word = db[container].find_one(uid=uid)
        except Exception:
            return
        if not word:
            return
        history = _parse_sentences(word.get("sentences"), limit=limit)
        if any(item.strip().lower() == text.lower() for item in history):
            return
        history.append(text)
        history = history[-limit:]
        with self.manager.write_lock:
            db[container].update(
                {
                    "uid": uid,
                    "sentences": json.dumps(history, ensure_ascii=False),
                    "updated_at": int(self._now()),
                },
                keys=["uid"],
            )

    def fail_problem(self, container: str, kind: str, uid: str, error: str) -> None:
        with self.manager.write_lock:
            self.tasks[schema.task_table_name(container, kind)].update(
                {
                    "uid": uid,
                    "problem_state": "failed",
                    "problem_error": str(error)[:500],
                    "updated_at": int(self._now()),
                },
                keys=["uid"],
            )

    def skip_problem(self, container: str, kind: str, uid: str) -> None:
        """跳过的词移出本轮（不写词库、state 不变，下次还会被选中）。"""
        with self.manager.write_lock:
            self.tasks[schema.task_table_name(container, kind)].delete(uid=uid)

    def retry_problems(self, container: str, kind: str, uids: Iterable[str] | None = None) -> list[str]:
        """把失败的 problem 打回 pending，交给生成线程重跑。"""
        wanted = set(uids) if uids else None
        table_name = schema.task_table_name(container, kind)
        target_rows = [
            row["uid"] for row in self.failed_problems(container, kind) if wanted is None or row["uid"] in wanted
        ]
        with self.manager.write_lock:
            for uid in target_rows:
                self.tasks[table_name].update(
                    {"uid": uid, "problem_state": "pending", "problem_error": None, "updated_at": int(self._now())},
                    keys=["uid"],
                )
        return target_rows

    def get_problem(self, container: str, kind: str, uid: str) -> dict:
        row = self.tasks[schema.task_table_name(container, kind)].find_one(uid=uid)
        if row is None:
            raise TaskRowNotFound(uid)
        row = dict(row)
        problem = row.get("problem")
        row["problem_data"] = json.loads(problem) if problem else {}
        return row

    # ------------------------------------------------------------------ 答题
    def next_task(self, container: str, kind: str) -> dict | None:
        """环形顺序取下一题。

        规则：还没出现过的词（``served_seq IS NULL``）按 id 顺序先来；出过的词按
        ``served_seq`` 升序排到后面。每次作答把它推到队尾，所以答错的词不会被
        立刻重问，而要绕完一整圈才轮到它。
        """
        table = self._task_sql(container, kind)
        rows = list(
            self.tasks.query(
                f"SELECT * FROM {table} WHERE task_state != 1 AND problem_state = :s "
                "ORDER BY CASE WHEN served_seq IS NULL THEN 0 ELSE 1 END, served_seq ASC, id ASC LIMIT 1",
                s="ready",
            )
        )
        if not rows:
            return None
        row = dict(rows[0])
        problem = row.get("problem")
        row["problem_data"] = json.loads(problem) if problem else {}
        return row

    def _push_to_queue_tail(self, table_name: str, uid: str) -> None:
        """把一道题推到环的尾部（下次轮到它是绕完一圈之后）。"""
        seq = self._scalar(
            self.tasks, f"SELECT COALESCE(MAX(served_seq), 0) + 1 AS s FROM {schema.quote(table_name)}"
        )
        self.tasks[table_name].update({"uid": uid, "served_seq": seq}, keys=["uid"])

    def progress(self, container: str, kind: str) -> dict:
        table = self._task_sql(container, kind)
        total = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table}")
        done = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE task_state = 1")
        return {"total": total, "done": done, "remaining": max(0, total - done)}

    def submit(
        self,
        container: str,
        ctype: str,
        kind: str,
        uid: str,
        *,
        passed: bool,
        answer: str | None = None,
        sentence_score: float | None = None,
        keyword_score: float | None = None,
        note: str | None = None,
    ) -> dict:
        """应用一次作答结果，必要时完成该词并写回。"""
        table = schema.task_table_name(container, kind)
        with self.manager.write_lock:
            row = self.tasks[table].find_one(uid=uid)
            if row is None:
                raise TaskRowNotFound(uid)
            row = dict(row)
            current = _int(row.get("task_state"), 0)
            if current >= 1:
                return {
                    "uid": uid,
                    "task_state": 1,
                    "finished": True,
                    "first_pass": bool(row.get("first_pass")),
                    "error_count": _int(row.get("error_count")),
                    "already_finished": True,
                    "progress": self.progress(container, kind),
                }

            first_answer = row.get("first_pass") is None
            first_pass = row.get("first_pass")
            errors = _int(row.get("error_count"))

            if passed:
                current = min(1, current + 1)
                if first_answer:
                    first_pass = True
            else:
                current = max(-1, current - 1)
                first_pass = False
                errors += 1

            payload = {
                "uid": uid,
                "task_state": current,
                "first_pass": first_pass,
                "error_count": errors,
                "answer": answer if answer is not None else row.get("answer"),
                "sentence_score": _float(sentence_score) if sentence_score is not None else _float(row.get("sentence_score")),
                "keyword_score": _float(keyword_score) if keyword_score is not None else _float(row.get("keyword_score")),
                "updated_at": int(self._now()),
            }
            if note is not None:
                payload["note"] = note
            self.tasks[table].update(payload, keys=["uid"])
            self._push_to_queue_tail(table, uid)

            finished = current >= 1
            if finished:
                self._apply(container, ctype, kind, uid)

        return {
            "uid": uid,
            "task_state": current,
            "finished": finished,
            "first_pass": bool(first_pass),
            "error_count": errors,
            "already_finished": False,
            "progress": self.progress(container, kind),
        }

    def _apply(self, container: str, ctype: str, kind: str, uid: str) -> None:
        """写回容器表 + 写 history + 标记 applied。三步可重放。"""
        task_table = schema.task_table_name(container, kind)
        row = self.tasks[task_table].find_one(uid=uid)
        if row is None:
            raise TaskRowNotFound(uid)
        row = dict(row)
        if row.get("applied"):
            return

        answer_id = _uid_answer_id(container, ctype, kind, row["id"], row.get("batch_id"))
        first_pass = bool(row.get("first_pass"))
        errors = _int(row.get("error_count"))
        now = self._now()

        words = self._db(ctype)
        word = words[container].find_one(uid=uid)
        if word is None:
            raise TaskRowNotFound(f"{container} 中找不到 {uid}")
        word = dict(word)

        if word.get("last_answer_id") != answer_id:
            if kind == "learning":
                result = scheduler.first_learning(first_pass, errors, now, self.config.fsrs)
            else:
                result = scheduler.review_pass(
                    first_pass, errors, now, word.get("difficulty"), word.get("stability"), self.config.fsrs
                )
            words[container].update(
                {
                    "uid": uid,
                    "state": "review",
                    "difficulty": result["difficulty"],
                    "stability": result["stability"],
                    "last_review": now,
                    "due": int(result["next_review"]),
                    "reps": _int(word.get("reps")) + errors + 1,
                    "lapses": _int(word.get("lapses")) + errors,
                    "last_answer_id": answer_id,
                    "updated_at": int(now),
                },
                keys=["uid"],
            )

        exists = self._scalar(
            self.history,
            "SELECT COUNT(*) AS c FROM history WHERE answer_id = :aid",
            aid=answer_id,
        )
        if not exists:
            self.history["history"].insert(
                {
                    "answer_id": answer_id,
                    "uid": uid,
                    "container": container,
                    "container_type": ctype,
                    "task_type": kind,
                    "time": int(now),
                    "first_pass": first_pass,
                    "error_count": errors,
                    "sentence_score": _float(row.get("sentence_score")),
                    "keyword_score": _float(row.get("keyword_score")),
                }
            )

        self.tasks[task_table].update({"uid": uid, "applied": True, "updated_at": int(now)}, keys=["uid"])

    # ------------------------------------------------------------------ 拼写
    def create_spelling(self, container: str, ctype: str, source_kind: str) -> int:
        if source_kind not in schema.SOURCE_KINDS:
            raise ValueError(source_kind)
        source_table = schema.quote(schema.task_table_name(container, source_kind))
        if schema.task_table_name(container, source_kind) not in self.tasks.tables:
            return 0
        table = schema.ensure_task_table(self.tasks, container, "spelling")
        now = int(self._now())
        rows = list(self.tasks.query(f"SELECT * FROM {source_table} WHERE task_state = 1 ORDER BY id ASC"))
        with self.manager.write_lock:
            self.tasks.query(f"DELETE FROM {schema.quote(schema.task_table_name(container, 'spelling'))}")
            self.tasks.commit()
            batch_id = uuid.uuid4().hex
            for row in rows:
                row = dict(row)
                table.insert(
                    {
                        "uid": row["uid"],
                        "batch_id": batch_id,
                        "word": row.get("word"),
                        "translation": row.get("translation"),
                        "phonetic": row.get("phonetic"),
                        "task_state": 0,
                        "first_pass": None,
                        "error_count": 0,
                        "revealed": False,
                        "served_seq": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        return len(rows)

    def next_spelling(self, container: str) -> dict | None:
        table = self._task_sql(container, "spelling")
        if schema.task_table_name(container, "spelling") not in self.tasks.tables:
            return None
        rows = list(
            self.tasks.query(
                f"SELECT * FROM {table} WHERE task_state != 1 "
                "ORDER BY CASE WHEN served_seq IS NULL THEN 0 ELSE 1 END, served_seq ASC, id ASC LIMIT 1"
            )
        )
        return dict(rows[0]) if rows else None

    def submit_spelling(self, container: str, ctype: str, uid: str, typed: str) -> dict:
        table = schema.task_table_name(container, "spelling")
        with self.manager.write_lock:
            row = self.tasks[table].find_one(uid=uid)
            if row is None:
                raise TaskRowNotFound(uid)
            row = dict(row)
            expected = (row.get("word") or "").strip()
            correct = (typed or "").strip().lower() == expected.lower()
            state = _int(row.get("task_state"))
            errors = _int(row.get("error_count"))
            revealed = bool(row.get("revealed"))
            showed_answer = revealed

            if correct:
                if state == 0:
                    state = 1
                else:  # -1：抄写正确，回到 0，留在队列里等盲拼
                    state = 0
                revealed = False
            else:
                state = -1
                revealed = True
                errors += 1

            first_pass = None
            if state == 1:
                first_pass = errors == 0

            self.tasks[table].update(
                {
                    "uid": uid,
                    "task_state": state,
                    "revealed": revealed,
                    "error_count": errors,
                    "first_pass": first_pass,
                    "updated_at": int(self._now()),
                },
                keys=["uid"],
            )
            self._push_to_queue_tail(table, uid)

            finished = state == 1
            if finished:
                self._apply_spelling(container, ctype, uid, row, errors)

            remaining = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {self._task_sql(container, 'spelling')} WHERE task_state != 1")
        return {
            "uid": uid,
            "correct": correct,
            "task_state": state,
            "revealed": revealed,
            "showed_answer": showed_answer,
            "expected": expected if (revealed or correct) else None,
            "finished": finished,
            "round_finished": remaining == 0,
        }

    def _apply_spelling(self, container: str, ctype: str, uid: str, row: dict, errors: int) -> None:
        answer_id = _uid_answer_id(container, ctype, "spelling", row["id"], row.get("batch_id"))
        exists = self._scalar(self.history, "SELECT COUNT(*) AS c FROM history WHERE answer_id = :aid", aid=answer_id)
        if exists:
            return
        self.history["history"].insert(
            {
                "answer_id": answer_id,
                "uid": uid,
                "container": container,
                "container_type": ctype,
                "task_type": "spelling",
                "time": int(self._now()),
                "first_pass": errors == 0,
                "error_count": errors,
                "sentence_score": None,
                "keyword_score": None,
            }
        )

    def abandon_spelling(self, container: str) -> None:
        table_name = schema.task_table_name(container, "spelling")
        if table_name not in self.tasks.tables:
            return
        with self.manager.write_lock:
            self.tasks.query(f"DELETE FROM {schema.quote(table_name)}")
            self.tasks.commit()

    def cleanup_spelling(self) -> int:
        """清掉所有残留的拼写队列。

        拼写是附加练习，不做断点续做：进程重启后残留直接丢弃，
        已经完成的词在 history 里本来就已经记过了。
        """
        removed = 0
        with self.manager.write_lock:
            for table_name in list(self.tasks.tables):
                if not table_name.endswith("__spelling") or table_name.startswith("sqlite_"):
                    continue
                table = schema.quote(table_name)
                try:
                    removed += self._scalar(
                        self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE task_state IS NULL OR task_state != 1"
                    )
                    self.tasks.query(f"DELETE FROM {table}")
                    self.tasks.commit()
                except Exception:
                    continue
        return removed

    def spelling_progress(self, container: str) -> dict:
        table_name = schema.task_table_name(container, "spelling")
        if table_name not in self.tasks.tables:
            return {"total": 0, "done": 0, "remaining": 0}
        table = schema.quote(table_name)
        total = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table}")
        done = self._scalar(self.tasks, f"SELECT COUNT(*) AS c FROM {table} WHERE task_state = 1")
        return {"total": total, "done": done, "remaining": max(0, total - done)}

    # ------------------------------------------------------------------ 结果
    def round_summary(self, container: str, ctype: str, kind: str) -> list[dict]:
        table = self._task_sql(container, kind)
        now = self._now()
        words_db = self._db(ctype)
        result: list[dict] = []
        for row in self.tasks.query(f"SELECT * FROM {table} WHERE task_state = 1 ORDER BY id ASC"):
            row = dict(row)
            word = words_db[container].find_one(uid=row["uid"])
            word = dict(word) if word else {}
            result.append(
                {
                    "uid": row["uid"],
                    "word": row.get("word"),
                    "translation": row.get("translation"),
                    "phonetic": row.get("phonetic"),
                    "first_pass": bool(row.get("first_pass")),
                    "error_count": _int(row.get("error_count")),
                    "state": word.get("state"),
                    "due": word.get("due"),
                    "learn_state": clock.learn_state(
                        word.get("state"), word.get("due"), now, self._proficiency_days()
                    ),
                    "due_text": clock.humanize_due(word.get("due"), now),
                }
            )
        return result

    def migrate_served_seq(self) -> int:
        """给旧轮次补出题顺序。

        环形出题依赖 ``served_seq``：NULL = 还没出现过，优先按 id 出。老版本创建的
        任务行没有这个值，如果不补，答错过的词会被当成"没出现过"而立刻重出。
        规则：已经作答过的行（``first_pass`` 非空）按 id 设一个序号，这样它们会排在
        所有未出场的词之后——正好是"绕一圈再回来"。
        """
        touched = 0
        with self.manager.write_lock:
            for table_name in list(self.tasks.tables):
                if table_name.startswith("sqlite_"):
                    continue
                if not table_name.endswith(("__learning", "__review", "__spelling")):
                    continue
                table = schema.quote(table_name)
                try:
                    count = self._scalar(
                        self.tasks,
                        f"SELECT COUNT(*) AS c FROM {table} WHERE served_seq IS NULL AND first_pass IS NOT NULL",
                    )
                    if count:
                        self.tasks.query(
                            f"UPDATE {table} SET served_seq = id WHERE served_seq IS NULL AND first_pass IS NOT NULL"
                        )
                        self.tasks.commit()
                        touched += count
                except Exception:
                    continue
        return touched

    def recover(self) -> list[dict]:
        """启动时重放未完成的写回。"""
        recovered: list[dict] = []
        for table_name in list(self.tasks.tables):
            if table_name.startswith("sqlite_"):
                continue
            container, _, kind = table_name.rpartition("__")
            if not container or kind not in ("learning", "review"):
                continue
            table = schema.quote(table_name)
            for row in self.tasks.query(f"SELECT * FROM {table} WHERE task_state = 1 AND (applied IS NULL OR applied = 0)"):
                row = dict(row)
                ctype = "wordbook" if container in self.containers("wordbook") else "book"
                try:
                    with self.manager.write_lock:
                        self._apply(container, ctype, kind, row["uid"])
                    recovered.append({"container": container, "kind": kind, "uid": row["uid"]})
                except Exception:
                    continue
        return recovered
