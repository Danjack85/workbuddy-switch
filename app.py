#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyInstaller 打包入口：一个 exe 同时充当图形界面和命令行。

为什么需要这个入口，而不是直接拿 `wbswitch/cli.py` 打包：
  1. 冻结后 `python -m wbswitch.cli` 不再适用，exe 需要一个顶层脚本作为起点；
  2. 双击运行时没有参数，应该直接开图形界面；带参数运行时
     （如 `workbuddy-switch.exe switch --id 主号`）走命令行。
     `cli.main()` 本身就有「无子命令 → 开 GUI」的行为，这里只需转发。

## 一个 exe 兼顾 GUI 与 CLI 的做法

矛盾点：CLI 需要控制台（否则 print 无处可去），GUI 不该弹黑框。

这里采用「打 console 版 + GUI 模式下把控制台窗口藏起来」：
  · 打包用 console=True → 标准输出**永远**有效，CLI 在终端/管道/重定向下都正常；
  · 无参数启动（双击）→ 立刻 ShowWindow(SW_HIDE) 隐藏并 FreeConsole 释放控制台，
    界面干净，只可能闪一下。

比反过来（打 windowed 再 AttachConsole 把控制台接回来）可靠得多 ——
后者在部分终端下接不到父控制台，输出会静默丢失。

用法：
    python app.py                  # 打开图形界面
    python app.py state            # 命令行模式
    python app.py switch --id 主号
"""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import sys


def _hide_console_for_gui() -> None:
    """图形界面模式：隐藏并释放控制台，避免双击时留下黑框。

    必须同时用 ShowWindow 隐藏**并且** FreeConsole 摘掉控制台，否则
    控制台窗口会在进程存活期间一直挂在任务栏上。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
        kernel32.FreeConsole()
    except Exception:
        pass


def main() -> int:
    if getattr(sys, "frozen", False):
        multiprocessing.freeze_support()

    argv = sys.argv[1:]

    # 无参数 = 双击运行 → 纯图形界面，不留下控制台
    if not argv:
        _hide_console_for_gui()

    if not getattr(sys, "frozen", False):
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)

    from wbswitch.cli import main as cli_main

    return int(cli_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
