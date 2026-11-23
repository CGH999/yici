"""DeepSeek 客户端：对外只有三个接口。

* :meth:`AIClient.generate_sentence` 造例句
* :meth:`AIClient.score_answer` 给用户译文打两个分
* :meth:`AIClient.get_balance` 查余额

约定：

* 返回值一律已校验，字段缺失/越界抛 :class:`AIResponseError`
* AI 失败**绝不等价于用户答错**，错误由上层决定如何降级
* 余额不足抛 :class:`InsufficientBalance`，不重试
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import requests

from app.services.ai_memory import AIMemory


class AIError(RuntimeError):
    """AI 调用基类异常。"""


class AIResponseError(AIError):
    """响应结构不对、分数越界、JSON 解析失败。"""


class InsufficientBalance(AIError):
    """余额不足（HTTP 402 或本地判断）。"""


class AIRateLimited(AIError):
    """被限流（HTTP 429）。"""


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_content(content: str) -> dict:
    """从模型输出里抠出 JSON：容忍 ```json 围栏和前后废话。"""
    text = (content or "").strip()
    text = _FENCE.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise AIResponseError(f"返回的不是 JSON：{text[:120]!r}") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AIResponseError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise AIResponseError("返回的 JSON 顶层必须是对象")
    return data


def _score(value: Any, name: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise AIResponseError(f"{name} 不是数字：{value!r}") from None
    if score < 0 or score > 1:
        score = max(0.0, min(1.0, score))
    return score


class AIClient:
    def __init__(self, config, memory: AIMemory | None = None) -> None:
        self.config = config
        ai = config.ai
        self.memory = memory or AIMemory(config.memory_path, ai.get("max_history", 2))
        self._lock = threading.RLock()
        self._usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0}
        self._supports_reasoning = True
        self._supports_json_format = True

    # ---------------------------------------------------------------- 统计
    @property
    def usage(self) -> dict:
        with self._lock:
            return dict(self._usage)

    def reset_usage(self) -> None:
        with self._lock:
            self._usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0}

    def _record_usage(self, usage: dict) -> None:
        with self._lock:
            self._usage["calls"] += 1
            self._usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            self._usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            self._usage["cache_hit_tokens"] += int(usage.get("prompt_cache_hit_tokens") or 0)

    # ---------------------------------------------------------------- 请求
    def _messages(self, section: str, user_input: str) -> list[dict]:
        prompt = self.config.prompts.get(section) or {}
        system = prompt.get("system")
        if not system:
            raise AIResponseError(f"config/prompts.json 缺少 {section}.system")
        messages: list[dict] = [{"role": "system", "content": system}]
        for pair in prompt.get("few_shot") or []:
            if "user" in pair and "assistant" in pair:
                messages.append({"role": "user", "content": pair["user"]})
                messages.append({"role": "assistant", "content": pair["assistant"]})
        for item in self.memory.history(section):
            messages.append({"role": "user", "content": item["user"]})
            messages.append({"role": "assistant", "content": item["assistant"]})
        messages.append({"role": "user", "content": user_input})
        return messages

    def _build_body(self, section: str, messages: list[dict]) -> dict:
        ai = self.config.ai
        prompt = self.config.prompts.get(section) or {}
        body: dict[str, Any] = {
            "model": ai.get("model") or "deepseek-chat",
            "messages": messages,
            "stream": False,
        }
        temperature = prompt.get("temperature", ai.get("temperature"))
        if temperature is not None:
            body["temperature"] = float(temperature)
        if self._supports_reasoning:
            if ai.get("thinking"):
                body["thinking"] = ai["thinking"]
            if ai.get("reasoning_effort"):
                body["reasoning_effort"] = ai["reasoning_effort"]
        if self._supports_json_format and prompt.get("response_format"):
            body["response_format"] = prompt["response_format"]
        return body

    def _headers(self) -> dict:
        ai = self.config.ai
        api_key = (ai.get("api_key") or "").strip()
        if not api_key or api_key.startswith("sk-xxx"):
            raise AIError("config/ai.json 里没有可用的 api_key")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    def _backoff(self, attempt: int) -> float:
        schedule = (self.config.ai.get("retry") or {}).get("backoff_seconds") or [0.5, 1.0, 2.0]
        try:
            return float(schedule[min(attempt, len(schedule) - 1)])
        except (IndexError, TypeError, ValueError):
            return 1.0

    def _post_once(self, body: dict) -> dict:
        ai = self.config.ai
        url = ai.get("chat_url")
        timeout = (10, float(ai.get("timeout_seconds") or 60))
        response = requests.post(url, headers=self._headers(), json=body, timeout=timeout)
        return self._handle_response(response)

    def _handle_response(self, response: requests.Response) -> dict:
        if response.status_code == 402:
            raise InsufficientBalance("账户余额不足（HTTP 402）")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            error = AIRateLimited("请求被限流（HTTP 429）")
            error.retry_after = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 5.0
            raise error
        if response.status_code == 400:
            text = self._error_text(response)
            if self._supports_reasoning and ("thinking" in text or "reasoning" in text.lower()):
                self._supports_reasoning = False
                raise _RetryWithFallback(f"服务端不接受 thinking/reasoning_effort：{text[:120]}")
            if self._supports_json_format and "response_format" in text:
                self._supports_json_format = False
                raise _RetryWithFallback(f"服务端不接受 response_format：{text[:120]}")
            raise AIError(f"请求被拒绝（HTTP 400）：{text[:200]}")
        if response.status_code >= 500:
            raise AIError(f"服务端错误（HTTP {response.status_code}）")
        if not response.ok:
            raise AIError(f"HTTP {response.status_code}：{self._error_text(response)[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise AIResponseError(f"响应不是 JSON：{exc}") from exc

    @staticmethod
    def _error_text(response: requests.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text[:300]
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)
            if error:
                return str(error)
            if data.get("message"):
                return str(data["message"])
        return str(data)[:300]

    def _chat(self, section: str, user_input: str) -> tuple[str, dict]:
        ai = self.config.ai
        retries = int((ai.get("retry") or {}).get("times") or 3)
        messages = self._messages(section, user_input)
        last_error: Exception | None = None

        for attempt in range(retries):
            body = self._build_body(section, messages)
            try:
                data = self._post_once(body)
            except _RetryWithFallback as exc:
                last_error = exc
                continue  # 参数降级后立刻重试，不消耗退避
            except AIRateLimited as exc:
                last_error = exc
                time.sleep(getattr(exc, "retry_after", 5.0))
                continue
            except InsufficientBalance:
                raise
            except AIError as exc:
                last_error = exc
                time.sleep(self._backoff(attempt))
                continue
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(self._backoff(attempt))
                continue

            choices = data.get("choices") or []
            if not choices:
                last_error = AIResponseError("响应缺少 choices")
                time.sleep(self._backoff(attempt))
                continue
            message = choices[0].get("message") or {}
            content = message.get("content")
            if not content:
                last_error = AIResponseError("响应缺少 content")
                continue
            self._record_usage(data.get("usage") or {})
            return content, data.get("usage") or {}

        raise AIError(f"重试 {retries} 次仍失败：{last_error}")

    # ---------------------------------------------------------------- 业务接口
    def generate_sentence(
        self, word: str, translation: str | None = None, recent_sentences: list[str] | None = None
    ) -> dict:
        """造一个例句。输入是 JSON（含历史例句黑名单），输出也是 JSON。"""
        avoid = [str(item).strip() for item in (recent_sentences or []) if str(item).strip()][-8:]
        payload = json.dumps(
            {
                "word": word,
                "translation": (translation or "").strip(),
                "avoid_sentences": avoid,
            },
            ensure_ascii=False,
        )
        content, _ = self._chat("generate_sentence", payload)
        data = parse_json_content(content)
        sentence = str(data.get("sentence") or "").strip()
        if not sentence:
            raise AIResponseError("生成的句子为空")
        keyword_form = str(data.get("keyword_form") or "").strip() or word
        result = {
            "sentence": sentence,
            "translation": str(data.get("translation") or "").strip(),
            "keyword_form": keyword_form,
            "avoid_sentences": avoid,
            "model": self.config.ai.get("model"),
            "generated_at": int(time.time()),
        }
        self.memory.append("generate_sentence", payload, content)
        return result

    def score_answer(
        self,
        sentence: str,
        user_translation: str,
        keyword: str,
        reference_translation: str | None = None,
    ) -> dict:
        payload = json.dumps(
            {
                "english_sentence": sentence,
                "chinese_translation": user_translation,
                "keyword": keyword,
                "reference_translation": reference_translation or "",
            },
            ensure_ascii=False,
        )
        content, _ = self._chat("check_answer", payload)
        data = parse_json_content(content)
        sentence_score = _score(data.get("sentence_score"), "sentence_score")
        keyword_score = _score(data.get("keyword_score"), "keyword_score")
        self.memory.append("check_answer", payload, content)
        return {"sentence_score": sentence_score, "keyword_score": keyword_score}

    def get_balance(self) -> dict:
        ai = self.config.ai
        response = requests.get(
            ai.get("balance_url"),
            headers=self._headers(),
            timeout=(10, float(ai.get("timeout_seconds") or 60)),
        )
        if response.status_code == 402:
            raise InsufficientBalance("账户余额不足（HTTP 402）")
        if not response.ok:
            raise AIError(f"查询余额失败（HTTP {response.status_code}）")
        try:
            data = response.json()
        except ValueError as exc:
            raise AIResponseError(f"余额响应不是 JSON：{exc}") from exc
        infos = data.get("balance_infos") or []
        if not infos:
            return {"available": False, "amount": 0.0, "currency": "CNY", "fetched_at": int(time.time())}
        info = infos[0]
        amount = float(info.get("total_balance") or 0)
        return {
            "available": bool(data.get("is_available")),
            "amount": amount,
            "currency": info.get("currency") or "CNY",
            "fetched_at": int(time.time()),
        }


class _RetryWithFallback(AIError):
    """内部用：参数不被接受，降级后立刻重试。"""
