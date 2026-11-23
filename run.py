"""本地启动入口。

    python run.py                 # 用 config/app.json 里的端口与访问范围
    python run.py --port 5050     # 临时改端口
    python run.py --allow-lan     # 临时允许局域网访问
    python run.py --browser on    # 强制启动后自动打开浏览器

启动后的动作（后台线程，等服务器真的能应答再执行）：
1. 把访问地址写进剪贴板；
2. 打包成 exe 运行时，自动打开默认浏览器（源码运行时默认不打开，免得每次调试都弹一个）。

同时会在 exe 旁边写 startup.log：从快捷方式启动出问题时，看这个文件就知道断在哪一步。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from app import create_app
from app.config import BASE_DIR, Config
from app.services.clipboard import copy_to_clipboard

LOG_PATH = BASE_DIR / "startup.log"


def log_line(message: str) -> None:
    """往 exe 旁边的 startup.log 追加一行；失败也不影响启动。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def detect_lan_ip() -> str | None:
    """猜一下本机在局域网里的地址，只用于打印提示。"""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except Exception:
        return None


def announce(url: str, open_browser: bool, timeout: float = 20.0, poll: float = 0.2) -> bool:
    """等 /api/ping 通了再写剪贴板、开浏览器。返回是否打开了浏览器。"""
    probe = url.rstrip("/") + "/api/ping"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=1):
                break
        except Exception:
            time.sleep(poll)
    else:
        log_line(f"服务器 {timeout:.0f}s 内没就绪，跳过剪贴板与浏览器：{url}")
        print("（服务器还没就绪，跳过剪贴板与浏览器提示）", flush=True)
        return False

    log_line(f"服务器已就绪：{url}")

    if copy_to_clipboard(url):
        log_line(f"已写入剪贴板：{url}")
        print("（访问地址已复制到剪贴板，直接粘贴到浏览器即可）", flush=True)
    else:
        log_line("剪贴板写入失败")

    if not open_browser:
        return False

    # 优先明确地拉起一个浏览器新窗口：webbrowser.open 依赖系统关联，
    # 有些机器上它返回 True 却什么都不弹（本机就复现了）。
    launched = _launch_known_browser(url)
    if launched:
        log_line(f"已直接启动浏览器：{launched} {url}")
        opened = True
    else:
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception as exc:  # noqa: BLE001
            log_line(f"webbrowser.open 抛异常：{exc!r}")
        if not opened and sys.platform == "win32":
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                opened = True
                log_line("已用 os.startfile 兜底")
            except OSError as exc:
                log_line(f"os.startfile 也失败：{exc!r}")

    log_line(f"打开浏览器：{opened}")
    if not opened:
        print(f"（没能自动打开浏览器，请手动访问 {url}）", flush=True)
    return opened


BROWSER_EXES = ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")

BROWSER_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
)

CHROMIUM = ("msedge.exe", "chrome.exe", "brave.exe", "opera.exe", "vivaldi.exe")


def _default_browser_exe() -> str | None:
    """解析"系统实际会用哪个浏览器打开链接"。

    这里刻意**先读旧式协议关联** ``HKCR\\http\\shell\\open\\command``——
    资源管理器/ShellExecute（也就是 ``webbrowser.open``、双击链接）走的是它。
    很多机器上它和现代的 ``UrlAssociations\\http\\UserChoice`` 并不一致
    （例如前者是 Edge、后者是 Chrome），先读前者才能和"双击链接"的表现一致。
    """
    import winreg

    def _exe_from_command(command: str) -> str | None:
        command = (command or "").strip()
        if not command:
            return None
        if command.startswith('"'):
            exe = command.split('"')[1]
        else:
            exe = command.split(" ")[0]
        return exe if exe and Path(exe).exists() else None

    # 1) 旧式协议关联：ShellExecute 用的就是它
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"http\shell\open\command") as key:
            exe = _exe_from_command(winreg.QueryValueEx(key, "")[0])
        if exe:
            return exe
    except Exception:
        pass

    # 2) 现代默认应用（UserChoice）
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            return _exe_from_command(winreg.QueryValueEx(key, "")[0])
    except Exception:
        pass

    return None


def _launch_known_browser(url: str) -> str | None:
    """明确拉起浏览器并开一个新窗口，返回用了哪个 exe。"""
    candidates: list[str] = []
    default = _default_browser_exe()
    if default:
        candidates.append(default)
    for exe in BROWSER_EXES:
        found = shutil.which(exe)
        if found:
            candidates.append(found)
    candidates.extend(path for path in BROWSER_PATHS if Path(path).exists())

    seen: set[str] = set()
    for path in candidates:
        key = path.lower()
        if key in seen or not Path(path).exists():
            continue
        seen.add(key)
        name = Path(path).name.lower()
        if name in CHROMIUM:
            args = [path, "--new-window", url]
        elif name == "firefox.exe":
            args = [path, "-new-window", url]
        else:
            args = [path, url]
        try:
            subprocess.Popen(args, close_fds=True)
            return path
        except OSError:
            continue
    return None


def main() -> None:
    config = Config()
    server = config.app.get("server") or {}
    default_port = int(server.get("port") or 5000)
    default_lan = bool(server.get("allow_lan"))

    parser = argparse.ArgumentParser(description="启动译词")
    parser.add_argument("--host", default=None, help="绑定地址，默认按配置决定")
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--allow-lan", action="store_true", default=None,
                        help="允许局域网访问（等价于设置里打开开关）")
    parser.add_argument("--browser", choices=("auto", "on", "off"), default="auto",
                        help="启动后是否打开浏览器：auto=打包运行时打开，on=总是，off=从不")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    allow_lan = default_lan if args.allow_lan is None else True
    host = args.host or ("0.0.0.0" if allow_lan else "127.0.0.1")
    open_browser = args.browser == "on" or (args.browser == "auto" and is_frozen())

    local_url = f"http://127.0.0.1:{args.port}/"

    # 启动上下文：从快捷方式启动时 cwd 常常是 System32，这一行能立刻看出问题
    log_line("=" * 60)
    log_line(f"frozen = {is_frozen()}")
    log_line(f"exe    = {sys.executable}")
    log_line(f"cwd    = {os.getcwd()}")
    log_line(f"base   = {BASE_DIR}")
    log_line(f"argv   = {sys.argv}")
    log_line(f"url    = {local_url}")

    app = create_app(allow_lan=True if allow_lan else None)

    print(f"本机访问： {local_url}")
    if allow_lan:
        lan_ip = detect_lan_ip()
        if lan_ip:
            print(f"局域网访问： http://{lan_ip}:{args.port}")
        print("（已允许非本机访问；同一 Wi-Fi 下的设备打开时要输设置里的 6 位令牌）")
    else:
        print("（只允许本机访问；需要手机/别的电脑打开时，到「设置」里打开开关后重启）")

    threading.Thread(target=announce, args=(local_url, open_browser), daemon=True).start()

    app.run(host=host, port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
