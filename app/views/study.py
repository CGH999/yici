"""学习 / 复习 / 拼写 / 结果页。"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

from app.services import clock, grading
from app.services.ai_client import AIError, AIResponseError, InsufficientBalance
from app.views._helpers import active_round, cfg, current_container, round_state, svc, tm

bp = Blueprint("study", __name__)


def _requested_kind() -> str | None:
    """这一轮到底是学习还是复习：优先 URL 上的 ?kind=，其次请求体里的 kind。"""
    kind = (request.args.get("kind") or "").strip()
    if kind not in ("learning", "review") and request.method == "POST":
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind") or "").strip()
    if kind in ("learning", "review"):
        return kind
    return None


def _resume(kind: str | None = None) -> dict:
    """拿到这一轮的状态；内存里没有就从任务库恢复（进程重启 / 换了浏览器）。"""
    container, ctype = current_container()
    state = active_round(kind) if kind else active_round()
    if state["kind"] and state["container"] == container:
        return state
    if not container:
        return state

    candidates = [kind] if kind in ("learning", "review") else ["learning", "review"]
    for candidate in candidates:
        if tm().has_unfinished(container, ctype, candidate):
            round_state().start(container, ctype, candidate)
            return round_state().get(candidate)
    return active_round(kind)


def _round_or_redirect(kind: str | None = None):
    state = _resume(kind)
    if not state["kind"]:
        return None
    return state


def _link(endpoint: str, state: dict) -> str:
    """带上 kind 的站内链接，保证来回跳转不会串到另一轮。"""
    return url_for(endpoint, kind=state["kind"])


@bp.get("/study/session")
def session():
    kind = _requested_kind()
    state = _round_or_redirect(kind)
    if state is None:
        return redirect(url_for("home.index"))
    if tm().problems(state["container"], state["kind"])["ready"] == 0:
        return redirect(url_for("home.prepare", kind=state["kind"]))

    task = tm().next_task(state["container"], state["kind"])
    if task is None:
        return redirect(_link("study.done", state))

    progress = tm().progress(state["container"], state["kind"])
    word = tm().get_word(state["container"], state["ctype"], task["uid"]) or {}
    return render_template(
        "session.html",
        state=state,
        task=task,
        problem=task.get("problem_data") or {},
        progress=progress,
        word=word,
    )


@bp.post("/api/study/submit")
def submit():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400

    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    answer = (payload.get("answer") or "").strip()
    if not uid:
        return jsonify({"ok": False, "error": "缺少题目", "counted": False}), 400

    container, ctype, kind = state["container"], state["ctype"], state["kind"]

    # 空白提交 = 直接算错，不调 AI（省 token，也省一次等待）
    if not answer:
        result = tm().submit(container, ctype, kind, uid, passed=False, answer="")
        return jsonify(
            {
                "ok": True,
                "empty": True,
                "passed": False,
                "sentence_score": None,
                "keyword_score": None,
                "task_state": result["task_state"],
                "finished": result["finished"],
                "first_pass": result["first_pass"],
                "error_count": result["error_count"],
                "remaining": result["progress"]["remaining"],
                "next": "error",
                "error_url": url_for("study.error_page", uid=uid, kind=kind),
                "session_url": _link("study.session", state),
            }
        )

    try:
        task = tm().get_problem(container, kind, uid)
    except Exception:
        return jsonify({"ok": False, "error": "题目不存在或已过期", "counted": False}), 404

    problem = task.get("problem_data") or {}
    sentence = problem.get("sentence") or ""
    reference = problem.get("translation") or ""
    if not sentence:
        return jsonify({"ok": False, "error": "这道题没有句子，请重新生成", "counted": False}), 409

    try:
        scores = svc("ai").score_answer(sentence, answer, task.get("word") or "", reference)
    except InsufficientBalance as exc:
        svc("balance").invalidate()
        return jsonify({"ok": False, "error": f"余额不足，任务已暂停：{exc}", "counted": False, "balance": True}), 402
    except (AIResponseError, AIError) as exc:
        return jsonify({"ok": False, "error": f"评分失败，未计入错误：{exc}", "counted": False}), 503

    passed = grading.judge(
        scores["sentence_score"], scores["keyword_score"], cfg().app.get("pass_rule")
    )
    result = tm().submit(
        container,
        ctype,
        kind,
        uid,
        passed=passed,
        answer=answer,
        sentence_score=scores["sentence_score"],
        keyword_score=scores["keyword_score"],
    )

    # 对错都要先看一眼反馈页：答对看释义与标准译文，答错看答案
    nxt = "correct" if passed else "error"

    return jsonify(
        {
            "ok": True,
            "passed": passed,
            "sentence_score": scores["sentence_score"],
            "keyword_score": scores["keyword_score"],
            "task_state": result["task_state"],
            "finished": result["finished"],
            "first_pass": result["first_pass"],
            "error_count": result["error_count"],
            "remaining": result["progress"]["remaining"],
            "next": nxt,
            "error_url": url_for("study.error_page", uid=uid, kind=kind),
            "correct_url": url_for("study.correct_page", uid=uid, kind=kind),
            "session_url": _link("study.session", state),
            "done_url": _link("study.done", state),
        }
    )


@bp.post("/api/study/skip_answer")
def skip_answer():
    """「我不会」：直接算一次答错，不调 AI。"""
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    if not uid:
        return jsonify({"ok": False, "error": "缺少 uid"}), 400
    result = tm().submit(state["container"], state["ctype"], state["kind"], uid, passed=False, answer="")
    return jsonify(
        {
            "ok": True,
            "task_state": result["task_state"],
            "finished": result["finished"],
            "error_url": url_for("study.error_page", uid=uid, kind=state["kind"]),
            "counted": True,
        }
    )


@bp.get("/study/correct/<uid>")
def correct_page(uid: str):
    """答对之后先看一眼：词义、标准译文、分数、笔记，再进下一题。"""
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return redirect(url_for("home.index"))
    try:
        task = tm().get_problem(state["container"], state["kind"], uid)
    except Exception:
        return redirect(_link("study.session", state))
    word = tm().get_word(state["container"], state["ctype"], uid) or {}
    progress = tm().progress(state["container"], state["kind"])
    next_url = _link("study.done", state) if progress["remaining"] == 0 else _link("study.session", state)
    due_text = clock.humanize_due(word.get("due"), clock.now_ts(svc("config").timezone()), "待学习")
    return render_template(
        "correct.html",
        state=state,
        task=task,
        problem=task.get("problem_data") or {},
        word=word,
        progress=progress,
        next_url=next_url,
        due_text=due_text,
    )


@bp.get("/study/error/<uid>")
def error_page(uid: str):
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return redirect(url_for("home.index"))
    try:
        task = tm().get_problem(state["container"], state["kind"], uid)
    except Exception:
        return redirect(_link("study.session", state))
    word = tm().get_word(state["container"], state["ctype"], uid) or {}
    progress = tm().progress(state["container"], state["kind"])
    return render_template(
        "error.html",
        state=state,
        task=task,
        problem=task.get("problem_data") or {},
        word=word,
        progress=progress,
    )


@bp.post("/api/study/note")
def save_note():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    note = (payload.get("note") or "").strip()
    if not uid:
        return jsonify({"ok": False, "error": "缺少 uid"}), 400
    tm().set_note(state["container"], state["ctype"], uid, note)
    return jsonify({"ok": True})


@bp.get("/study/done")
def done():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return redirect(url_for("home.index"))
    progress = tm().progress(state["container"], state["kind"])
    spelling_enabled = bool(
        (cfg().app.get("spelling") or {}).get("ask_after_review" if state["kind"] == "review" else "ask_after_learning")
    )
    return render_template(
        "done.html",
        state=state,
        progress=progress,
        spelling_enabled=spelling_enabled,
        summary=tm().round_summary(state["container"], state["ctype"], state["kind"]),
    )


@bp.post("/api/spelling/start")
def spelling_start():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return redirect(url_for("home.index"))
    created = tm().create_spelling(state["container"], state["ctype"], state["kind"])
    if created == 0:
        return redirect(_link("study.result", state))
    return redirect(_link("study.spelling", state))


@bp.get("/study/spelling")
def spelling():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return redirect(url_for("home.index"))
    task = tm().next_spelling(state["container"])
    if task is None:
        return redirect(_link("study.result", state))
    progress = tm().spelling_progress(state["container"])
    return render_template("spelling.html", state=state, task=task, progress=progress)


@bp.post("/api/spelling/submit")
def spelling_submit():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    typed = payload.get("typed") or ""
    if not uid:
        return jsonify({"ok": False, "error": "缺少 uid"}), 400
    result = tm().submit_spelling(state["container"], state["ctype"], uid, typed)
    result["session_url"] = _link("study.spelling", state)
    result["result_url"] = _link("study.result", state)
    return jsonify(result)


@bp.post("/api/spelling/abandon")
def spelling_abandon():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return jsonify({"ok": False, "error": "没有正在进行的任务"}), 400
    tm().abandon_spelling(state["container"])
    return jsonify({"ok": True, "result_url": _link("study.result", state)})


@bp.get("/study/result")
def result():
    state = _round_or_redirect(_requested_kind())
    if state is None:
        return redirect(url_for("home.index"))
    summary = tm().round_summary(state["container"], state["ctype"], state["kind"])
    stats = svc("stats")
    container = state["container"]
    info = tm().container_stats(container, state["ctype"]) if tm().container_exists(container, state["ctype"]) else {}
    return render_template(
        "result.html",
        state=state,
        summary=summary,
        info=info,
        totals=stats.totals(container, state["ctype"]) if info else {},
        progress=tm().progress(container, state["kind"]),
    )
