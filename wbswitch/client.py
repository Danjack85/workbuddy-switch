# -*- coding: utf-8 -*-
"""WorkBuddy 客户端进程控制（对齐 zcode-switch 的 kill / launch）。

Windows 用 tasklist / taskkill，macOS/Linux 用 pgrep / pkill。
所有子进程都隐藏窗口，避免弹黑框。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from . import paths
from .i18n import t

SYSTEM = platform.system()

#: 进程名候选（Electron 应用通常主进程即产品名）
PROCESS_NAMES = ("WorkBuddyAI.exe", "WorkBuddy.exe", "workbuddy.exe", "WorkBuddy AI.exe")

#: 沙箱/演练模式：设置该环境变量后不做真实进程操作
_SANDBOX_ENV = "WBSWITCH_SANDBOX"


def in_sandbox() -> bool:
    return bool(os.environ.get(_SANDBOX_ENV))


def _no_window_kwargs() -> dict:
    if SYSTEM == "Windows":
        return {"creationflags": 0x0800_0000}  # CREATE_NO_WINDOW
    return {}


def _detached_kwargs() -> dict:
    if SYSTEM == "Windows":
        return {"creationflags": 0x0000_0008 | 0x0000_0200}  # DETACHED | NEW_PROCESS_GROUP
    return {"start_new_session": True, "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def _run(args: list[str]) -> str:
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **_no_window_kwargs(),
        )
        return p.stdout or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 运行状态
# --------------------------------------------------------------------------


def is_running() -> bool:
    if in_sandbox():
        return False
    if SYSTEM == "Windows":
        out = _run(["tasklist", "/FO", "CSV", "/NH"]).lower()
        return any(f'"{n.lower()}"' in out for n in PROCESS_NAMES)
    for name in ("WorkBuddyAI", "WorkBuddy", "workbuddy"):
        if shutil.which("pgrep") and _run(["pgrep", "-x", name]).strip():
            return True
    return False


def running_pids() -> list[int]:
    if in_sandbox():
        return []
    pids: list[int] = []
    if SYSTEM == "Windows":
        out = _run(["tasklist", "/FO", "CSV", "/NH"])
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0] in PROCESS_NAMES:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    return pids


def running_exe_path() -> str:
    """从正在运行的进程反查可执行文件路径（Windows）。"""
    if in_sandbox() or SYSTEM != "Windows":
        return ""
    for name in PROCESS_NAMES:
        out = _run(["wmic", "process", "where", f"name='{name}'", "get", "ExecutablePath"])
        for line in out.splitlines():
            line = line.strip()
            if line and line.lower().endswith(".exe") and Path(line).exists():
                return line
    return ""


# --------------------------------------------------------------------------
# 结束 / 启动
# --------------------------------------------------------------------------


def kill(timeout: float = 8.0) -> bool:
    """结束 WorkBuddy。返回是否已确认全部退出。"""
    if in_sandbox():
        return True
    if not is_running():
        return True

    if SYSTEM == "Windows":
        for name in PROCESS_NAMES:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", name],
                capture_output=True,
                **_no_window_kwargs(),
            )
    else:
        for name in ("WorkBuddyAI", "WorkBuddy", "workbuddy"):
            subprocess.run(["pkill", "-x", name], capture_output=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            return True
        time.sleep(0.4)

    if SYSTEM != "Windows":
        for name in ("WorkBuddyAI", "WorkBuddy", "workbuddy"):
            subprocess.run(["pkill", "-9", "-x", name], capture_output=True)
        deadline = time.time() + 4
        while time.time() < deadline:
            if not is_running():
                return True
            time.sleep(0.4)

    return not is_running()


def launch(path: str) -> None:
    if in_sandbox():
        return
    p = Path(path)
    if not p.exists():
        raise RuntimeError(t("err.no_client"))
    subprocess.Popen([str(p)], cwd=str(p.parent), **_detached_kwargs())


def launch_ok(path: str) -> bool:
    """启动并返回是否成功（不抛异常，供编排层收集警告）。"""
    try:
        launch(path)
        return True
    except Exception:
        return False


def restart(path: str, wait: float = 1.0) -> bool:
    """结束再启动。返回是否成功启动。"""
    if not kill():
        raise RuntimeError(t("err.kill_timeout"))
    time.sleep(wait)
    if not path:
        return False
    launch(path)
    return True


# --------------------------------------------------------------------------
# 客户端路径解析
# --------------------------------------------------------------------------


def effective_client_path(settings_path: str = "") -> tuple[str, bool]:
    """返回 (路径, 是否可用)。

    优先级：设置里手填且存在 > 正在运行的进程路径 > 常见安装位置。
    """
    if settings_path and Path(settings_path).exists():
        return settings_path, True

    live = running_exe_path()
    if live:
        return live, True

    cand, ok = paths.detect_client_path()
    if ok:
        return cand, True

    return (settings_path or cand), False


def pick_client_path_dialog() -> str:
    """弹出文件选择框（tkinter），失败返回空串。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        pattern = "*.exe" if SYSTEM == "Windows" else "*"
        chosen = filedialog.askopenfilename(
            title="选择 WorkBuddy 客户端",
            filetypes=[("Executable", pattern), ("All files", "*.*")],
        )
        root.destroy()
        return chosen or ""
    except Exception:
        return ""
