"""配置加载。

四份 JSON 各自职责：

* ``app.json``    业务参数（每轮数量、过关规则、熟练阈值、余额警戒线）
* ``fsrs.json``   算法参数（D/S 公式）
* ``ai.json``     密钥与请求参数（不入版本控制）
* ``prompts.json`` 提示词（只读的稳定前缀，改一个字缓存全失效）

按 mtime 缓存：改完 JSON 下一次读取就生效，不需要重启。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _detect_base_dir() -> Path:
    """可写目录：源码运行时是项目根，PyInstaller 打包后是 exe 所在目录。

    打包成单文件后 ``__file__`` 指向临时解包目录，配置和数据不能放那儿，
    否则每次启动都会丢。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _detect_base_dir()

DEFAULTS: dict[str, dict[str, Any]] = {
    "app.json": {
        "app_name": "译词",
        "timezone": "Asia/Shanghai",
        "current_container": "",
        "current_container_type": "book",
        "learning_per_round": 20,
        "review_per_round": 50,
        "preview_multiplier": 2,
        "proficiency_days": 365,
        "pass_rule": {
            "mode": "and",
            "sentence_min": 0.8,
            "keyword_min": 0.8,
            "sentence_weight": 0.5,
            "keyword_weight": 0.5,
            "total_min": 0.8,
        },
        "spelling": {"ask_after_learning": True, "ask_after_review": True},
        # 每个词记住多少条历史例句，生成新句子时作为"要避开"的清单；0 = 关闭
        "sentence_history": 5,
        "balance": {"warning": 5.0, "refresh_seconds": 300},
        "server": {"port": 5000, "allow_lan": False, "token": "", "secret": ""},
        "ui": {"show_phonetic": True},
    },
    "fsrs.json": {
        "initial_difficulty": 3.0,
        "initial_stability": 86400.0,
        "target_d": 3.0,
        "alpha": 0.8,
        "beta": 0.4,
        "damping": 0.02,
        "failure_stability_cap_seconds": 86400,
        "min_difficulty": 1.0,
        "max_difficulty": 10.0,
        "min_interval_seconds": 86400,
    },
    "ai.json": {
        "api_key": "",
        "chat_url": "https://api.deepseek.com/chat/completions",
        "balance_url": "https://api.deepseek.com/user/balance",
        "model": "deepseek-chat",
        "stream": False,
        "temperature": 0.3,
        "timeout_seconds": 60,
        "max_history": 2,
        "concurrency": {"enabled": True, "mode": "warmup", "workers": 4, "warmup_requests": 1},
        "retry": {"times": 3, "backoff_seconds": [0.5, 1.0, 2.0]},
    },
    "prompts.json": {},
}


class ConfigError(RuntimeError):
    """配置文件缺失或格式错误。"""


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else BASE_DIR
        self.config_dir = self.base_dir / "config"
        self.data_dir = self.base_dir / "data"
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.RLock()

    # ---------- 路径 ----------
    def path(self, name: str) -> Path:
        return self.config_dir / name

    @property
    def library_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def tasks_path(self) -> Path:
        return self.data_dir / "tasks.db"

    @property
    def wordbooks_path(self) -> Path:
        return self.data_dir / "wordbooks.db"

    @property
    def history_path(self) -> Path:
        return self.data_dir / "user" / "history.db"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory" / "deepseek_memory.json"

    def ensure_dirs(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.data_dir / "user", self.data_dir / "memory"):
            directory.mkdir(parents=True, exist_ok=True)

    def bootstrap(self) -> None:
        """首次运行时把缺的配置文件写出来（打包成 exe 后尤其需要）。"""
        self.ensure_dirs()
        for name in ("app.json", "fsrs.json", "ai.json"):
            if not self.path(name).exists():
                self.save(name, DEFAULTS[name])
        prompts = self.prompt_defaults()
        if prompts:
            if not self.path("prompts.json").exists():
                self.save("prompts.json", prompts)
            if not self.path("prompts.default.json").exists():
                self.save("prompts.default.json", prompts)
        if not self.path("ai.example.json").exists():
            example = dict(DEFAULTS["ai.json"])
            example["api_key"] = "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            self.save("ai.example.json", example)

    # ---------- 读写 ----------
    def load(self, name: str) -> dict:
        file_path = self.path(name)
        with self._lock:
            try:
                mtime = file_path.stat().st_mtime
            except FileNotFoundError:
                if name == "ai.json":
                    raise ConfigError(
                        f"缺少 {file_path}。请把 config/ai.example.json 复制成 config/ai.json 并填入 api_key。"
                    ) from None
                mtime = -1.0

            cached = self._cache.get(name)
            if cached and cached[0] == mtime:
                return cached[1]

            data: dict = {}
            if mtime >= 0:
                try:
                    with open(file_path, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except json.JSONDecodeError as exc:
                    raise ConfigError(f"{file_path} 不是合法 JSON：{exc}") from exc
                if not isinstance(data, dict):
                    raise ConfigError(f"{file_path} 顶层必须是对象")

            merged = _deep_merge(DEFAULTS.get(name, {}), data)
            self._cache[name] = (mtime, merged)
            return merged

    def save(self, name: str, data: dict) -> None:
        file_path = self.path(name)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = file_path.with_suffix(file_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            tmp.replace(file_path)
            self._cache.pop(name, None)

    def update(self, name: str, patch: dict) -> dict:
        """深度合并写回，保留用户手写的其它键。"""
        current = self.load(name)
        merged = _deep_merge(current, patch)
        self.save(name, merged)
        return merged

    # ---------- 便捷访问 ----------
    @property
    def app(self) -> dict:
        return self.load("app.json")

    @property
    def fsrs(self) -> dict:
        return self.load("fsrs.json")

    @property
    def ai(self) -> dict:
        return self.load("ai.json")

    @property
    def prompts(self) -> dict:
        return self.load("prompts.json")

    def prompt_defaults(self) -> dict:
        """出厂提示词，用于"恢复默认"。

        优先读 ``config/prompts.default.json``（可被用户替换），没有就用包里自带的
        ``app/defaults/prompts.json``。程序只读这两个文件，不会被保存覆盖。
        """
        candidates = [
            self.path("prompts.default.json"),
            Path(__file__).resolve().parent / "defaults" / "prompts.json",
        ]
        for file_path in candidates:
            if not file_path.exists():
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data:
                return data
        return {}

    # ---------- 业务便捷值 ----------
    def timezone(self) -> str:
        return self.app.get("timezone") or "Asia/Shanghai"

    def current_container(self) -> tuple[str, str]:
        data = self.app
        return (data.get("current_container") or "", data.get("current_container_type") or "book")

    def set_current_container(self, name: str, ctype: str) -> None:
        self.update("app.json", {"current_container": name, "current_container_type": ctype})

    def intake_limits(self) -> dict:
        data = self.app
        return {
            "learning_per_round": max(0, int(data.get("learning_per_round") or 0)),
            "review_per_round": max(0, int(data.get("review_per_round") or 0)),
            "preview_multiplier": max(1, int(data.get("preview_multiplier") or 1)),
            "proficiency_days": max(1, int(data.get("proficiency_days") or 365)),
        }

    def sentence_history_size(self) -> int:
        """每个词保留多少条历史例句（0 表示不记录、不注入）。"""
        try:
            value = int(self.app.get("sentence_history"))
        except (TypeError, ValueError):
            value = 5
        return max(0, min(50, value))
