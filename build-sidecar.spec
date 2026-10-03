# -*- mode: python ; coding: utf-8 -*-
"""侧车专用打包配置：给 Tauri 应用做后端引擎用。

与 `build.spec` 的区别（那是独立发行版，自带 tkinter 图形界面）：

| | build.spec（独立版） | build-sidecar.spec（本文件） |
| --- | --- | --- |
| 形态 | 单文件 onefile | **目录 onedir** |
| 界面 | tkinter 图形界面 | **无界面，纯 CLI** |
| 启动耗时 | 约 4.0 秒（每次解包到临时目录） | 约 0.3 秒（直接加载） |
| 体积 | 单文件 15 MB | 目录约 40 MB |

为什么必须是 onedir：侧车是嵌在 Tauri 应用里的后端，用户每次点界面的操作都会
调它一次。onefile 每次运行都要把 Python 运行时和依赖解包到临时目录，实测 4 秒
（杀软还会扫描这些新解包的文件）。onedir 没有这个开销。

为什么排除 tkinter：Tauri 应用本身就是界面，侧车只需要命令行。去掉 tkinter 与
tcl/tk（约 10 MB）既省体积也省启动时间 —— 少加载一个图形栈。
"""

from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "wbswitch",
        "wbswitch.cli",
        "wbswitch.engine",
        "wbswitch.profiles",
        "wbswitch.sessions",
        "wbswitch.switcher",
        "wbswitch.client",
        "wbswitch.config",
        "wbswitch.i18n",
        "wbswitch.paths",
        "wbswitch.upstream",
        "wbswitch.billing",
        "wbswitch.gateway",
        "wbswitch.login",
        "wbswitch.console",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 无界面：整个图形栈都不需要
        "tkinter", "_tkinter", "tcl", "tk", "Tkinter",
        # 用不到的重量级库
        "numpy", "pandas", "matplotlib", "scipy", "PIL", "PyQt5", "PySide2",
        "PySide6", "PyQt6", "IPython", "pytest", "setuptools", "pip", "wheel",
        "unittest", "pydoc_data", "lib2to3", "distutils",
        # gateway 里的 HTTP 服务端只在独立版用；侧车模式下由 Tauri 调起 CLI，
        # 但保留 http.server 以防将来要用（体积很小，不排除）
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,      # onedir：二进制与依赖放到 COLLECT
    name="wbswitch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,               # 侧车需要 stdout/stderr 与父进程通信
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="wbswitch",            # 产物目录名
)
