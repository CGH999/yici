"""精简版 FSRS：难度 D、稳定性 S、下次复习时间。

公式完全来自用户给定的 ``calculate_first_learning`` / ``calculate_review``。
唯一的补充是 ``failure_stability_cap_seconds``：``first_pass=False`` 时把新的 S 封顶，
避免 "S × 0.7" 把间隔拖得过长。该值设为 0 或 None 即完全关闭，恢复原公式。

本模块是纯函数，不碰数据库、不碰时间来源（now 由调用方传入），便于单测。
"""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_CONFIG: dict[str, float] = {
    "initial_difficulty": 3.0,
    "initial_stability": 86400.0,  # 1 天（秒）
    "target_d": 3.0,
    "alpha": 0.8,
    "beta": 0.4,
    "damping": 0.02,
    "min_difficulty": 1.0,
    "max_difficulty": 10.0,
    "failure_stability_cap_seconds": 86400,
    # 两次复习之间的最短间隔（秒）。S 掉到几小时时会把 due 顶到这个下限，
    # 避免同一个词同一天被反复抽到；0 = 关闭，恢复纯公式行为。
    "min_interval_seconds": 86400.0,
}

_SUCCESS_MULTIPLIER = 1.8
_FAILURE_FLOOR = 0.3


def normalize_config(config: Mapping[str, Any] | None = None) -> dict[str, float]:
    """补齐缺省值并做类型转换，缺 key 不会在答题时才炸。"""
    merged = dict(DEFAULT_CONFIG)
    if config:
        for key in DEFAULT_CONFIG:
            if key in config and config[key] is not None:
                merged[key] = float(config[key])
    return merged


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any, fallback: float) -> float:
    try:
        if value is None:
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _apply_failure_cap(stability: float, first_pass: bool, cfg: Mapping[str, float]) -> float:
    cap = cfg.get("failure_stability_cap_seconds") or 0
    if (not first_pass) and cap and cap > 0:
        return min(stability, float(cap))
    return stability


def _schedule(
    *,
    first_pass: bool,
    error_count: int,
    current_ts: float,
    difficulty: float,
    stability: float,
    cfg: Mapping[str, float],
) -> dict:
    if error_count < 0:
        raise ValueError("error_count 不能为负数")
    if first_pass:
        error_count = 0

    # --- 难度：答对降，答错升，并向 target_d 均值回归 ---
    delta = -cfg["alpha"] if first_pass else cfg["beta"] * error_count
    new_difficulty = difficulty + delta + cfg["damping"] * (cfg["target_d"] - difficulty)
    new_difficulty = _clamp(new_difficulty, cfg["min_difficulty"], cfg["max_difficulty"])

    # --- 稳定性：答对 ×1.8，答错按次数衰减（下限 0.3 倍）---
    if first_pass:
        multiplier = _SUCCESS_MULTIPLIER
    else:
        multiplier = max(_FAILURE_FLOOR, 1.0 - 0.3 * error_count)
    new_stability = stability * multiplier
    new_stability = _apply_failure_cap(new_stability, first_pass, cfg)

    # 到期时间 = max(按 S 算出来的, 完成时间 + 最短间隔)。
    # 只顶 due，不改 S——否则 S×1.8 再叠加下限，间隔会膨胀过快。
    computed = current_ts + new_stability
    floor_seconds = max(0.0, float(cfg.get("min_interval_seconds") or 0.0))
    next_review = max(computed, current_ts + floor_seconds)

    return {
        "difficulty": round(new_difficulty, 4),
        "stability": round(new_stability, 2),
        "next_review": round(next_review, 2),
    }


def first_learning(
    first_pass: bool,
    error_count: int,
    current_ts: float,
    config: Mapping[str, Any] | None = None,
) -> dict:
    """单词首次学习完成后的 D / S / 下次复习时间。"""
    cfg = normalize_config(config)
    return _schedule(
        first_pass=first_pass,
        error_count=error_count,
        current_ts=current_ts,
        difficulty=cfg["initial_difficulty"],
        stability=cfg["initial_stability"],
        cfg=cfg,
    )


def review_pass(
    first_pass: bool,
    error_count: int,
    current_ts: float,
    old_difficulty: float | None,
    old_stability: float | None,
    config: Mapping[str, Any] | None = None,
) -> dict:
    """一次复习后的 D / S / 下次复习时间。

    旧值缺失（脏数据 / 手工导入）时退回配置初值，不让 None 参与运算。
    """
    cfg = normalize_config(config)
    return _schedule(
        first_pass=first_pass,
        error_count=error_count,
        current_ts=current_ts,
        difficulty=_as_float(old_difficulty, cfg["initial_difficulty"]),
        stability=_as_float(old_stability, cfg["initial_stability"]),
        cfg=cfg,
    )
