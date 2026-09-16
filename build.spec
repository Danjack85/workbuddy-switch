# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：WorkBuddy Switch 单文件 exe。

    pyinstaller build.spec --noconfirm

产物：dist/WorkBuddySwitch.exe（单文件，内含 Python 运行时与 tkinter）。

设计说明：
· console=True —— 保证标准输出永远有效，CLI 在终端/管道/重定向下都正常；
  图形界面模式下由 app.py 用 ShowWindow(SW_HIDE) + FreeConsole 把控制台藏掉，
  因此双击时最多闪一下、不会留黑框。比"打 windowed 再 AttachConsole 接控制台"
  可靠得多（后者在部分终端下接不到父控制台，输出会静默丢失）。
· 只收集 tkinter / tcl / tk 的数据文件（这是唯一的非纯 Python 依赖）。
· 显式排除用不到的重量级库，压缩体积。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)

datas = []
# tkinter 需要 tcl/tk 的库文件，否则打包后界面起不来
for pkg in ("tkinter", "_tkinter", "tcl", "tk"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

# 图标（可选，缺失也能构建）
icon = ROOT / "icon.ico"
icon_arg = str(icon) if icon.exists() else None

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "wbswitch",
        "wbswitch.cli",
        "wbswitch.gui",
        "wbswitch.engine",
        "wbswitch.profiles",
        "wbswitch.sessions",
        "wbswitch.switcher",
        "wbswitch.client",
        "wbswitch.config",
        "wbswitch.i18n",
        "wbswitch.paths",
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.font",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除：这些都用不到，去掉能显著减小体积
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL", "PyQt5", "PySide2",
        "PySide6", "PyQt6", "IPython", "pytest", "setuptools", "pip", "wheel",
        "unittest", "pydoc_data", "lib2to3", "sqlite3.test", "test",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WorkBuddySwitch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 常被杀软误报，且省不了多少，不用
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True：标准输出始终有效（CLI 需要）；GUI 模式下 app.py 会隐藏控制台
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)
