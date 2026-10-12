# -*- coding: utf-8 -*-
"""WorkBuddy 客户端进程控制（对齐 zcode-switch 的 kill / launch）。

**按版本操作**：国内版与国际版是两个独立的程序（`WorkBuddy.exe` /
`WorkBuddyAI.exe`），可以同时运行。结束/启动/探测一律要求指明版本
（省略时取当前绑定版本），绝不把另一个版本的进程一起杀掉。

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

#: 进程名并集（兼容旧引用；实际判断按版本走 paths.process_names）。
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


def is_running(edition: str | None = None) -> bool:
    """指定版本的客户端是否在运行（不关心另一个版本）。"""
    if in_sandbox():
        return False
    names = paths.process_names(edition)
    if SYSTEM == "Windows":
        out = _run(["tasklist", "/FO", "CSV", "/NH"]).lower()
        return any(f'"{n}"' in out for n in names)
    for stem in paths.process_stems(edition):
        if shutil.which("pgrep") and _run(["pgrep", "-x", stem]).strip():
            return True
    return False


def running_pids(edition: str | None = None) -> list[int]:
    if in_sandbox():
        return []
    names = paths.process_names(edition)
    pids: list[int] = []
    if SYSTEM == "Windows":
        out = _run(["tasklist", "/FO", "CSV", "/NH"])
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() in names:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
    return pids


def running_exe_path(edition: str | None = None) -> str:
    """从正在运行的进程反查该版本可执行文件路径（Windows）。"""
    if in_sandbox() or SYSTEM != "Windows":
        return ""
    for name in paths.edition_spec(edition)["exe_names"]:
        out = _run(["wmic", "process", "where", f"name='{name}'", "get", "ExecutablePath"])
        for line in out.splitlines():
            line = line.strip()
            if line and line.lower().endswith(".exe") and Path(line).exists():
                return line
    return ""


# --------------------------------------------------------------------------
# 结束 / 启动
# --------------------------------------------------------------------------


def kill(edition: str | None = None, timeout: float = 8.0) -> bool:
    """结束指定版本的 WorkBuddy。返回是否已确认全部退出。"""
    if in_sandbox():
        return True
    if not is_running(edition):
        return True

    names = list(paths.edition_spec(edition)["exe_names"])
    if SYSTEM == "Windows":
        for name in names:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", name],
                capture_output=True,
                **_no_window_kwargs(),
            )
    else:
        for stem in paths.process_stems(edition):
            subprocess.run(["pkill", "-x", stem], capture_output=True)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(edition):
            return True
        time.sleep(0.4)

    if SYSTEM != "Windows":
        for stem in paths.process_stems(edition):
            subprocess.run(["pkill", "-9", "-x", stem], capture_output=True)
        deadline = time.time() + 4
        while time.time() < deadline:
            if not is_running(edition):
                return True
            time.sleep(0.4)

    return not is_running(edition)


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


def restart(path: str, edition: str | None = None, wait: float = 1.0) -> bool:
    """结束该版本再启动。返回是否成功启动。"""
    if not kill(edition):
        raise RuntimeError(t("err.kill_timeout"))
    time.sleep(wait)
    if not path:
        return False
    launch(path)
    return True


# --------------------------------------------------------------------------
# 客户端路径解析
# --------------------------------------------------------------------------


def effective_client_path(settings_path: str = "",
                          edition: str | None = None) -> tuple[str, bool]:
    """返回指定版本的 (路径, 是否可用)。

    优先级：设置里手填且存在 > 该版本正在运行的进程路径 > 注册表/常见位置。
    """
    if settings_path and Path(settings_path).exists():
        return settings_path, True

    live = running_exe_path(edition)
    if live:
        return live, True

    cand, ok = paths.detect_client_path(edition)
    if ok:
        return cand, True

    return (settings_path or cand), False


def pick_client_path_dialog(edition: str | None = None) -> str:
    """弹出文件选择框（tkinter），失败返回空串。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        pattern = "*.exe" if SYSTEM == "Windows" else "*"
        label = paths.edition_label(edition)
        chosen = filedialog.askopenfilename(
            title=f"选择 {label} WorkBuddy 客户端",
            filetypes=[("Executable", pattern), ("All files", "*.*")],
        )
        root.destroy()
        return chosen or ""
    except Exception:
        return ""
