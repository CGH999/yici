"""过关判定：把 AI 给的两个分数翻译成"过 / 不过"。

AI 只负责打分，判不判过是这里的事——这样改阈值只动配置，不动提示词
（提示词是缓存前缀，改一个字缓存全失效）。
"""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_RULE: dict[str, Any] = {
    "mode": "and",
    "sentence_min": 0.8,
    "keyword_min": 0.8,
    "sentence_weight": 0.5,
    "keyword_weight": 0.5,
    "total_min": 0.8,
}

MODES = ("and", "weighted")


def normalize_rule(rule: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(DEFAULT_RULE)
    if rule:
        merged.update({k: v for k, v in rule.items() if v is not None})
    if merged["mode"] not in MODES:
        raise ValueError(f"pass_rule.mode 必须是 {MODES} 之一，当前为 {merged['mode']!r}")
    for key in ("sentence_min", "keyword_min", "sentence_weight", "keyword_weight", "total_min"):
        merged[key] = float(merged[key])
    return merged


def clamp_score(value: float) -> float:
    """把分数夹到 [0, 1]；非数字抛 ValueError。"""
    score = float(value)
    return max(0.0, min(1.0, score))


def total_score(sentence_score: float, keyword_score: float, rule: Mapping[str, Any] | None = None) -> float:
    """加权总分，只在 weighted 模式有意义，界面展示也可以用它。"""
    cfg = normalize_rule(rule)
    return cfg["sentence_weight"] * clamp_score(sentence_score) + cfg["keyword_weight"] * clamp_score(keyword_score)


def judge(sentence_score: float, keyword_score: float, rule: Mapping[str, Any] | None = None) -> bool:
    """判定这一次作答是否过关。"""
    cfg = normalize_rule(rule)
    sentence_score = clamp_score(sentence_score)
    keyword_score = clamp_score(keyword_score)

    if cfg["mode"] == "and":
        return sentence_score >= cfg["sentence_min"] and keyword_score >= cfg["keyword_min"]
    return total_score(sentence_score, keyword_score, cfg) >= cfg["total_min"]
