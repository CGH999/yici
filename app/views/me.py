"""二级「我的」、三级工具页、四级预习页，以及词本 / 收藏 / 设置。"""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from app.db import schema
from app.services.reset import reset_user_data
from app.services.wordbook_import import ImportError, import_container, parse_payload
from app.views._helpers import cfg, current_container, round_state, svc, tm

bp = Blueprint("me", __name__)

FILTERS = {
    "all": "全部",
    "new": "未学习",
    "review": "复习中",
    "mastered": "复习完成",
    "favorite": "已标熟",
    "due": "今天到期",
}


def _port(value, fallback: int = 5000) -> int:
    """端口校验；表单里没带这个字段时沿用当前值。"""
    if value in (None, ""):
        return fallback
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("端口必须是数字") from None
    if not (1 <= port <= 65535):
        raise ValueError("端口要在 1–65535 之间")
    return port


@bp.get("/me")
def index():
    container, ctype = current_container()
    books = tm().containers("book")
    wordbooks = tm().containers("wordbook")
    info = tm().container_stats(container, ctype) if container else {}
    today = svc("stats").today_counts(container) if container else {"total": 0}
    return render_template(
        "me.html",
        info=info,
        books=books,
        wordbooks=wordbooks,
        filters=FILTERS,
        container=container,
        ctype=ctype,
        today=today,
        wordbook_count=len(wordbooks),
    )


@bp.get("/me/wordbank/<path:name>")
def wordbank(name: str):
    ctype = request.args.get("ctype", "book")
    if ctype not in ("book", "wordbook"):
        abort(404)
    if not tm().container_exists(name, ctype):
        flash(f"找不到容器 {name}", "error")
        return redirect(url_for("me.index"))
    state = request.args.get("state", "all")
    keyword = request.args.get("q") or None
    page = max(1, int(request.args.get("page", 1) or 1))
    limit = 100
    rows = tm().words(name, ctype, state=state, keyword=keyword, limit=limit, offset=(page - 1) * limit)
    info = tm().container_stats(name, ctype)
    return render_template(
        "wordbank.html",
        name=name,
        ctype=ctype,
        rows=rows,
        info=info,
        state=state,
        keyword=keyword or "",
        page=page,
        limit=limit,
        filters=FILTERS,
        is_current=(name == current_container()[0] and ctype == current_container()[1]),
    )


@bp.get("/me/tools")
def tools():
    container, ctype = current_container()
    info = tm().container_stats(container, ctype) if container else {}
    return render_template("tools.html", info=info, container=container)


@bp.get("/me/tools/preview")
def preview():
    container, ctype = current_container()
    if not container:
        flash("还没有词书", "error")
        return redirect(url_for("me.index"))
    data = tm().preview(container, ctype)
    return render_template("preview.html", data=data, state=container)


@bp.post("/api/preview/seen")
def preview_seen():
    container, ctype = current_container()
    payload = request.get_json(silent=True) or {}
    index = int(payload.get("index") or 0)
    uids = payload.get("uids") or []
    if uids:
        tm().mark_previewed(container, ctype, index, uids)
    return jsonify({"ok": True})


@bp.get("/me/wordbooks")
def wordbooks():
    items = []
    for name in tm().containers("wordbook"):
        try:
            items.append(tm().container_stats(name, "wordbook"))
        except Exception:
            continue
    return render_template("wordbooks.html", items=items)


@bp.post("/api/container/create")
def container_create():
    payload = request.get_json(silent=True) or request.form
    name = (payload.get("name") or "").strip()
    ctype = payload.get("ctype") or "wordbook"
    try:
        created = tm().create_container(ctype, name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if request.is_json:
        return jsonify({"ok": True, "name": created})
    flash(f"已新建：{created}", "success")
    return redirect(url_for("me.wordbooks"))


@bp.post("/api/container/import")
def container_import():
    """从粘贴的 JSON 新建词书（前端弹窗提交）。"""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    ctype = payload.get("ctype") or "book"
    text = payload.get("text") or ""

    if not name:
        return jsonify({"ok": False, "error": "先填词书名"}), 400

    try:
        rows = parse_payload(text)
        result = import_container(svc("db"), name, ctype, rows)
    except ImportError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except ValueError as exc:  # schema.validate_name 抛的
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, **result})


@bp.post("/api/container/delete")
def container_delete():
    payload = request.get_json(silent=True) or request.form
    name = payload.get("name") or ""
    ctype = payload.get("ctype") or "book"
    if not name:
        return jsonify({"ok": False, "error": "缺少词书名"}), 400
    if not tm().container_exists(name, ctype):
        return jsonify({"ok": False, "error": "词书不存在"}), 404
    try:
        if ctype == "book" and len(tm().containers("book")) <= 1:
            raise ValueError("至少保留一本词书")
        tm().drop_container(ctype, name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # 删掉的正好是当前词书 / 正在进行的那一轮 → 切到第一本、清掉轮次
    round_state().forget_container(name)
    container, _ = current_container()
    if not container or container == name:
        books = tm().containers("book")
        wordbooks = tm().containers("wordbook")
        if books:
            cfg().set_current_container(books[0], "book")
        elif wordbooks:
            cfg().set_current_container(wordbooks[0], "wordbook")
        else:
            cfg().set_current_container("", "book")

    if request.is_json:
        return jsonify({"ok": True, "name": name})
    flash(f"已删除：{name}", "success")
    return redirect(url_for("me.wordbooks") if ctype == "wordbook" else url_for("dashboard.index"))


@bp.get("/me/favorites")
def favorites():
    # 词书和词本里的收藏都要收进来；同一个词只显示一次（优先词书那条）
    merged: list[dict] = []
    seen: set[str] = set()
    for row in tm().favorites("book") + tm().favorites("wordbook"):
        if row["uid"] in seen:
            continue
        seen.add(row["uid"])
        merged.append(row)
    return render_template("favorites.html", rows=merged, wordbooks=tm().containers("wordbook"))


@bp.post("/api/favorite")
def favorite():
    payload = request.get_json(silent=True) or {}
    uid = payload.get("uid")
    container = payload.get("container") or current_container()[0]
    ctype = payload.get("ctype") or current_container()[1]
    if not uid or not container:
        return jsonify({"ok": False, "error": "缺少参数"}), 400
    try:
        value = tm().toggle_favorite(container, ctype, uid)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "favorite": value})


@bp.post("/api/wordbook/add")
def wordbook_add():
    """把收藏的词复制进某个词本（uid 相同，进度独立）。"""
    payload = request.get_json(silent=True) or {}
    target = payload.get("target")
    uids = payload.get("uids") or []
    if not target or not uids:
        return jsonify({"ok": False, "error": "缺少参数"}), 400
    if target not in tm().containers("wordbook"):
        return jsonify({"ok": False, "error": "词本不存在"}), 400
    added = tm().copy_words_to_container(uids, target)
    return jsonify({"ok": True, "added": added})


@bp.get("/me/wordbooks/<path:name>/edit")
def wordbook_edit(name: str):
    ctype = "wordbook"
    if not tm().container_exists(name, ctype):
        flash(f"找不到词本 {name}", "error")
        return redirect(url_for("me.wordbooks"))
    info = tm().container_stats(name, ctype)
    return render_template("wordbook_edit.html", name=name, info=info, books=tm().containers("book"))


@bp.get("/me/wordbooks/<path:name>/pick")
def wordbook_pick(name: str):
    ctype = "wordbook"
    if not tm().container_exists(name, ctype):
        flash(f"找不到词本 {name}", "error")
        return redirect(url_for("me.wordbooks"))
    books = tm().containers("book")
    selected = request.args.get("book") or (books[0] if books else "")
    return render_template("wordbook_pick.html", name=name, books=books, selected=selected)


@bp.get("/api/wordbank/words")
def wordbank_words():
    """选词界面的数据源：某本词书的词，带"是否已在目标词本里"的标记。"""
    container = (request.args.get("container") or "").strip()
    ctype = request.args.get("ctype") or "book"
    target = (request.args.get("target") or "").strip()
    keyword = request.args.get("q") or None
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except ValueError:
        page = 1
    limit = 50

    if ctype not in ("book", "wordbook"):
        return jsonify({"ok": False, "error": "未知的容器类型"}), 400
    if not container or not tm().container_exists(container, ctype):
        return jsonify({"ok": False, "error": "词书不存在"}), 400

    rows = tm().words(container, ctype, keyword=keyword, limit=limit, offset=(page - 1) * limit)

    target_uids: set[str] = set()
    if target and target in tm().containers("wordbook"):
        db = svc("db").get("wordbooks")
        target_uids = {row["uid"] for row in db.query(f"SELECT uid FROM {schema.quote(target)}")}

    items = [
        {
            "uid": row["uid"],
            "word": row.get("word") or "",
            "translation": row.get("translation") or "",
            "in_target": row["uid"] in target_uids,
        }
        for row in rows
    ]
    return jsonify(
        {
            "ok": True,
            "items": items,
            "page": page,
            "limit": limit,
            "has_more": len(rows) == limit,
            "keyword": keyword or "",
        }
    )


@bp.post("/api/wordbook/rename")
def wordbook_rename():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    new_name = (payload.get("new_name") or "").strip()
    ctype = payload.get("ctype") or "wordbook"
    if ctype not in ("book", "wordbook"):
        return jsonify({"ok": False, "error": "未知的容器类型"}), 400
    if not name or not new_name:
        return jsonify({"ok": False, "error": "名字不能为空"}), 400
    try:
        renamed = tm().rename_container(ctype, name, new_name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "name": renamed})


@bp.post("/api/wordbook/append")
def wordbook_append():
    """往已有词本追加单词（已存在的跳过，不动原有进度）。"""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    ctype = payload.get("ctype") or "wordbook"
    text = payload.get("text") or ""
    if not name:
        return jsonify({"ok": False, "error": "缺少词本名"}), 400
    try:
        rows = parse_payload(text)
        result = import_container(svc("db"), name, ctype, rows, append=True)
    except ImportError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@bp.post("/api/wordbook/remove")
def wordbook_remove():
    """从词本（或词书）里删词，连带清掉任务表里对应的行。"""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    ctype = payload.get("ctype") or "wordbook"
    uids = payload.get("uids") or []
    if ctype not in ("book", "wordbook"):
        return jsonify({"ok": False, "error": "未知的容器类型"}), 400
    if not name:
        return jsonify({"ok": False, "error": "缺少容器名"}), 400
    if not uids:
        return jsonify({"ok": False, "error": "没有选中单词"}), 400
    try:
        removed = tm().remove_words(name, ctype, uids)
    except Exception as exc:  # noqa: BLE001 - 找不到容器等
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "removed": removed})


@bp.get("/me/settings")
def settings():
    api_key = (cfg().ai.get("api_key") or "").strip()
    prompts = cfg().prompts
    return render_template(
        "settings.html",
        app_cfg=cfg().app,
        fsrs=cfg().fsrs,
        ai_cfg={
            key: cfg().ai.get(key)
            for key in ("model", "chat_url", "balance_url", "temperature", "timeout_seconds", "max_history")
        },
        concurrency=cfg().ai.get("concurrency") or {},
        has_key=bool(api_key.startswith("sk-")),
        api_key=api_key,
        server=cfg().app.get("server") or {},
        server_token=(cfg().app.get("server") or {}).get("token", ""),
        prompt_generate=(prompts.get("generate_sentence") or {}).get("system", ""),
        prompt_check=(prompts.get("check_answer") or {}).get("system", ""),
    )


@bp.post("/me/settings")
def settings_save():
    form = request.form

    # --- API Key：输入框里就是当前值，清空即清空 ---
    new_key = (form.get("api_key") or "").strip()
    old_key = (cfg().ai.get("api_key") or "").strip()
    if new_key != old_key:
        if new_key and (not new_key.startswith("sk-") or len(new_key) < 20):
            flash("API Key 格式不对（应该以 sk- 开头，长度 20 以上），未保存", "error")
            return redirect(url_for("me.settings"))
        ai_cfg = cfg().load("ai.json")
        ai_cfg["api_key"] = new_key
        cfg().save("ai.json", ai_cfg)
        svc("balance").invalidate()
        flash("API Key 已更新" if new_key else "API Key 已清空", "success")

    # --- 提示词：留空视为保留原值，避免误删 ---
    prompts = cfg().load("prompts.json")
    empty_prompt = []
    for field, section in (("prompt_generate", "generate_sentence"), ("prompt_check", "check_answer")):
        text = (form.get(field) or "").strip()
        if not text:
            empty_prompt.append(section)
            continue
        prompts.setdefault(section, {})["system"] = text
    if empty_prompt:
        flash("提示词不能为空，已保留原来的内容", "error")
    else:
        cfg().save("prompts.json", prompts)

    current_port = int((cfg().app.get("server") or {}).get("port") or 5000)

    try:
        cfg().update(
            "app.json",
            {
                "learning_per_round": int(form.get("learning_per_round", 20)),
                "review_per_round": int(form.get("review_per_round", 50)),
                "preview_multiplier": int(form.get("preview_multiplier", 2)),
                "proficiency_days": int(form.get("proficiency_days", 365)),
                "sentence_history": max(0, min(20, int(form.get("sentence_history", 5) or 5))),
                "pass_rule": {
                    "mode": form.get("pass_mode", "and"),
                    "sentence_min": float(form.get("sentence_min", 0.8)),
                    "keyword_min": float(form.get("keyword_min", 0.8)),
                    "total_min": float(form.get("total_min", 0.8)),
                },
                "balance": {
                    "warning": float(form.get("balance_warning", 5)),
                    "refresh_seconds": int(form.get("balance_refresh", 300)),
                },
                "server": {
                    "port": _port(form.get("server_port"), current_port),
                    "allow_lan": form.get("server_allow_lan") == "on",
                },
            },
        )
        cfg().update(
            "fsrs.json",
            {
                "initial_difficulty": float(form.get("initial_difficulty", 3.0)),
                "initial_stability": float(form.get("initial_stability", 86400)),
                "alpha": float(form.get("alpha", 0.8)),
                "beta": float(form.get("beta", 0.4)),
                "damping": float(form.get("damping", 0.02)),
                "failure_stability_cap_seconds": float(form.get("failure_cap", 86400)),
                "min_interval_seconds": max(0.0, float(form.get("min_interval_hours", 24) or 24) * 3600),
            },
        )
        cfg().update(
            "ai.json",
            {
                "concurrency": {
                    "enabled": form.get("concurrency_enabled") == "on",
                    "mode": form.get("concurrency_mode", "warmup"),
                    "workers": int(form.get("workers", 4)),
                    "warmup_requests": int(form.get("warmup_requests", 1)),
                }
            },
        )
        flash("设置已保存（端口与访问范围需要重启程序才生效，其余下次请求即生效）", "success")
    except ValueError as exc:
        flash(f"保存失败：{exc}", "error")
    return redirect(url_for("me.settings"))


@bp.post("/api/prompt/restore")
def prompt_restore():
    """恢复出厂提示词。一次可以恢复一条或全部。"""
    payload = request.get_json(silent=True) or {}
    which = payload.get("which") or "all"
    sections = ["generate_sentence", "check_answer"] if which == "all" else [which]
    defaults = cfg().prompt_defaults()
    patch = {section: defaults[section] for section in sections if section in defaults}
    if not patch:
        return jsonify({"ok": False, "error": "找不到出厂提示词（config/prompts.default.json）"}), 400
    prompts = cfg().load("prompts.json")
    for section, value in patch.items():
        prompts[section] = value
    cfg().save("prompts.json", prompts)
    return jsonify({"ok": True, "restored": list(patch)})


@bp.post("/api/server/token")
def regenerate_token():
    """重新生成 6 位访问令牌；已解锁的设备需要重新输入。"""
    from app import new_token

    token = new_token()
    cfg().update("app.json", {"server": {"token": token}})
    return jsonify({"ok": True, "token": token})


@bp.post("/api/reset")
def reset_all():
    """初始化：清空学习数据与笔记收藏，保留词书。"""
    payload = request.get_json(silent=True) or {}
    counts = reset_user_data(svc("db"), cfg(), clear_api_key=bool(payload.get("clear_api_key")))
    round_state().clear()
    return jsonify({"ok": True, **counts})
