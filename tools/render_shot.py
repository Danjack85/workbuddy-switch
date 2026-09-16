# -*- coding: utf-8 -*-
"""确定性 GUI 截图：tkinter -> PostScript -> PIL -> PNG。

不依赖屏幕抓取（会被前台窗口遮挡）。tkinter 的 canvas.postscript()
可以离屏把画面导出成 PS，再用 PIL 的 Ghostscript 后端栅格化。

前提：本机装有 Ghostscript（gswin64c）。缺失时给出清晰提示，
并回退到 ImageGrab（可能被遮挡）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs"
OUT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("WBSWITCH_SANDBOX", "1")
os.environ.setdefault("WBSWITCH_STORE", str(ROOT / "docs" / "_shot_store"))

import tkinter as tk  # noqa: E402

from wbswitch import gui, i18n, paths  # noqa: E402

UID_A = "ef537f12-97ce-47ef-aaf2-3712a811ac2b"
UID_B = "c409fef3-e9a7-486d-b17b-26e34825c81f"


FAKE_STATE = {
    "running": False,
    "live_logged_in": True,
    "live_uid": UID_A,
    "unsaved_login": False,
    "workbuddy_home": str(paths.workbuddy_dir()),
    "client_path": r"F:\工具类\ai\WorkBuddyAI\WorkBuddyAI.exe",
    "client_path_ok": True,
    "backup_count": 3,
    "accounts": [
        {
            "id": "f11a2a54-8f4f-4d54-ae26-368512d80d36",
            "name": "当前账号",
            "uid": UID_A,
            "nickname": "buddy@example.com",
            "account_type": "personal",
            "edition_type": "pro",
            "is_pro": True,
            "created_at": "2026-09-16 20:31",
            "updated_at": "2026-09-16 20:33",
            "identity_text": "buddy@example.com · 个人版",
            "stats": {
                "sessions": 7, "memory_bytes": 204, "memory_lines": 3,
                "mcp_servers": 9, "connector_states": 9,
                "automations": 0, "private_files": 9,
            },
            "has_private": True,
            "is_active": True,
        },
        {
            "id": "demo-0002",
            "name": "工作号 (企业版)",
            "uid": UID_B,
            "nickname": "work@example.com",
            "account_type": "personal",
            "edition_type": "pro",
            "is_pro": True,
            "created_at": "2026-09-10 09:12",
            "updated_at": "2026-09-15 18:40",
            "identity_text": "work@example.com · 个人版",
            "stats": {
                "sessions": 23, "memory_bytes": 13312, "memory_lines": 41,
                "mcp_servers": 9, "connector_states": 6,
                "automations": 2, "private_files": 7,
            },
            "has_private": True,
            "is_active": False,
        },
    ],
}

LOGS = [
    ("info", "WorkBuddy Switch v1.0.0 启动"),
    ("info", "WorkBuddy 客户端未运行"),
    ("ok", "已建立档案：当前账号（uid ef537f12）"),
    ("info", "账号库已加载：2 个档案"),
]


# --------------------------------------------------------------------------
# 渲染后端
# --------------------------------------------------------------------------


def find_ghostscript() -> str | None:
    for name in ("gswin64c", "gswin32c", "gs"):
        p = shutil.which(name)
        if p:
            return p
    for base in (
        r"C:\Program Files\gs",
        r"C:\Program Files (x86)\gs",
    ):
        b = Path(base)
        if b.is_dir():
            for cand in sorted(b.glob("gs*/bin/gswin64c.exe"), reverse=True):
                return str(cand)
    return None


def rasterize(ps_file: Path, out_png: Path, scale: float = 2.0) -> bool:
    """把 PostScript 栅格化成 PNG（需要 Ghostscript）。"""
    gs = find_ghostscript()
    if not gs:
        print("[x] 未找到 Ghostscript（gswin64c），无法栅格化")
        return False
    subprocess.run(
        [
            gs,
            "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET",
            "-sDEVICE=png16m",
            f"-r{int(72 * scale)}",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            f"-sOutputFile={out_png}",
            str(ps_file),
        ],
        check=True,
        capture_output=True,
    )
    print(f"[gs] saved {out_png}")
    return True


def render_widget(widget: tk.Misc, out_png: Path, scale: float = 2.0) -> bool:
    """把 widget 的可见内容渲染成 PNG。

    策略一：屏幕抓取（要求窗口在最前，用 topmost + focus 顶上去）
    策略二：若 widget 是 Canvas，用 canvas.postscript() 离屏导出

    tkinter 的 Tk 根窗口没有 postscript()（只有 Canvas 有），
    所以对整窗只能走抓取路线。
    """
    widget.update_idletasks()
    widget.update()

    # ---- 策略二：Canvas 离屏（对 Canvas 组件最干净） ----
    if isinstance(widget, tk.Canvas):
        ps_file = Path(tempfile.gettempdir()) / f"wbshot_{out_png.stem}.ps"
        widget.postscript(
            file=str(ps_file),
            colormode="color",
            x=0, y=0,
            width=widget.winfo_width(),
            height=widget.winfo_height(),
            pagewidth=f"{widget.winfo_width()}p",
            pageheight=f"{widget.winfo_height()}p",
        )
        if rasterize(ps_file, out_png, scale):
            return True

    # ---- 策略一：屏幕抓取 ----
    return grab_screen(widget, out_png)


def grab_screen(widget: tk.Misc, out_png: Path, scale: float = 2.0) -> bool:
    """屏幕抓取：把窗口顶到最前再抓，避免被其它窗口遮挡。"""
    try:
        from PIL import ImageGrab
    except ImportError:
        print("[x] 缺少 pillow，无法抓屏")
        return False

    import time

    # 顶到最前 + 抢焦点，否则会被前台窗口盖住
    try:
        widget.lift()
        widget.attributes("-topmost", True)
        widget.focus_force()
    except Exception:
        pass
    widget.update_idletasks()
    widget.update()
    time.sleep(0.6)  # 给窗口管理器时间完成置顶
    widget.update()

    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    w, h = widget.winfo_width(), widget.winfo_height()
    im = ImageGrab.grab(bbox=(x, y, x + w, y + h))

    # 放大一点，便于阅读
    if scale != 1.0:
        from PIL import Image

        im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
    im.save(out_png)
    try:
        widget.attributes("-topmost", False)
    except Exception:
        pass
    print(f"[grab] saved {out_png}  {im.size}")
    return True


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


def scene_main() -> None:
    root = tk.Tk()
    app = gui.App(root)
    root.update()

    # App.__init__ 里已经发起过一次异步 refresh()，其结果会在 _drain_queue
    # 里回灌并覆盖演示数据。所以这里：
    #   1. 拦掉后续的 _periodic 定时刷新
    #   2. 清空队列里挂着的真实状态
    #   3. 再注入演示数据
    app._periodic = lambda: None
    app.refresh = lambda: None

    import queue as _q

    try:
        while True:
            app._q.get_nowait()
    except _q.Empty:
        pass

    app.state = FAKE_STATE
    app.render()
    for level, msg in LOGS:
        app.log(msg, level)
    app.dot.itemconfigure(app.dot_id, fill=gui.GREEN)
    app.status_text.configure(text=i18n.t("state.safe"))
    root.update()
    root.update_idletasks()

    ok = render_widget(root, OUT / "screenshot.png")
    if not ok:
        grab_screen(root, OUT / "screenshot.png")
    root.destroy()


def scene_diag() -> None:
    root = tk.Tk()
    app = gui.App(root)
    root.update()
    app.state = FAKE_STATE
    app.render()
    root.update()

    d = {
        # 演示用：换成真实的默认档案库路径，不要把项目目录暴露在截图里
        "paths": {
            **paths.diagnostics(),
            "store_dir": str(Path.home() / ".workbuddy-switch"),
        },
        "counts": {UID_A: 7, UID_B: 23},
        "autos": {UID_A: 0, UID_B: 2},
        "uids": [UID_A, UID_B],
        "live": UID_A,
        "integrity": "ok",
        "registered": [UID_A, UID_B],
    }
    app._show_diagnostics(d)
    root.update()

    modal = None
    for w in root.winfo_children():
        if isinstance(w, tk.Toplevel):
            modal = w
            break
    if modal is None:
        print("[x] 没找到诊断窗口")
        root.destroy()
        return
    modal.update()
    ok = render_widget(modal, OUT / "screenshot-diagnostics.png")
    if not ok:
        grab_screen(modal, OUT / "screenshot-diagnostics.png")
    root.destroy()


def scene_switch_dialog() -> None:
    root = tk.Tk()
    app = gui.App(root)
    root.update()
    app.state = FAKE_STATE
    app.render()
    root.update()

    acc = FAKE_STATE["accounts"][1]
    gui.confirm(
        root,
        i18n.t("wiz.switch_title", name=acc["name"]),
        i18n.t("wiz.switch_desc", tag="(自动备份)"),
        i18n.t("btn.switch"),
        lambda: None,
        kind="primary",
    )
    root.update()
    modal = None
    for w in root.winfo_children():
        if isinstance(w, tk.Toplevel):
            modal = w
            break
    if modal is None:
        root.destroy()
        return
    modal.update()
    ok = render_widget(modal, OUT / "screenshot-switch.png")
    if not ok:
        grab_screen(modal, OUT / "screenshot-switch.png")
    root.destroy()


def main() -> None:
    paths.ensure_store_dirs()
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    print("== Ghostscript:", find_ghostscript() or "缺失")
    scene_main()
    scene_diag()
    scene_switch_dialog()


if __name__ == "__main__":
    main()
