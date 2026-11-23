# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

    python -m PyInstaller --noconfirm --clean build.spec
    # 或者 python build_exe.py（= 本 spec + 改名 + 附带 ico/说明）

重点：
* ``icon=`` 用绝对路径并在 spec 里 ``assert``——PyInstaller 找不到图标时**只警告不报错**，
  会静默产出一个没有图标的 exe，这是"快捷方式图标空白"最常见的根因。
* onedir 而不是 onefile：onefile 每次启动都要解包到临时目录，容易被安全软件/权限拦掉。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
ICON = ROOT / "app" / "static" / "icon.ico"
assert ICON.exists(), f"图标不存在：{ICON}"

datas = [
    (str(ROOT / "app" / "templates"), "app/templates"),
    (str(ROOT / "app" / "static"), "app/static"),
    (str(ROOT / "app" / "defaults"), "app/defaults"),
]

hiddenimports = (
    ["dataset"]
    + collect_submodules("alembic")
    + collect_submodules("sqlalchemy.dialects")
)

a = Analysis(
    ["run.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Yici",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Yici",
)
