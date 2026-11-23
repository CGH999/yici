"""主页、建任务、problem 生成进度。"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.services import clock
from app.views._helpers import active_round, cfg, current_container, round_state, svc, tm

bp = Blueprint("home", __name__)

# 非本机设备的令牌尝试次数（防止被人慢慢猜 6 位数字）
_ATTEMPTS: dict[str, tuple[int, float]] = {}
_ATTEMPTS_LOCK = threading.Lock()
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 60


@bp.route("/unlock", methods=["GET", "POST"])
def unlock():
    """非本机设备输入 6 位访问令牌。"""
    error: str | None = None
    if request.method == "POST":
        address = request.remote_addr or ""
        now = time.time()
        with _ATTEMPTS_LOCK:
            count, started = _ATTEMPTS.get(address, (0, now))
            if now - started > _WINDOW_SECONDS:
                count, started = 0, now
            if count >= _MAX_ATTEMPTS:
                return render_template("unlock.html", error="试太多次了，等一分钟再来"), 429

        entered = (request.form.get("token") or "").strip()
        expected = str((cfg().app.get("server") or {}).get("token") or "")
        if expected and entered and secrets.compare_digest(entered, expected):
            session["lan_token"] = expected
            session.permanent = True
            with _ATTEMPTS_LOCK:
                _ATTEMPTS.pop(address, None)
            return redirect(url_for("home.index"))

        with _ATTEMPTS_LOCK:
            _ATTEMPTS[address] = (count + 1, started)
        error = "令牌不对，再看一眼电脑上的那 6 位数字"

    return render_template("unlock.html", error=error)


@bp.get("/api/ping")
def ping():
    """就绪探测：启动脚本在打开浏览器前会轮询这个地址。"""
    return jsonify({"ok": True})


@bp.get("/")
def index():
    container, ctype = current_container()
    if not container:
        return render_template("index.html", empty=True, stats=None, today=None, unfinished=[])

    manager = tm()
    stats = svc("stats")
    info = manager.container_stats(container, ctype)
    today = stats.today_counts(container)
    unfinished = [item for item in manager.unfinished_tasks() if item["container"] == container]
    totals = stats.totals(container, ctype)
    now = datetime.now(clock.tzinfo(svc("config").timezone()))
    today_label = now.strftime("%m/%d %a")
    return render_template(
        "index.html",
        empty=False,
        stats=info,
        today=today,
        unfinished=unfinished,
        totals=totals,
        streak=stats.streak(container),
        today_label=today_label,
    )


def _requested_kind(default: str = "") -> str:
    """从 query / JSON / 表单里取 kind，取不到就用传进来的默认值。"""
    kind = (request.args.get("kind") or "").strip()
    if kind not in ("learning", "review"):
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind") or "").strip()
    if kind not in ("learning", "review"):
        kind = (request.form.get("kind") or "").strip() if request.form else ""
    if kind not in ("learning", "review"):
        kind = default
    return kind


@bp.post("/study/start")
def start():
    kind = request.form.get("kind")
    if kind not in ("learning", "review"):
        flash("未知任务类型", "error")
        return redirect(url_for("home.index"))
    container, ctype = current_container()

    balanced, balance_data = svc("balance").can_start()
    if not balanced:
        flash(f"余额 ¥{balance_data.get('amount', 0):.2f} 已低于警戒线，先充值再学", "error")
        return redirect(url_for("home.index"))

    force = request.form.get("force") == "1"
    if tm().has_unfinished(container, ctype, kind) and not force:
        return redirect(url_for("home.conflict", kind=kind))

    result = tm().create_task(container, ctype, kind)
    if result["created"] == 0:
        message = "今天没有新词了" if kind == "learning" else "今天没有到期单词"
        flash(message, "info")
        return redirect(url_for("home.index"))

    round_state().start(container, ctype, kind)
    return redirect(url_for("home.prepare", kind=kind))


@bp.get("/study/conflict")
def conflict():
    container, ctype = current_container()
    kind = request.args.get("kind", "learning")
    progress = tm().progress(container, kind)
    return render_template("conflict.html", kind=kind, progress=progress, container=container)


@bp.get("/study/prepare")
def prepare():
    kind = _requested_kind(round_state().last_kind())
    state = active_round(kind)
    if not state["kind"]:
        flash("没有正在进行的学习任务", "error")
        return redirect(url_for("home.index"))
    info = tm().problems(state["container"], state["kind"])
    return render_template("prepare.html", state=state, info=info)


@bp.post("/api/study/generate")
def generate():
    kind = _requested_kind(round_state().last_kind())
    state = active_round(kind)
    if not state["kind"]:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    started = _spawn_generation(current_app_obj(), state["container"], state["kind"], state["ctype"])
    return jsonify({"ok": True, "started": started})


@bp.get("/api/study/progress")
def progress():
    kind = _requested_kind(round_state().last_kind())
    state = active_round(kind)
    if not state["kind"]:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    info = tm().problems(state["container"], state["kind"])
    snapshot = round_state().snapshot(state["kind"])
    info["generating"] = snapshot["generating"]
    info["error"] = snapshot["error"]
    info["kind"] = state["kind"]
    info["container"] = state["container"]
    return jsonify(info)


@bp.post("/api/study/retry")
def retry():
    kind = _requested_kind(round_state().last_kind())
    state = active_round(kind)
    if not state["kind"]:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    payload = request.get_json(silent=True) or {}
    uids = payload.get("uids")
    container, kind = state["container"], state["kind"]
    targets = tm().retry_problems(container, kind, uids)
    started = _spawn_generation(current_app_obj(), container, kind, state["ctype"])
    return jsonify({"ok": True, "retried": len(targets), "started": started})


@bp.post("/api/study/skip")
def skip():
    kind = _requested_kind(round_state().last_kind())
    state = active_round(kind)
    if not state["kind"]:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    if not uid:
        return jsonify({"ok": False, "error": "缺少 uid"}), 400
    tm().skip_problem(state["container"], state["kind"], uid)
    return jsonify({"ok": True})


def current_app_obj():
    from flask import current_app

    return current_app._get_current_object()


def _spawn_generation(app, container: str, kind: str, ctype: str = "book") -> bool:
    state = round_state()
    if not state.begin_generation(kind):
        return False

    manager = app.extensions["tm"]
    pool = app.extensions["pool"]

    def worker() -> None:
        try:
            items = manager.pending_problems(container, kind, ctype)
            result = pool.generate_many(
                items,
                on_success=lambda uid, problem: manager.set_problem(
                    container, kind, uid, problem, ctype=ctype
                ),
                on_error=lambda uid, message: manager.fail_problem(container, kind, uid, message),
            )
            if result.get("stopped"):
                state.set_error(kind, result["stopped"])
        except Exception as exc:  # 线程里不能冒泡
            state.set_error(kind, f"{type(exc).__name__}: {exc}")
        finally:
            state.end_generation(kind)

    threading.Thread(target=worker, name=f"problem-generation-{kind}", daemon=True).start()
    return True
