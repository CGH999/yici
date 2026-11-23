"""视图公共工具。"""

from __future__ import annotations

from flask import abort, current_app

from app.services.round_state import Round


def svc(name: str):
    return current_app.extensions[name]


def tm():
    return current_app.extensions["tm"]


def cfg():
    return current_app.extensions["config"]


def round_state() -> Round:
    return current_app.extensions["round"]


def current_container() -> tuple[str, str]:
    return cfg().current_container()


def require_container() -> tuple[str, str]:
    container, ctype = current_container()
    if not container:
        abort(404)
    return container, ctype


def active_round(kind: str | None = None) -> dict:
    """取某一轮的状态；kind 为空时用最近一次的那轮。

    内存里没有这一轮就返回"没有轮次"（kind 为空），由调用方决定是否从任务库恢复。
    注意不能把请求里的 kind 直接塞进返回值——那会伪装成"这一轮正在进行"。
    """
    state = round_state().get(kind)
    if state["container"] and state["kind"]:
        return state
    container, ctype = current_container()
    return {
        "container": container,
        "ctype": ctype,
        "kind": "",
        "generating": False,
        "error": None,
    }
