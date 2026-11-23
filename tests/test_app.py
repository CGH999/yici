"""应用级冒烟测试：不联网，只验证页面能渲染、路由能通。"""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from app import create_app
from app.config import Config
from app.db.manager import DBManager
from app.db.schema import ensure_container_table, ensure_indexes

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "config").mkdir(parents=True)
        shutil.copy(PROJECT_ROOT / "config" / "fsrs.json", base / "config" / "fsrs.json")
        shutil.copy(PROJECT_ROOT / "config" / "prompts.json", base / "config" / "prompts.json")
        (base / "config" / "ai.json").write_text(
            json.dumps({"api_key": "sk-test-not-used", "model": "deepseek-chat"}),
            encoding="utf-8",
        )
        (base / "config" / "app.json").write_text(
            json.dumps(
                {
                    "timezone": "Asia/Shanghai",
                    "current_container": "cet4",
                    "learning_per_round": 3,
                    "review_per_round": 3,
                    "preview_multiplier": 2,
                    "proficiency_days": 365,
                    "balance": {"warning": 0, "refresh_seconds": 3600},
                }
            ),
            encoding="utf-8",
        )
        self.config = Config(base)

        # 先建库填词，再 create_app
        manager = DBManager.from_config(self.config)
        table = ensure_container_table(manager.get("library"), "cet4")
        ensure_indexes(manager.get("library"), "cet4")
        for i, word in enumerate(("able", "county", "damage", "eager")):
            table.insert(
                {
                    "uid": f"uid-{word}",
                    "word": word,
                    "translation": f"释义-{word}",
                    "state": "new",
                    "reps": 0,
                    "lapses": 0,
                }
            )
        manager.close_all()

        self.app = create_app(self.config)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["db"].close_all()
        self.tmp.cleanup()

    def test_all_pages_render(self):
        for url in (
            "/",
            "/dashboard",
            "/me",
            "/me/tools",
            "/me/tools/preview",
            "/me/wordbooks",
            "/me/favorites",
            "/me/settings",
            "/me/wordbank/cet4",
            "/me/wordbank/cet4?state=new",
            "/me/wordbank/cet4?state=due",
            "/study/prepare",
            "/api/balance",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(response.status_code, (200, 302), f"{url} -> {response.status_code}")

    def test_start_learning_creates_task(self):
        response = self.client.post("/study/start", data={"kind": "learning"}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/study/prepare", response.headers["Location"])
        progress = self.client.get("/api/study/progress").get_json()
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["pending"], 3)

    def test_session_redirects_without_ready_problems(self):
        self.client.post("/study/start", data={"kind": "learning"})
        response = self.client.get("/study/session", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/study/prepare", response.headers["Location"])

    def test_settings_save(self):
        response = self.client.post(
            "/me/settings",
            data={
                "learning_per_round": "5",
                "review_per_round": "7",
                "preview_multiplier": "3",
                "proficiency_days": "180",
                "pass_mode": "weighted",
                "sentence_min": "0.7",
                "keyword_min": "0.75",
                "total_min": "0.7",
                "balance_warning": "2",
                "balance_refresh": "600",
                "concurrency_enabled": "on",
                "concurrency_mode": "serial",
                "workers": "1",
                "warmup_requests": "0",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        cfg = self.config.app
        self.assertEqual(cfg["learning_per_round"], 5)
        self.assertEqual(cfg["pass_rule"]["mode"], "weighted")
        self.assertEqual(self.config.ai["concurrency"]["mode"], "serial")

    def test_switch_container(self):
        response = self.client.post("/api/container/switch", json={"name": "cet4", "ctype": "book"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_empty_answer_counts_as_wrong_without_ai(self):
        """空白提交直接算错，不花 token。"""
        self.client.post("/study/start", data={"kind": "learning"})
        self.app.extensions["tm"].set_problem(
            "cet4", "learning", "uid-able", {"sentence": "He is able.", "translation": "他很能干。"}
        )
        calls = {"n": 0}

        def should_not_be_called(*args, **kwargs):
            calls["n"] += 1
            raise AssertionError("空白答案不应该调用 AI")

        self.app.extensions["ai"].score_answer = should_not_be_called
        result = self.client.post(
            "/api/study/submit", json={"uid": "uid-able", "answer": "   "}
        ).get_json()
        self.assertTrue(result["ok"])
        self.assertTrue(result["empty"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["task_state"], -1)
        self.assertEqual(result["next"], "error")
        self.assertEqual(calls["n"], 0)

    def test_server_settings_saved(self):
        response = self.client.post(
            "/me/settings",
            data={"server_port": "5050", "server_allow_lan": "on", "api_key": "sk-test-not-used"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        server = self.config.app["server"]
        self.assertEqual(server["port"], 5050)
        self.assertTrue(server["allow_lan"])

    def test_invalid_port_rejected(self):
        before = self.config.app["server"]["port"]
        self.client.post(
            "/me/settings",
            data={"server_port": "70000", "api_key": "sk-test-not-used"},
            follow_redirects=True,
        )
        self.assertEqual(self.config.app["server"]["port"], before)

    def test_non_local_requests_blocked_by_default(self):
        response = self.client.get("/", environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(response.status_code, 403)

    def test_lan_enabled_requires_token(self):
        self.config.update("app.json", {"server": {"allow_lan": True}})
        token = self.config.app["server"]["token"]
        remote = {"REMOTE_ADDR": "192.168.1.9"}

        blocked = self.client.get("/", environ_overrides=remote)
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/unlock", blocked.headers["Location"])

        wrong = self.client.post("/unlock", data={"token": "000000" if token != "000000" else "111111"},
                                 environ_overrides=remote)
        self.assertEqual(wrong.status_code, 200)

        right = self.client.post("/unlock", data={"token": token}, environ_overrides=remote)
        self.assertEqual(right.status_code, 302)
        self.assertEqual(self.client.get("/", environ_overrides=remote).status_code, 200)

    def test_api_requires_token_too(self):
        self.config.update("app.json", {"server": {"allow_lan": True}})
        response = self.client.get("/api/balance", environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(response.status_code, 401)

    def test_local_is_never_asked_for_token(self):
        self.config.update("app.json", {"server": {"allow_lan": True}})
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_token_can_be_regenerated(self):
        old = self.config.app["server"]["token"]
        response = self.client.post("/api/server/token", json={})
        self.assertEqual(response.status_code, 200)
        new = response.get_json()["token"]
        self.assertEqual(len(new), 6)
        self.assertTrue(new.isdigit())
        self.assertEqual(self.config.app["server"]["token"], new)
        self.assertNotEqual(old, new)

    def test_import_wordbook_endpoint(self):
        payload = {
            "name": "cet6",
            "ctype": "book",
            "text": json.dumps({"data": [{"word": "alpha", "translation": "阿尔法"}]}),
        }
        response = self.client.post("/api/container/import", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["inserted"], 1)
        self.assertIn("cet6", self.app.extensions["tm"].containers("book"))

    def test_import_rejects_bad_json(self):
        payload = {"name": "badbook", "ctype": "book", "text": "{oops"}
        response = self.client.post("/api/container/import", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertNotIn("badbook", self.app.extensions["tm"].containers("book"))

    def test_correct_answer_goes_to_correct_page(self):
        """答对后必须先给"答对了"页（词义 + 标准译文 + 笔记），不是直接跳下一题。"""
        self.client.post("/study/start", data={"kind": "learning"})
        self.app.extensions["tm"].set_problem(
            "cet4", "learning", "uid-able", {"sentence": "He is able.", "translation": "他很能干。"}
        )
        self.app.extensions["ai"].score_answer = lambda *a, **k: {
            "sentence_score": 0.95,
            "keyword_score": 0.95,
        }
        result = self.client.post(
            "/api/study/submit", json={"uid": "uid-able", "answer": "他很能干。"}
        ).get_json()
        self.assertTrue(result["passed"])
        self.assertEqual(result["next"], "correct")

        page = self.client.get(result["correct_url"])
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("答对了", html)
        self.assertIn("他很能干。", html)      # 标准译文
        self.assertIn("释义-able", html)      # 词义
        self.assertIn('id="note"', html)      # 笔记框

    def test_wrong_answer_waits_at_least_a_day(self):
        """答错一次后 S 只有 16.8 小时，但写回的 due 要被顶到 24 小时之后。"""
        self.client.post("/study/start", data={"kind": "learning"})
        tm = self.app.extensions["tm"]
        tm.set_problem(
            "cet4", "learning", "uid-able", {"sentence": "He is able.", "translation": "他很能干。"}
        )

        # 先答错一次：task_state 变 -1，词还留在本轮里，不写回词表
        self.app.extensions["ai"].score_answer = lambda *a, **k: {"sentence_score": 0.1, "keyword_score": 0.1}
        self.client.post("/api/study/submit", json={"uid": "uid-able", "answer": "错的"})
        self.assertEqual(tm.get_word("cet4", "book", "uid-able")["state"], "new")

        # 再连续答对两次，把 task_state 从 -1 推到 1（完成），这时才写回
        self.app.extensions["ai"].score_answer = lambda *a, **k: {"sentence_score": 0.95, "keyword_score": 0.95}
        before = int(time.time())
        self.client.post("/api/study/submit", json={"uid": "uid-able", "answer": "他很能干。"})
        result = self.client.post("/api/study/submit", json={"uid": "uid-able", "answer": "他很能干。"}).get_json()
        self.assertTrue(result["finished"])

        word = tm.get_word("cet4", "book", "uid-able")
        self.assertEqual(word["state"], "review")
        self.assertGreaterEqual(word["due"], before + 86400)

    def test_settings_total_min_is_independent(self):
        """总分线不能被 sentence_min 顶掉。"""
        self.client.post(
            "/me/settings",
            data={
                "learning_per_round": "5",
                "sentence_min": "0.6",
                "keyword_min": "0.8",
                "total_min": "0.75",
                "pass_mode": "weighted",
            },
            follow_redirects=True,
        )
        rule = self.config.app["pass_rule"]
        self.assertEqual(rule["sentence_min"], 0.6)
        self.assertEqual(rule["total_min"], 0.75)

    def test_api_key_can_be_updated(self):
        response = self.client.post(
            "/me/settings", data={"api_key": "sk-abcdefghijklmnopqrst"}, follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.ai["api_key"], "sk-abcdefghijklmnopqrst")
        # 设置页要能看到当前 key（用户要求）
        self.assertIn("sk-abcdefghijklmnopqrst", response.get_data(as_text=True))

    def test_api_key_can_be_cleared(self):
        self.client.post("/me/settings", data={"api_key": ""}, follow_redirects=True)
        self.assertEqual(self.config.ai["api_key"], "")

    def test_prompts_can_be_edited_and_restored(self):
        self.client.post(
            "/me/settings",
            data={
                "api_key": "sk-test-not-used",
                "prompt_generate": "自定义造例句提示词",
                "prompt_check": "自定义评分提示词",
            },
            follow_redirects=True,
        )
        prompts = self.config.prompts
        self.assertEqual(prompts["generate_sentence"]["system"], "自定义造例句提示词")
        self.assertEqual(prompts["check_answer"]["system"], "自定义评分提示词")

        response = self.client.post("/api/prompt/restore", json={"which": "all"})
        self.assertEqual(response.status_code, 200)
        defaults = self.config.prompt_defaults()
        self.assertEqual(
            self.config.prompts["generate_sentence"]["system"], defaults["generate_sentence"]["system"]
        )

    def test_empty_prompt_is_not_saved(self):
        self.client.post(
            "/me/settings",
            data={"api_key": "sk-test-not-used", "prompt_generate": "  ", "prompt_check": ""},
            follow_redirects=True,
        )
        self.assertNotEqual(self.config.prompts.get("generate_sentence", {}).get("system"), "")

    def test_reset_clears_user_data_but_keeps_books(self):
        manager = self.app.extensions["tm"]
        manager.create_task("cet4", "book", "learning")
        manager.set_problem("cet4", "learning", "uid-able", {"sentence": "x"})
        manager.submit("cet4", "book", "learning", "uid-able", passed=True)
        manager.set_note("cet4", "book", "uid-able", "我的笔记")
        manager.toggle_favorite("cet4", "book", "uid-able")
        self.assertEqual(manager.get_word("cet4", "book", "uid-able")["state"], "review")

        result = self.client.post("/api/reset", json={"clear_api_key": False}).get_json()
        self.assertTrue(result["ok"])

        word = manager.get_word("cet4", "book", "uid-able")
        self.assertEqual(word["state"], "new")
        self.assertIsNone(word["difficulty"])
        self.assertIsNone(word["due"])
        self.assertIsNone(word["note"])
        self.assertFalse(bool(word["favorite"]))
        self.assertEqual(int(word["reps"]), 0)

        history = self.app.extensions["db"].get("history")
        self.assertEqual(list(history.query("SELECT COUNT(*) c FROM history"))[0]["c"], 0)
        self.assertIn("cet4", manager.containers("book"))
        self.assertTrue(self.config.ai["api_key"].startswith("sk-test"))

    def test_reset_can_clear_api_key(self):
        self.client.post("/api/reset", json={"clear_api_key": True})
        self.assertEqual(self.config.ai["api_key"], "")

    def test_cannot_delete_the_last_book(self):
        response = self.client.post("/api/container/delete", json={"name": "cet4", "ctype": "book"})
        self.assertEqual(response.status_code, 400)

    def test_delete_book_and_switch_current(self):
        manager = self.app.extensions["tm"]
        manager.create_container("book", "cet6")
        self.config.set_current_container("cet6", "book")
        response = self.client.post("/api/container/delete", json={"name": "cet6", "ctype": "book"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("cet6", manager.containers("book"))
        self.assertEqual(self.config.current_container()[0], "cet4")

    def test_bad_api_key_rejected(self):
        self.client.post("/me/settings", data={"api_key": "bad-key"}, follow_redirects=True)
        self.assertEqual(self.config.ai["api_key"], "sk-test-not-used")

    def test_mark_filter_escapes_once(self):
        mark = self.app.jinja_env.filters["mark"]
        rendered = str(mark("Don't abandon your friends.", "abandon"))
        self.assertIn("<mark>abandon</mark>", rendered)
        self.assertIn("Don&#x27;t", rendered)
        self.assertNotIn("&lt;mark&gt;", rendered)

    def test_mark_filter_tolerates_inflection(self):
        mark = self.app.jinja_env.filters["mark"]
        self.assertIn("<mark>abandoned</mark>", str(mark("He abandoned it.", "abandon")))

    def test_unknown_container_rejected(self):
        response = self.client.post("/api/container/switch", json={"name": "nope", "ctype": "book"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
