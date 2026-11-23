"""仪表盘：统计图表与词书切换。"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from app.services import clock
from app.views._helpers import current_container, svc, tm

bp = Blueprint("dashboard", __name__)


@bp.get("/dashboard")
def index():
    container, ctype = current_container()
    stats = svc("stats")
    daily = stats.daily(30, container) if container else []
    forecast = stats.due_forecast(7, container, ctype) if container else []
    info = tm().container_stats(container, ctype) if container else {}
    books = []
    for name in tm().containers("book"):
        try:
            books.append(tm().container_stats(name, "book"))
        except Exception:
            continue
    wordbooks = []
    for name in tm().containers("wordbook"):
        try:
            wordbooks.append(tm().container_stats(name, "wordbook"))
        except Exception:
            continue
    max_daily = max([day["total"] for day in daily] + [1])
    max_forecast = max([day["count"] for day in forecast] + [1])
    streak = stats.streak(container) if container else 0
    today = stats.today_counts(container) if container else {"total": 0}

    now = datetime.now(clock.tzinfo(svc("config").timezone()))
    monday = now - timedelta(days=now.weekday())
    week = [
        {
            "label": (monday + timedelta(days=offset)).strftime("%a").upper()[:3],
            "day": (monday + timedelta(days=offset)).day,
            "today": (monday + timedelta(days=offset)).date() == now.date(),
        }
        for offset in range(7)
    ]

    return render_template(
        "dashboard.html",
        daily=daily,
        forecast=forecast,
        info=info,
        books=books,
        wordbooks=wordbooks,
        max_daily=max_daily,
        max_forecast=max_forecast,
        streak=streak,
        today=today,
        container=container,
        ctype=ctype,
        week=week,
        has_key=bool((svc("config").ai.get("api_key") or "").startswith("sk-")),
    )


@bp.post("/api/container/switch")
def switch():
    payload = request.get_json(silent=True) or request.form
    name = payload.get("name") or ""
    ctype = payload.get("ctype") or "book"
    if not tm().container_exists(name, ctype):
        if request.is_json:
            return jsonify({"ok": False, "error": "容器不存在"}), 404
        flash("容器不存在", "error")
        return redirect(url_for("dashboard.index"))
    svc("config").set_current_container(name, ctype)
    if request.is_json:
        return jsonify({"ok": True, "url": url_for("home.index")})
    return redirect(url_for("home.index"))


@bp.get("/api/balance")
def balance():
    data = svc("balance").current(force=request.args.get("force") == "1")
    data["warning"] = svc("balance").warning_line()
    return jsonify(data)
