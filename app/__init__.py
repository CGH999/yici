"""Flask 应用装配。

单用户本地应用：一个进程、一个 ``Config``、四个 SQLite 连接、一个 AI 客户端。
"""

from __future__ import annotations

import atexit
import ipaddress
import secrets

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from app.config import Config
from app.db import schema
from app.db.manager import DBManager
from app.services.ai_client import AIClient
from app.services.ai_pool import AIPool
from app.services.balance import Balance
from app.services.round_state import Round
from app.services.stats import Stats
from app.services.task_manager import TaskManager


def ensure_server_config(config: Config) -> dict:
    """确保有会话密钥和 6 位访问令牌；缺了就生成并写回配置。"""
    server = dict(config.app.get("server") or {})
    changed: dict = {}
    if not server.get("secret"):
        changed["secret"] = secrets.token_hex(16)
    if not server.get("token"):
        changed["token"] = f"{secrets.randbelow(1_000_000):06d}"
    if changed:
        config.update("app.json", {"server": changed})
        server.update(changed)
    return server


def new_token() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def is_loopback(address: str | None) -> bool:
    try:
        return ipaddress.ip_address(address or "").is_loopback
    except ValueError:
        return False


def create_app(config: Config | None = None, allow_lan: bool | None = None) -> Flask:
    """:param allow_lan: 命令行覆盖项；None 表示按配置文件来。"""
    config = config or Config()
    config.bootstrap()
    server_cfg = ensure_server_config(config)

    manager = DBManager.from_config(config)
    schema.ensure_library(manager, config)
    atexit.register(manager.close_all)

    task_manager = TaskManager(manager, config)
    try:
        task_manager.recover()
    except Exception:
        pass
    try:
        task_manager.cleanup_spelling()
    except Exception:
        pass
    try:
        task_manager.migrate_served_seq()
    except Exception:
        pass

    client = AIClient(config)
    pool = AIPool(client, config)
    balance = Balance(client, config)
    stats = Stats(manager, config, task_manager)
    round_state = Round()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json.ensure_ascii = False
    app.config["ALLOW_LAN_OVERRIDE"] = allow_lan
    # 用配置里的随机密钥签名会话：别人拿到源码也伪造不了"已解锁"的 cookie
    app.config["SECRET_KEY"] = server_cfg.get("secret") or secrets.token_hex(16)
    app.extensions.update(
        config=config,
        db=manager,
        tm=task_manager,
        ai=client,
        pool=pool,
        balance=balance,
        stats=stats,
        round=round_state,
    )

    _ensure_current_container(config, task_manager)

    from app.views import dashboard, home, me, study

    @app.before_request
    def _guard_non_local():
        """本机直通；其它设备需要在设置里打开开关，并输入 6 位令牌。"""
        if request.endpoint in ("static", "home.unlock"):
            return None
        address = request.remote_addr or ""
        if is_loopback(address):
            return None

        server = config.app.get("server") or {}
        allow = bool(server.get("allow_lan"))
        override = app.config.get("ALLOW_LAN_OVERRIDE")
        if override is not None:
            allow = bool(override)
        if not allow:
            return (
                render_template(
                    "error_page.html",
                    code=403,
                    message=f"只允许本机访问（当前请求来自 {address or '未知地址'}）。"
                    "如果需要用手机或别的电脑打开，请到「设置 → 服务器」打开“允许非本机访问”后重启程序。",
                ),
                403,
            )

        token = str(server.get("token") or "")
        if token and session.get("lan_token") == token:
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "需要访问令牌", "unlock": url_for("home.unlock")}), 401
        return redirect(url_for("home.unlock"))

    app.register_blueprint(home.bp)
    app.register_blueprint(study.bp)
    app.register_blueprint(me.bp)
    app.register_blueprint(dashboard.bp)

    @app.context_processor
    def inject_globals():
        container, ctype = config.current_container()
        try:
            balance_data = balance.current()
        except Exception:
            balance_data = {"amount": 0, "currency": "CNY", "error": "查询失败"}
        return {
            "app_name": config.app.get("app_name") or "译词",
            "current_container": container,
            "current_ctype": ctype,
            "balance": balance_data,
            "balance_warning": balance.warning_line(),
            "pass_rule": config.app.get("pass_rule") or {},
            "round": round_state.snapshot(),
        }

    @app.template_filter("short")
    def short(value, length=40):
        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= length else text[: length - 1] + "…"

    @app.template_filter("mark")
    def mark(sentence, word):
        """把句子里的目标词标出来（容忍词形变化，找不到就原样返回）。

        返回值包成 Markup：内部已经 escape 过一次，不能再让 Jinja 自动转义，
        否则 ``<mark>`` 会变成字面量、``'`` 会变成 ``&#x27;``。
        """
        import html
        import re

        from markupsafe import Markup

        text = html.escape(str(sentence or ""))
        token = str(word or "").strip()
        if not token:
            return Markup(text)
        safe = re.escape(html.escape(token))
        pattern = re.compile(rf"\b{safe}[a-z]{{0,3}}\b", re.IGNORECASE)
        if not pattern.search(text):
            return Markup(text)
        return Markup(pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", text))

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error_page.html", code=404, message="页面不存在"), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("error_page.html", code=500, message="服务器出错了，看一眼控制台日志"), 500

    return app


def _ensure_current_container(config: Config, task_manager: TaskManager) -> None:
    """当前容器失配时自动纠正，避免整站空着。"""
    container, ctype = config.current_container()
    books = task_manager.containers("book")
    wordbooks = task_manager.containers("wordbook")
    if container and container in (books if ctype == "book" else wordbooks):
        return
    if books:
        config.set_current_container(books[0], "book")
    elif wordbooks:
        config.set_current_container(wordbooks[0], "wordbook")
    else:
        config.set_current_container("", "book")
