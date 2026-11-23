"""数据库连接管理。

四个 SQLite 文件各持有一个 ``dataset.Database``：

``library``    词库（单文件多表，表名 = 词书名）
``wordbooks``  词本库（单文件多表，表名 = 词本名）
``tasks``      任务库（表名 = <容器名>__learning / __review / __spelling）
``history``    历史

连接参数：``NullPool`` + ``check_same_thread=False``，每次取用都是新连接，
因此可以安全地在 Flask 的多线程请求里使用。所有写入再由 :class:`WriteLock`
串行化，避免 SQLite 写锁冲突。
"""

from __future__ import annotations

import threading
from pathlib import Path

import dataset

try:  # 本地环境可能没装 sqlalchemy 的 NullPool 路径，退化为默认
    from sqlalchemy.pool import NullPool

    # AUTOCOMMIT：dataset 的 db.query() 不会自动提交，留着未提交事务会长期占着写锁
    # （表现为别的连接 / 下次请求报 "database is locked"）。单用户场景不需要显式事务，
    # 直接让每条语句自己提交最稳。
    _ENGINE_KWARGS = {
        "poolclass": NullPool,
        "isolation_level": "AUTOCOMMIT",
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }
except Exception:  # pragma: no cover
    _ENGINE_KWARGS = None

DB_NAMES = ("library", "wordbooks", "tasks", "history")


class DBManager:
    def __init__(self, paths: dict[str, Path]) -> None:
        missing = [name for name in DB_NAMES if name not in paths]
        if missing:
            raise ValueError(f"缺少数据库路径：{missing}")
        self._paths = {name: Path(path) for name, path in paths.items()}
        self._dbs: dict[str, dataset.Database] = {}
        self._lock = threading.RLock()
        # 串行化所有写操作；单用户场景下几乎无争用
        self.write_lock = threading.RLock()

    @classmethod
    def from_config(cls, config) -> "DBManager":
        return cls(
            {
                "library": config.library_path,
                "wordbooks": config.wordbooks_path,
                "tasks": config.tasks_path,
                "history": config.history_path,
            }
        )

    def path(self, name: str) -> Path:
        return self._paths[name]

    def get(self, name: str) -> dataset.Database:
        if name not in self._paths:
            raise KeyError(f"未知数据库 {name!r}，可用：{list(self._paths)}")
        with self._lock:
            db = self._dbs.get(name)
            if db is None:
                db = self._connect(self._paths[name])
                self._dbs[name] = db
            return db

    @staticmethod
    def _checkpoint(path: Path) -> None:
        """开主连接之前，先把上次异常退出留下的 WAL 收掉。

        否则在 WAL 需要恢复时，任何写操作都会报 "database is locked"。
        这一步用一次性短连接做，失败也不影响启动。
        """
        if not path.exists():
            return
        try:
            import sqlite3

            conn = sqlite3.connect(str(path), timeout=10)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except Exception:
            pass

    @staticmethod
    def _connect(path: Path) -> dataset.Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        DBManager._checkpoint(path)
        url = f"sqlite:///{path}"
        if _ENGINE_KWARGS:
            try:
                db = dataset.connect(url, engine_kwargs=_ENGINE_KWARGS)
            except TypeError:
                db = dataset.connect(url)
        else:
            db = dataset.connect(url)
        try:
            list(db.query("PRAGMA journal_mode=WAL"))
            list(db.query("PRAGMA synchronous=NORMAL"))
        except Exception:
            pass
        return db

    def close_all(self) -> None:
        with self._lock:
            for db in self._dbs.values():
                try:
                    db.close()
                except Exception:
                    pass
            self._dbs.clear()
