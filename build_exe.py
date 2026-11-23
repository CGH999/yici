"""打包成 Windows exe（PyInstaller onedir）。

    python build_exe.py

产物：``dist/译词/``（里面 ``译词.exe`` + ``_internal/`` + ``译词.ico``）

只打包程序本体：代码、模板、静态资源、出厂提示词。
**不打包** data/（词库与个人数据）、config/（用户配置）、docs/、tests/、tools/——
这些在第一次运行时由程序自己在 exe 旁边生成，词库需要用户自己放进去。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
APP_NAME = "Yici"          # PyInstaller 内部名，避免中文名踩坑
EXE_NAME = "译词.exe"


def run_pyinstaller() -> int:
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "build.spec"]
    print(" ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


def check_icon_warnings(output_path: Path) -> None:
    """PyInstaller 找不到图标时只警告不失败，这里主动检查一遍。"""
    if not output_path.exists():
        return
    text = output_path.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        lowered = line.lower()
        if "icon" in lowered and ("not found" in lowered or "error" in lowered):
            print(f"⚠ 打包日志里的图标相关警告：{line.strip()}")


def main() -> int:
    if not (ROOT / "app" / "static" / "icon.ico").exists():
        raise SystemExit("缺少 app/static/icon.ico，无法给 exe 嵌入图标")

    for stale in (ROOT / "dist" / APP_NAME, ROOT / "dist" / "译词"):
        if stale.exists():
            shutil.rmtree(stale)

    code = run_pyinstaller()
    if code != 0:
        return code

    built_dir = ROOT / "dist" / APP_NAME
    target_dir = ROOT / "dist" / "译词"
    if not built_dir.exists():
        print("没有找到打包产物目录")
        return 1

    built_dir.rename(target_dir)
    exe_in_dir = target_dir / f"{APP_NAME}.exe"
    if exe_in_dir.exists():
        exe_in_dir.rename(target_dir / EXE_NAME)

    # 单独放一份 ico：快捷方式图标读不到时，可以「属性 → 更改图标」指到这个文件
    shutil.copy2(ROOT / "app" / "static" / "icon.ico", target_dir / "译词.ico")

    (target_dir / "把词库放这里.txt").write_text(
        "把词库文件放到这个文件夹的 data 目录里，例如：\n\n"
        "    data\\library.db\n\n"
        "第一次运行会自动创建 data 和 config 目录。\n"
        "没有词库时程序也能启动，只是首页没有词可学。\n\n"
        "如果桌面快捷方式的图标显示为空白：\n"
        "  1. 确认快捷方式「目标」指向本文件夹里的 译词.exe（不是旧路径）；\n"
        "  2. 运行一次 ie4uinit.exe -show 刷新图标缓存；\n"
        "  3. 还不行就右键快捷方式 → 属性 → 更改图标 → 选同目录的 译词.ico。\n",
        encoding="utf-8",
    )

    print(f"\n完成：{target_dir / EXE_NAME}")
    print("发给别人时把整个「译词」文件夹压成 zip 即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
