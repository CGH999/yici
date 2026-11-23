"""AI 记忆文件：只存运行时的对话历史，用来提高缓存命中。

``data/memory/deepseek_memory.json`` 结构固定为两个 section::

    {"generate_sentence": {"history": [...]}, "check_answer": {"history": [...]}}

提示词（system + few-shot）不在这里，放在 ``config/prompts.json``——它们是
缓存前缀，必须字节级稳定，程序只读不写。

写文件用**单个写线程**：多线程各自「读整文件 → 改 → 写整文件」会互相覆盖。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

SECTIONS = ("generate_sentence", "check_answer")


class AIMemory:
    def __init__(self, path: str | Path, max_history: int = 2) -> None:
        self.path = Path(path)
        self.max_history = max(1, int(max_history))
        self._lock = threading.RLock()
        self._data = self._load()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="ai-memory-writer", daemon=True)
        self._worker.start()

    # ---------------------------------------------------------------- 内部
    def _empty(self) -> dict:
        return {section: {"history": []} for section in SECTIONS}

    def _load(self) -> dict:
        data = self._empty()
        if not self.path.exists():
            return data
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (json.JSONDecodeError, OSError):
            return data
        if not isinstance(raw, dict):
            return data
        for section in SECTIONS:
            block = raw.get(section) or {}
            history = block.get("history") if isinstance(block, dict) else None
            if isinstance(history, list):
                data[section]["history"] = [
                    item for item in history if isinstance(item, dict) and "user" in item and "assistant" in item
                ]
        return data

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        tmp.replace(self.path)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                section, user, assistant = item
                with self._lock:
                    history = self._data[section]["history"]
                    history.append({"user": user, "assistant": assistant})
                    if len(history) > self.max_history:
                        del history[: len(history) - self.max_history]
                self._write()
            except Exception:
                pass
            finally:
                self._queue.task_done()

    # ---------------------------------------------------------------- 对外
    def history(self, section: str) -> list[dict]:
        if section not in SECTIONS:
            raise KeyError(section)
        with self._lock:
            return [dict(item) for item in self._data[section]["history"]]

    def append(self, section: str, user: str, assistant: str) -> None:
        if section not in SECTIONS:
            raise KeyError(section)
        self._queue.put((section, user, assistant))

    def flush(self, timeout: float = 5.0) -> bool:
        """等待写线程把队列写完（测试与退出时用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        try:
            self._queue.put(None)
            self._worker.join(timeout=2.0)
        except Exception:
            pass
