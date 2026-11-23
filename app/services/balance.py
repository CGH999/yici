"""余额缓存与警戒线。

DeepSeek 的 ``/user/balance`` 返回的是**金额**（元），不是 token 数。
界面上显示金额，警戒线也按金额判断；中途没钱的场景由 HTTP 402 兜住。
"""

from __future__ import annotations

import threading
import time

from app.services.ai_client import AIResponseError, InsufficientBalance


class Balance:
    def __init__(self, client, config) -> None:
        self.client = client
        self.config = config
        self._lock = threading.RLock()
        self._data: dict | None = None
        self._fetched_at: float = 0.0
        self._error: str | None = None

    # ---------------------------------------------------------------- 配置
    def warning_line(self) -> float:
        try:
            return float((self.config.app.get("balance") or {}).get("warning", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def refresh_seconds(self) -> int:
        try:
            return int((self.config.app.get("balance") or {}).get("refresh_seconds", 300) or 300)
        except (TypeError, ValueError):
            return 300

    # ---------------------------------------------------------------- 读取
    def current(self, force: bool = False) -> dict:
        with self._lock:
            fresh = self._data is not None and (time.time() - self._fetched_at) < self.refresh_seconds()
            if fresh and not force:
                return dict(self._data, stale=False, error=self._error)
            try:
                data = self.client.get_balance()
                self._data = data
                self._fetched_at = time.time()
                self._error = None
            except InsufficientBalance:
                self._data = {"available": False, "amount": 0.0, "currency": "CNY", "fetched_at": int(time.time())}
                self._fetched_at = time.time()
                self._error = "账户余额不足"
            except (AIResponseError, Exception) as exc:
                # 查不到余额不应该阻断使用，保留上一次的值并把错误展示出来；
                # 同时刷新时间戳，避免每次请求都去撞一次网络。
                self._error = f"{type(exc).__name__}: {exc}"
                self._fetched_at = time.time()
                if self._data is None:
                    self._data = {"available": False, "amount": 0.0, "currency": "CNY", "fetched_at": 0}
            return dict(self._data or {}, stale=(time.time() - self._fetched_at) >= self.refresh_seconds(), error=self._error)

    # ---------------------------------------------------------------- 判定
    def can_start(self) -> tuple[bool, dict]:
        """能不能开始一轮。只有「明确拿到余额且低于警戒线」才阻止。"""
        data = self.current()
        amount = float(data.get("amount") or 0)
        line = self.warning_line()
        if data.get("error") and not data.get("fetched_at"):
            return True, data  # 查不到余额，放行
        if amount < line:
            return False, data
        return True, data

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_at = 0.0
