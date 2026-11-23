"""当前轮次的内存状态（单用户，进程内即可）。

**按 kind 分开**：学习和复习各自有一份状态，互不覆盖——
这样"学到一半去复习，再回来学习"两边的进度都在，指针也不会串。

任务数据本身全部落在 tasks.db（按 kind 分表），这里只放
「哪一轮正在进行、这一轮的出题是否在跑」这类易失信息。
"""

from __future__ import annotations

import threading
import time


class Round:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._states: dict[str, dict] = {}
        self._last: str = ""

    @staticmethod
    def _blank() -> dict:
        return {
            "container": "",
            "ctype": "book",
            "kind": "",
            "generating": False,
            "error": None,
            "started_at": 0.0,
        }

    # ------------------------------------------------------------------ 写入
    def start(self, container: str, ctype: str, kind: str) -> None:
        with self.lock:
            state = self._blank()
            state.update(
                {
                    "container": container,
                    "ctype": ctype,
                    "kind": kind,
                    "started_at": time.time(),
                }
            )
            self._states[kind] = state
            self._last = kind

    def begin_generation(self, kind: str) -> bool:
        """原子地占住出题位；已经在跑就返回 False。"""
        with self.lock:
            state = self._states.get(kind)
            if state is None or state["generating"]:
                return False
            state["generating"] = True
            state["error"] = None
            return True

    def end_generation(self, kind: str) -> None:
        with self.lock:
            state = self._states.get(kind)
            if state is not None:
                state["generating"] = False

    def set_error(self, kind: str, message: str | None) -> None:
        with self.lock:
            state = self._states.get(kind)
            if state is not None:
                state["error"] = message

    def clear(self, kind: str | None = None) -> None:
        with self.lock:
            if kind is None:
                self._states.clear()
                self._last = ""
                return
            self._states.pop(kind, None)
            if self._last == kind:
                remaining = list(self._states)
                self._last = remaining[-1] if remaining else ""

    def forget_container(self, container: str) -> None:
        with self.lock:
            for kind in [k for k, s in self._states.items() if s["container"] == container]:
                self._states.pop(kind, None)
                if self._last == kind:
                    self._last = ""
            if self._last not in self._states:
                remaining = list(self._states)
                self._last = remaining[-1] if remaining else ""

    # ------------------------------------------------------------------ 读取
    def get(self, kind: str | None = None) -> dict:
        with self.lock:
            key = kind or self._last
            state = self._states.get(key)
            return dict(state) if state else self._blank()

    def snapshot(self, kind: str | None = None) -> dict:
        return self.get(kind)

    def active(self, kind: str | None = None) -> bool:
        state = self.get(kind)
        return bool(state["container"] and state["kind"])

    def last_kind(self) -> str:
        with self.lock:
            return self._last

    def kinds(self) -> list[str]:
        """当前有记录的轮次类型（learning / review）。"""
        with self.lock:
            return [kind for kind, state in self._states.items() if state["container"]]
