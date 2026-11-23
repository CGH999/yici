"""AI 连通性自检：余额 → 造例句 → 评分。

    python tools/check_ai.py

顺带验证 ``thinking`` / ``reasoning_effort`` / ``response_format`` 是否被服务端接受：
被拒时客户端会自动降级并在下面的输出里标出来。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.services.ai_client import AIClient, AIError  # noqa: E402
from app.services.balance import Balance  # noqa: E402


def main() -> int:
    config = Config(PROJECT_ROOT)
    client = AIClient(config)

    print(f"model = {config.ai.get('model')}")

    try:
        balance = Balance(client, config)
        data = balance.current(force=True)
        print(f"balance = {data.get('amount')} {data.get('currency')}  available={data.get('available')}")
        if data.get("error"):
            print(f"balance_error = {data['error']}")
    except Exception as exc:
        print(f"balance FAILED: {type(exc).__name__}: {exc}")

    started = time.time()
    try:
        problem = client.generate_sentence("county", "县；郡")
        print(f"generate OK in {time.time() - started:.1f}s")
        print(f"  sentence  = {problem['sentence']}")
        print(f"  translation = {problem['translation']}")
        print(f"  keyword_form = {problem['keyword_form']}")
    except AIError as exc:
        print(f"generate FAILED: {type(exc).__name__}: {exc}")
        return 1

    started = time.time()
    try:
        scores = client.score_answer(problem["sentence"], "这个县非常安静。", "county", problem["translation"])
        print(f"score OK in {time.time() - started:.1f}s -> {scores}")
    except AIError as exc:
        print(f"score FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"reasoning_params_accepted = {client._supports_reasoning}")
    print(f"json_format_accepted      = {client._supports_json_format}")
    print(f"usage = {client.usage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
