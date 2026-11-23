"""并发生成 problem。

三种模式（`config/ai.json` 的 ``concurrency``）：

* ``serial``    一次一个，缓存命中率最高
* ``parallel``  全并发，最快，前缀会被重复按 miss 计费
* ``warmup``    先单发 ``warmup_requests`` 个把前缀写进缓存，其余再并发

线程池里只发 HTTP；回调由调用方负责（任务模块的写库自带锁）。
余额不足时立刻停止派发新任务，剩余条目按「余额不足」记录。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.services.ai_client import AIError, AIResponseError, InsufficientBalance


class AIPool:
    def __init__(self, client, config) -> None:
        self.client = client
        self.config = config

    def _settings(self) -> tuple[str, int, int]:
        conc = (self.config.ai.get("concurrency") or {})
        enabled = bool(conc.get("enabled", True))
        mode = str(conc.get("mode") or "warmup").lower()
        workers = max(1, int(conc.get("workers") or 1))
        warmup = max(0, int(conc.get("warmup_requests") or 1))
        if not enabled or workers <= 1:
            mode = "serial"
        if mode not in ("serial", "parallel", "warmup"):
            mode = "warmup"
        return mode, workers, warmup

    def generate_many(self, items: list[dict], on_success=None, on_error=None) -> dict:
        """为每个词生成 problem。

        :param items: ``[{"uid", "word", "translation"}]``
        :param on_success: ``(uid, problem) -> None``
        :param on_error: ``(uid, message) -> None``
        :return: ``{"ok": n, "failed": n, "errors": {...}, "stopped": reason|None}``
        """
        mode, workers, warmup = self._settings()
        results: dict[str, dict] = {}
        errors: dict[str, str] = {}
        stopped: list[str] = []
        lock = threading.Lock()

        def run_one(item: dict) -> None:
            uid = item["uid"]
            if stopped:
                with lock:
                    errors.setdefault(uid, "已中止")
                return
            try:
                problem = self.client.generate_sentence(
                    item.get("word") or "",
                    item.get("translation"),
                    item.get("recent_sentences") or [],
                )
                with lock:
                    results[uid] = problem
                if on_success:
                    on_success(uid, problem)
            except InsufficientBalance as exc:
                with lock:
                    stopped.append(str(exc))
                    errors[uid] = str(exc)
                if on_error:
                    on_error(uid, str(exc))
            except (AIResponseError, AIError) as exc:
                with lock:
                    errors[uid] = str(exc)
                if on_error:
                    on_error(uid, str(exc))
            except Exception as exc:  # 兜底，避免线程池吞掉异常
                with lock:
                    errors[uid] = f"{type(exc).__name__}: {exc}"
                if on_error:
                    on_error(uid, errors[uid])

        if not items:
            return {"ok": 0, "failed": 0, "errors": {}, "stopped": None}

        if mode == "serial":
            for item in items:
                if stopped:
                    with lock:
                        errors.setdefault(item["uid"], "已中止")
                    continue
                run_one(item)
        else:
            head = items[:warmup] if mode == "warmup" else []
            tail = items[len(head) :]
            for item in head:
                if stopped:
                    with lock:
                        errors.setdefault(item["uid"], "已中止")
                    continue
                run_one(item)
            if tail and not stopped:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai-problem") as pool:
                    futures = [pool.submit(run_one, item) for item in tail]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception:
                            pass

        return {
            "ok": len(results),
            "failed": len(errors),
            "errors": errors,
            "stopped": stopped[0] if stopped else None,
        }
