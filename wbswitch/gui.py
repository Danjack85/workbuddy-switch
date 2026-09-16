# -*- coding: utf-8 -*-
"""WorkBuddy Switch 图形界面（tkinter，零第三方依赖）。

视觉对齐 zcode-switch：深色面板 + 账号行 + 一键切换按钮 + 工具栏 + 托盘式状态。
所有耗时操作（备份 / 同步 / 换号）都在后台线程执行，主线程只负责渲染，
不会出现"界面卡死"。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import __version__, client, config, engine, i18n, paths, profiles, switcher

# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------

BG = "#0a0a0c"
PANEL = "#121216"
PANEL2 = "#17171d"
PANEL3 = "#1e1e26"
BORDER = "#26262e"
BORDER_SOFT = "#1c1c23"
TEXT = "#e8e8ee"
DIM = "#8a8a99"
DIM2 = "#5f5f6d"
ACCENT = "#5b8cff"
ACCENT_DK = "#3f6ae0"
GREEN = "#3ecf8e"
RED = "#ff5f6d"
WARN = "#ffb020"

NOTCH_COLORS = ["#5b8cff", "#3ecf8e", "#ffb020", "#c46bff", "#ff6b9d", "#39c5cf"]

FONT = ("Microsoft YaHei UI", 10) if __import__("platform").system() == "Windows" else ("Helvetica", 11)
FONT_B = (FONT[0], FONT[1], "bold")
FONT_S = (FONT[0], max(8, FONT[1] - 1))
FONT_TITLE = (FONT[0], FONT[1] + 4, "bold")
FONT_MONO = ("Consolas", 9) if FONT[0].startswith("Microsoft") else ("Menlo", 10)


def notch_color(seed: str) -> str:
    h = 0
    for ch in seed or "x":
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return NOTCH_COLORS[h % len(NOTCH_COLORS)]


# --------------------------------------------------------------------------
# 基础控件
# --------------------------------------------------------------------------


class Btn(tk.Label):
    """自绘按钮：tk 原生按钮在深色下太丑，这里用 Label 手搓。"""

    def __init__(self, master, text, command, kind="ghost", width=None, **kw):
        self._command = command
        self._enabled = True
        self._kind = kind
        self._base_bg = {
            "primary": ACCENT,
            "ghost": PANEL3,
            "danger": PANEL3,
            "ok": GREEN,
        }.get(kind, PANEL3)
        self._fg = {
            "primary": "#ffffff",
            "danger": RED,
            "ok": "#06231a",
        }.get(kind, TEXT)

        super().__init__(
            master,
            text=text,
            bg=self._base_bg,
            fg=self._fg,
            font=FONT_S,
            padx=12,
            pady=6,
            cursor="hand2",
            **kw,
        )
        if width:
            self.configure(width=width)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", self._click)

    def _hover(self, on: bool) -> None:
        if not self._enabled:
            return
        if on:
            self.configure(bg=self._lighten(self._base_bg))
        else:
            self.configure(bg=self._base_bg)

    @staticmethod
    def _lighten(hex_color: str) -> str:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        f = 1.28
        r, g, b = (min(255, int(v * f)) for v in (r, g, b))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _click(self, _e) -> None:
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self.configure(
            bg=self._base_bg if on else PANEL2,
            fg=self._fg if on else DIM2,
            cursor="hand2" if on else "arrow",
        )

    def set_text(self, text: str) -> None:
        self.configure(text=text)


class ScrollFrame(tk.Frame):
    """可滚动容器。"""

    def __init__(self, master, **kw):
        super().__init__(master, bg=BG, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)

        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_inner(self, _e) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)

    def _on_wheel(self, e) -> None:
        try:
            first, last = self.canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return
            self.canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()

    def scroll_top(self) -> None:
        self.canvas.yview_moveto(0)


class Modal(tk.Toplevel):
    """深色模态对话框。"""

    def __init__(self, master, title: str, width: int = 520, height: int = 320):
        super().__init__(master)
        self.overrideredirect(False)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (sw - width) // 2
        y = (sh - height) // 3
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.body = tk.Frame(self, bg=BG)
        self.body.pack(fill="both", expand=True, padx=20, pady=18)

        self.bind("<Escape>", lambda e: self.destroy())

    def add_title(self, text: str) -> None:
        tk.Label(self.body, text=text, bg=BG, fg=TEXT, font=FONT_B, anchor="w",
                 justify="left", wraplength=460).pack(fill="x")

    def add_text(self, text: str, color: str = DIM) -> None:
        tk.Label(self.body, text=text, bg=BG, fg=color, font=FONT_S, anchor="w",
                 justify="left", wraplength=460).pack(fill="x", pady=(8, 0))

    def add_actions(self, buttons: list[tuple[str, str, object]]) -> None:
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(14, 0))
        for text, kind, cmd in reversed(buttons):
            Btn(bar, text, cmd, kind=kind).pack(side="right", padx=(8, 0))


def confirm(master, title: str, desc: str, yes_label: str, on_yes, kind: str = "ghost") -> None:
    m = Modal(master, title, 520, 240)
    m.add_title(title)
    if desc:
        m.add_text(desc)
    m.add_actions(
        [
            (yes_label, kind, lambda: (m.destroy(), on_yes())),
            (i18n.t("common.cancel"), "ghost", m.destroy),
        ]
    )


def info(master, title: str, desc: str) -> None:
    m = Modal(master, title, 520, 220)
    m.add_title(title)
    if desc:
        m.add_text(desc)
    m.add_actions([(i18n.t("common.close"), "ghost", m.destroy)])


# --------------------------------------------------------------------------
# 主应用
# --------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = config.load()
        if self.settings.language:
            i18n.set_lang(self.settings.language)

        self.state: dict = {}
        self.busy = False
        self.logs: list[str] = []
        self._q: queue.Queue = queue.Queue()

        root.title(f"{i18n.t('app.name')} — {i18n.t('app.tagline')}")
        root.configure(bg=BG)
        root.geometry("920x780")
        root.minsize(760, 620)
        self._center(root, 920, 780)

        self._build_topbar()
        self._build_toolbar()
        self._build_section_head()
        self._build_list()
        self._build_statusbar()

        self.log(f"{i18n.t('app.name')} v{__version__} 启动")
        self.refresh()
        self.root.after(200, self._drain_queue)
        self.root.after(4000, self._periodic)

    # ------------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------------

    @staticmethod
    def _center(win, w: int, h: int) -> None:
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

    def _build_topbar(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=22, pady=(18, 10))

        left = tk.Frame(bar, bg=BG)
        left.pack(side="left")
        tk.Label(left, text=i18n.t("app.name"), bg=BG, fg=TEXT, font=FONT_TITLE).pack(side="left")
        tk.Label(left, text=f" v{__version__}", bg=BG, fg=DIM2, font=FONT_S).pack(side="left", pady=(8, 0))

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        self.dot = tk.Canvas(right, width=10, height=10, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", pady=(4, 0))
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill=DIM2, outline="")
        self.status_text = tk.Label(right, text="", bg=BG, fg=DIM, font=FONT_S)
        self.status_text.pack(side="left", padx=(7, 0))

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=22, pady=(0, 12))

        self.btn_capture = Btn(bar, i18n.t("btn.capture"), self.do_capture, kind="primary")
        self.btn_capture.pack(side="left")

        self.btn_refresh = Btn(bar, i18n.t("common.refresh"), self.refresh)
        self.btn_refresh.pack(side="left", padx=(8, 0))

        self.btn_sync_all = Btn(bar, i18n.t("btn.sync"), self.do_sync_pick)
        self.btn_sync_all.pack(side="left", padx=(8, 0))

        self.btn_proc = Btn(bar, i18n.t("btn.launch"), self.do_toggle_process)
        self.btn_proc.pack(side="left", padx=(8, 0))

        self.btn_backups = Btn(bar, i18n.t("bk.title"), self.open_backups)
        self.btn_backups.pack(side="left", padx=(8, 0))

        Btn(bar, i18n.t("common.settings"), self.open_settings).pack(side="right")
        self.btn_diag = Btn(bar, "诊断", self.open_diagnostics)
        self.btn_diag.pack(side="right", padx=(0, 8))

    def _build_section_head(self) -> None:
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=22, pady=(0, 6))
        tk.Label(head, text=i18n.t("state.accounts"), bg=BG, fg=TEXT, font=FONT_B).pack(side="left")
        self.count_label = tk.Label(head, text="", bg=BG, fg=DIM2, font=FONT_S)
        self.count_label.pack(side="left", padx=(8, 0))
        self.home_label = tk.Label(head, text="", bg=BG, fg=DIM2, font=FONT_S)
        self.home_label.pack(side="right")

    def _build_list(self) -> None:
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16)
        self.scroll = ScrollFrame(wrap)
        self.scroll.pack(fill="both", expand=True)

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=PANEL, height=92)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        tk.Label(bar, text=i18n.t("log.title"), bg=PANEL, fg=DIM2, font=FONT_S,
                 anchor="w").pack(fill="x", padx=14, pady=(8, 0))

        self.log_box = tk.Text(
            bar, height=4, bg=PANEL, fg=DIM, font=FONT_MONO, bd=0,
            highlightthickness=0, wrap="none", state="disabled",
        )
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(2, 8))

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        def work():
            return switcher.build_state()

        self.run_async(work, self._apply_state, silent=True)

    def _apply_state(self, state: dict) -> None:
        self.state = state
        self.render()

    def render(self) -> None:
        s = self.state
        if not s:
            return

        running = s.get("running")
        logged = s.get("live_logged_in")
        unsaved = s.get("unsaved_login")

        if running:
            color, text = WARN, i18n.t("state.running")
        elif logged and unsaved:
            color, text = ACCENT, i18n.t("state.unsaved")
        elif logged:
            color, text = GREEN, i18n.t("state.safe")
        else:
            color, text = DIM2, i18n.t("state.logged_out")
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.status_text.configure(text=text)

        self.count_label.configure(text=f"{len(s.get('accounts', []))}")
        home = s.get("workbuddy_home", "")
        self.home_label.configure(text=home)

        self.btn_capture.set_enabled(bool(logged) and not unsaved)
        self.btn_proc.set_text(
            i18n.t("btn.kill") if running else i18n.t("btn.launch")
        )
        self.btn_proc.set_enabled(True if running else bool(s.get("client_path_ok")))
        self.btn_sync_all.set_enabled(bool(s.get("accounts")) and bool(logged))

        self._render_rows(s)

    def _render_rows(self, s: dict) -> None:
        self.scroll.clear()
        rows = s.get("accounts", [])

        if not rows:
            box = tk.Frame(self.scroll.inner, bg=PANEL, highlightthickness=1,
                           highlightbackground=BORDER_SOFT)
            box.pack(fill="x", padx=6, pady=10, ipady=26)
            tk.Label(box, text="还没有任何账号档案", bg=PANEL, fg=TEXT, font=FONT_B).pack(pady=(0, 4))
            tk.Label(
                box,
                text="点击左上角「保全当前登录」把当前账号存下来，之后就能一键换号了。",
                bg=PANEL, fg=DIM, font=FONT_S,
            ).pack()
            return

        for acc in rows:
            self._render_row(acc)

    def _render_row(self, acc: dict) -> None:
        is_active = bool(acc.get("is_active"))
        border = ACCENT if is_active else BORDER_SOFT

        card = tk.Frame(self.scroll.inner, bg=PANEL, highlightthickness=1,
                        highlightbackground=border)
        card.pack(fill="x", padx=6, pady=5)

        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=12, pady=10)

        # 左侧色条
        notch = tk.Frame(inner, bg=notch_color(acc.get("id", "")), width=4)
        notch.pack(side="left", fill="y", padx=(0, 12))
        notch.pack_propagate(False)

        # 中间信息
        mid = tk.Frame(inner, bg=PANEL)
        mid.pack(side="left", fill="both", expand=True)

        name_row = tk.Frame(mid, bg=PANEL)
        name_row.pack(fill="x", anchor="w")
        tk.Label(name_row, text=acc.get("name", ""), bg=PANEL, fg=TEXT,
                 font=FONT_B).pack(side="left")
        if is_active:
            tk.Label(name_row, text=f" {i18n.t('row.in_use')} ", bg=ACCENT, fg="#ffffff",
                     font=(FONT[0], 8), padx=4).pack(side="left", padx=(8, 0))
        if acc.get("is_pro"):
            tk.Label(name_row, text=" PRO ", bg="#2b3a63", fg="#9db8ff",
                     font=(FONT[0], 8), padx=4).pack(side="left", padx=(6, 0))
        if acc.get("has_login_state"):
            # 有快照 = 切过去不用重新登录，值得在列表里直接看出来
            expired = acc.get("login_expired")
            tk.Label(
                name_row,
                text=f" {i18n.t('row.cred_expired') if expired else i18n.t('row.cred_saved')} ",
                bg="#4a2a1f" if expired else "#1f4a3a",
                fg="#ffb08a" if expired else "#7fe0b0",
                font=(FONT[0], 8), padx=4,
            ).pack(side="left", padx=(6, 0))

        ident = acc.get("identity_text") or ""
        stats = acc.get("stats") or {}
        meta_bits = []
        if ident:
            meta_bits.append(ident)
        # 「会话」优先显示本地留存的条数——那才是切过去后能看到的总量；
        # 还没归档过的账号退回显示当前可见数。
        shown = acc.get("archived_sessions") or stats.get("sessions", 0)
        meta_bits.append(i18n.t("row.sessions", n=shown))
        meta_bits.append(i18n.t("row.memory", v=self._mem_text(stats)))
        meta_bits.append(i18n.t("row.mcp", n=stats.get("mcp_servers", 0)))
        tk.Label(mid, text=" · ".join(meta_bits), bg=PANEL, fg=DIM, font=FONT_S,
                 anchor="w").pack(fill="x", pady=(3, 0))
        tk.Label(mid, text=f"uid {acc.get('uid', '')}", bg=PANEL, fg=DIM2,
                 font=FONT_MONO, anchor="w").pack(fill="x", pady=(2, 0))

        # 右侧动作
        acts = tk.Frame(inner, bg=PANEL)
        acts.pack(side="right")

        if is_active:
            Btn(acts, i18n.t("row.in_use"), None, kind="ok").pack(side="right")
        else:
            Btn(acts, i18n.t("btn.switch"), lambda a=acc: self.do_switch(a),
                kind="primary").pack(side="right")

        Btn(acts, i18n.t("btn.sync"), lambda a=acc: self.do_sync(a)).pack(
            side="right", padx=(0, 8))
        Btn(acts, i18n.t("btn.details"), lambda a=acc: self.open_details(a)).pack(
            side="right", padx=(0, 8))
        Btn(acts, i18n.t("btn.rename"), lambda a=acc: self.do_rename(a)).pack(
            side="right", padx=(0, 8))
        Btn(acts, i18n.t("btn.delete"), lambda a=acc: self.do_delete(a),
            kind="danger").pack(side="right", padx=(0, 8))

    @staticmethod
    def _mem_text(stats: dict) -> str:
        n = stats.get("memory_bytes") or 0
        if not n:
            return "-"
        return f"{n / 1024:.1f}KB"

    # ------------------------------------------------------------------
    # 异步执行
    # ------------------------------------------------------------------

    def run_async(self, fn, on_done=None, silent: bool = False) -> None:
        if self.busy and not silent:
            self.toast(i18n.t("err.busy"), WARN)
            return

        if not silent:
            self.busy = True
            self._set_buttons(False)

        def worker():
            try:
                result = fn()
                self._q.put(("ok", result, on_done, silent))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", e, on_done, silent))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload, on_done, silent = self._q.get_nowait()
                if kind == "ok":
                    if on_done:
                        try:
                            on_done(payload)
                        except Exception as e:  # noqa: BLE001
                            self.toast(f"{i18n.t('common.failed')}: {e}", RED)
                else:
                    self.log(f"ERROR {payload}", level="err")
                    self.toast(str(payload), RED)
                if not silent:
                    self.busy = False
                    self._set_buttons(True)
        except queue.Empty:
            pass
        self.root.after(200, self._drain_queue)

    def _set_buttons(self, on: bool) -> None:
        for b in (
            self.btn_capture, self.btn_refresh, self.btn_sync_all,
            self.btn_backups, self.btn_diag,
        ):
            b.set_enabled(on)
        if not on:
            self.btn_proc.set_enabled(False)
        else:
            self.render()

    def _periodic(self) -> None:
        if not self.busy:
            self.refresh()
        self.root.after(6000, self._periodic)

    # ------------------------------------------------------------------
    # 日志 / 提示
    # ------------------------------------------------------------------

    def log(self, msg: str, level: str = "info") -> None:
        import time

        stamp = time.strftime("%H:%M:%S")
        prefix = {"info": "  ", "ok": "OK", "err": "!!", "warn": "~~"}.get(level, "  ")
        line = f"{stamp} {prefix} {msg}"
        self.logs.append(line)
        if len(self.logs) > 400:
            self.logs = self.logs[-400:]
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            pass

    def toast(self, msg: str, color: str = GREEN) -> None:
        self.log(msg, "err" if color == RED else ("warn" if color == WARN else "ok"))
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.configure(bg=PANEL3)
        tk.Label(t, text=msg, bg=PANEL3, fg=color, font=FONT_S, padx=16, pady=10,
                 wraplength=420, justify="left").pack()
        self.root.update_idletasks()
        w, h = t.winfo_reqwidth(), t.winfo_reqheight()
        x = self.root.winfo_x() + self.root.winfo_width() - w - 30
        y = self.root.winfo_y() + self.root.winfo_height() - h - 120
        t.geometry(f"+{max(0, x)}+{max(0, y)}")
        t.after(3200, t.destroy)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def do_capture(self) -> None:
        self.log("保全当前登录…")

        def work():
            return profiles.capture_current()

        self.run_async(work, self._after_capture)

    def _after_capture(self, acc) -> None:
        self.log(i18n.t("res.captured", name=acc.name), "ok")
        self.toast(i18n.t("res.captured", name=acc.name))
        switcher.log_history("capture", {"name": acc.name, "uid": acc.uid})
        self.refresh()

    def do_switch(self, acc: dict) -> None:
        running = self.state.get("running")
        hot = self.settings.hot_switch
        tag = "(自动备份)"

        desc = i18n.t("wiz.switch_desc", tag=tag)
        if running and not hot:
            desc = i18n.t("err.switch_running") + "\n\n" + desc

        # 让用户提前知道这次换号要不要重新登录，而不是切完才发现
        if self.settings.switch_login_state:
            if hot and running:
                desc += "\n\n" + i18n.t("wiz.hot_needs_login")
            elif not acc.get("has_login_state"):
                desc += "\n\n" + i18n.t("wiz.no_snapshot")
            else:
                desc += "\n\n" + i18n.t("wiz.has_snapshot")

        confirm(
            self.root,
            i18n.t("wiz.switch_title", name=acc.get("name", "")),
            desc,
            i18n.t("btn.switch"),
            lambda: self._run_switch(acc),
            kind="primary",
        )

    def _run_switch(self, acc: dict) -> None:
        self.log(f"换号 → {acc.get('name')}")

        def work():
            return switcher.switch_to(acc["id"], force=True)

        self.run_async(work, self._after_switch)

    def _after_switch(self, res) -> None:
        if res.already_active:
            self.log(i18n.t("res.already", name=res.name), "ok")
            self.toast(i18n.t("res.already", name=res.name))
        else:
            self.log(i18n.t("res.switched", name=res.name), "ok")
            rep = res.report or {}
            for step in rep.get("steps", []):
                mark = "ok" if step.get("ok") else "err"
                if step.get("changed"):
                    self.log(f"  {step['name']}: {step['changed']}", mark)
                elif step.get("skipped"):
                    self.log(f"  {step['name']}: skipped ({step.get('detail','')})")
            if res.preserved_as:
                self.log(f"  自动保全原登录 → {res.preserved_as}", "warn")
            if res.preserved_login:
                self.log("  已保全原账号登录态（切回时无需重新登录）", "ok")
            if res.login_state:
                self.log("  " + i18n.t("res.credentials"), "ok")
            if res.sessions_visible:
                self.log("  " + i18n.t("res.sessions_visible", n=res.sessions_visible), "ok")
            if res.sessions_restored:
                self.log("  " + i18n.t("res.sessions_restored", n=res.sessions_restored), "ok")
            if res.sessions_parked:
                self.log("  " + i18n.t("res.sessions_parked", n=res.sessions_parked))
            if res.backup_tag:
                self.log(f"  备份 {res.backup_tag}")
            if res.killed:
                self.log("  已结束 WorkBuddy 进程")
            for w in res.warnings:
                self.log(f"  {w}", "warn")
            bits = " · ".join(res.toast_bits())
            self.toast(i18n.t("res.switched", name=res.name) + (f"  [{bits}]" if bits else ""))
            if not res.launched:
                self.log(i18n.t("res.restart_hint"), "warn")
        self.refresh()

    def do_sync(self, acc: dict) -> None:
        if not self.state.get("live_logged_in"):
            self.toast(i18n.t("state.logged_out"), WARN)
            return
        dst = self.state.get("live_uid", "")[:8]
        confirm(
            self.root,
            i18n.t("wiz.sync_title", src=acc.get("name", "")),
            i18n.t("wiz.sync_desc", src=acc.get("name", ""), dst=dst),
            i18n.t("btn.sync"),
            lambda: self._run_sync(acc),
            kind="primary",
        )

    def _run_sync(self, acc: dict) -> None:
        self.log(f"同步 {acc.get('name')} → 当前账号")

        def work():
            return switcher.sync_account_to_current(acc["id"])

        self.run_async(work, self._after_sync)

    def _after_sync(self, rep: dict) -> None:
        for step in rep.get("steps", []):
            if step.get("changed"):
                self.log(f"  {step['name']}: {step['changed']}", "ok" if step.get("ok") else "err")
            elif step.get("skipped"):
                self.log(f"  {step['name']}: skipped ({step.get('detail','')})")
        if rep.get("backup_tag"):
            self.log(f"  备份 {rep['backup_tag']}")
        for w in rep.get("warnings", []):
            self.log(f"  {w}", "warn")
        self.toast(i18n.t("res.synced", src=rep.get("source_uid", "")[:8],
                          dst=rep.get("target_uid", "")[:8]))
        self.log(i18n.t("res.restart_hint"), "warn")
        self.refresh()

    def do_sync_pick(self) -> None:
        rows = [a for a in self.state.get("accounts", []) if not a.get("is_active")]
        if not rows:
            self.toast("没有可同步的账号（当前账号已在最前）", WARN)
            return
        self._pick_account("选择要同步到当前账号的档案", rows,
                           lambda a: self.do_sync(a))

    def do_rename(self, acc: dict) -> None:
        m = Modal(self.root, i18n.t("btn.rename"), 480, 200)
        m.add_title(i18n.t("btn.rename"))
        var = tk.StringVar(value=acc.get("name", ""))
        entry = tk.Entry(m.body, textvariable=var, bg=PANEL3, fg=TEXT, font=FONT,
                         insertbackground=TEXT, bd=0, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT)
        entry.pack(fill="x", pady=(12, 0), ipady=6)
        entry.focus_set()
        entry.select_range(0, "end")

        def submit():
            name = var.get().strip()
            m.destroy()
            if not name:
                return
            self.run_async(lambda: profiles.rename_account(acc["id"], name),
                           self._after_rename)

        m.add_actions([(i18n.t("common.save"), "primary", submit),
                       (i18n.t("common.cancel"), "ghost", m.destroy)])
        entry.bind("<Return>", lambda e: submit())

    def _after_rename(self, acc) -> None:
        self.log(i18n.t("res.renamed", name=acc.name), "ok")
        self.refresh()

    def do_delete(self, acc: dict) -> None:
        confirm(
            self.root,
            i18n.t("wiz.delete_title", name=acc.get("name", "")),
            i18n.t("wiz.delete_desc"),
            i18n.t("common.delete"),
            lambda: self._run_delete(acc),
            kind="danger",
        )

    def _run_delete(self, acc: dict) -> None:
        def work():
            profiles.delete_account(acc["id"])
            return acc.get("name", "")

        self.run_async(work, lambda name: (
            self.log(i18n.t("res.deleted", name=name), "ok"), self.refresh()))

    def do_toggle_process(self) -> None:
        if self.state.get("running"):
            confirm(self.root, i18n.t("wiz.kill_title"), i18n.t("wiz.kill_desc"),
                    i18n.t("btn.kill"), self._run_kill, kind="danger")
        else:
            self._run_launch()

    def _run_kill(self) -> None:
        def work():
            return client.kill()

        self.run_async(work, lambda ok: (
            self.log("WorkBuddy 已结束" if ok else i18n.t("err.kill_timeout"),
                     "ok" if ok else "err"),
            self.refresh()))

    def _run_launch(self) -> None:
        path = self.state.get("client_path", "")

        def work():
            p, ok = client.effective_client_path(self.settings.client_path)
            if not ok:
                raise RuntimeError(i18n.t("err.no_client"))
            client.launch(p)
            return p

        self.run_async(work, lambda p: (self.log(f"已启动 {p}", "ok"), self.refresh()))

    # ------------------------------------------------------------------
    # 子窗口
    # ------------------------------------------------------------------

    def _pick_account(self, title: str, rows: list[dict], on_pick) -> None:
        m = Modal(self.root, title, 520, 90 + min(400, 46 * len(rows)))
        m.add_title(title)
        body = tk.Frame(m.body, bg=BG)
        body.pack(fill="both", expand=True, pady=(10, 0))
        for a in rows:
            row = tk.Frame(body, bg=PANEL2, highlightthickness=1,
                           highlightbackground=BORDER_SOFT)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=a.get("name", ""), bg=PANEL2, fg=TEXT, font=FONT_B,
                     anchor="w").pack(side="left", padx=10, pady=8)
            tk.Label(row, text=f"{(a.get('uid') or '')[:12]}…", bg=PANEL2, fg=DIM2,
                     font=FONT_MONO).pack(side="left")
            Btn(row, i18n.t("common.ok"), lambda x=a: (m.destroy(), on_pick(x)),
                kind="primary").pack(side="right", padx=8, pady=5)
        m.add_actions([(i18n.t("common.cancel"), "ghost", m.destroy)])

    def open_details(self, acc: dict) -> None:
        uid = acc.get("uid", "")
        mem = paths.memory_file(uid)
        priv = paths.user_storage_dir(uid)
        conn = paths.connector_dir(uid)
        stats = engine.scan_stats(uid)

        lines = [
            f"id          {acc.get('id')}",
            f"name        {acc.get('name')}",
            f"uid         {uid}",
            f"nickname    {acc.get('nickname') or '-'}",
            f"type        {acc.get('account_type')} / {acc.get('edition_type') or '-'}",
            f"pro         {acc.get('is_pro')}",
            f"created     {acc.get('created_at')}",
            f"updated     {acc.get('updated_at')}",
            f"last seen   {acc.get('last_seen_at')}",
            "",
            f"sessions    {stats.sessions}",
            f"memory      {stats.memory_bytes} bytes / {stats.memory_lines} lines",
            f"mcp servers {stats.mcp_servers}",
            f"conn states {stats.connector_states}",
            f"automations {stats.automations}",
            f"private     {stats.private_files} files",
            "",
            f"memory file {mem}",
            f"exists      {mem.exists()}",
            f"connectors  {conn}",
            f"exists      {conn.exists()}",
            f"private dir {priv}",
            f"exists      {priv.exists()}",
            f"snapshot    {acc.private_dir()}",
            f"snap exists {acc.private_dir().exists()}",
        ]
        m = Modal(self.root, i18n.t("btn.details"), 660, 560)
        m.add_title(f"{acc.get('name')}  ·  {uid[:12]}…")
        txt = tk.Text(m.body, bg=PANEL, fg=DIM, font=FONT_MONO, bd=0,
                      highlightthickness=1, highlightbackground=BORDER_SOFT, wrap="none")
        txt.pack(fill="both", expand=True, pady=(10, 0))
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        m.add_actions([(i18n.t("common.close"), "ghost", m.destroy)])

    def open_backups(self) -> None:
        def work():
            return engine.list_backups()

        self.run_async(work, self._show_backups, silent=True)

    def _show_backups(self, items) -> None:
        m = Modal(self.root, i18n.t("bk.title"), 720, 560)
        m.add_title(i18n.t("bk.title"))
        if not items:
            m.add_text(i18n.t("bk.empty"))
            m.add_actions([(i18n.t("common.close"), "ghost", m.destroy)])
            return

        body = tk.Frame(m.body, bg=BG)
        body.pack(fill="both", expand=True, pady=(10, 0))
        sf = ScrollFrame(body)
        sf.pack(fill="both", expand=True)

        for b in items:
            row = tk.Frame(sf.inner, bg=PANEL2, highlightthickness=1,
                           highlightbackground=BORDER_SOFT)
            row.pack(fill="x", pady=3)
            left = tk.Frame(row, bg=PANEL2)
            left.pack(side="left", fill="x", expand=True, padx=10, pady=8)
            tk.Label(left, text=b.tag, bg=PANEL2, fg=TEXT, font=FONT_B,
                     anchor="w").pack(fill="x")
            tk.Label(left, text=f"{b.created_at} · {b.size_text()} · target {(b.target_uid or '-')[:12]}",
                     bg=PANEL2, fg=DIM2, font=FONT_S, anchor="w").pack(fill="x")
            Btn(row, i18n.t("btn.restore"), lambda x=b: self._confirm_rollback(x, m),
                kind="danger").pack(side="right", padx=8, pady=6)
        m.add_actions([(i18n.t("common.close"), "ghost", m.destroy)])

    def _confirm_rollback(self, backup, parent_modal) -> None:
        confirm(
            self.root,
            i18n.t("wiz.rollback_title", tag=backup.tag),
            i18n.t("wiz.rollback_desc"),
            i18n.t("btn.restore"),
            lambda: (parent_modal.destroy(), self._run_rollback(backup.tag)),
            kind="danger",
        )

    def _run_rollback(self, tag: str) -> None:
        def work():
            return switcher.rollback(tag)

        self.run_async(work, lambda done: (
            self.log(i18n.t("res.rolled_back", tag=tag), "ok"),
            self.toast(i18n.t("res.rolled_back", tag=tag)),
            self.log(i18n.t("res.restart_hint"), "warn"),
            self.refresh()))

    def open_settings(self) -> None:
        s = config.load()
        m = Modal(self.root, i18n.t("set.title"), 640, 640)
        m.add_title(i18n.t("set.title"))

        body = tk.Frame(m.body, bg=BG)
        body.pack(fill="both", expand=True, pady=(12, 0))

        # 语言
        lang_row = tk.Frame(body, bg=BG)
        lang_row.pack(fill="x", pady=4)
        tk.Label(lang_row, text=i18n.t("set.language"), bg=BG, fg=TEXT, font=FONT,
                 width=26, anchor="w").pack(side="left")
        lang_var = tk.StringVar(value=s.language or i18n.current())
        ttk.Combobox(lang_row, textvariable=lang_var, values=list(i18n.LANGS),
                     state="readonly", width=10).pack(side="left")

        # 客户端路径
        path_row = tk.Frame(body, bg=BG)
        path_row.pack(fill="x", pady=4)
        tk.Label(path_row, text=i18n.t("set.client_path"), bg=BG, fg=TEXT, font=FONT,
                 width=26, anchor="w").pack(side="left")
        path_var = tk.StringVar(value=s.client_path)
        tk.Entry(path_row, textvariable=path_var, bg=PANEL3, fg=TEXT, font=FONT_S,
                 insertbackground=TEXT, bd=0, highlightthickness=1,
                 highlightbackground=BORDER).pack(side="left", fill="x", expand=True, ipady=4)

        def browse():
            p = client.pick_client_path_dialog()
            if p:
                path_var.set(p)

        Btn(path_row, i18n.t("set.pick"), browse).pack(side="left", padx=(6, 0))

        # 布尔开关
        bool_vars: dict[str, tk.BooleanVar] = {}
        bool_defs = [
            ("hot_switch", "set.hot_switch"),
            ("restart_after_switch", "set.restart_after"),
            ("auto_backup", "set.auto_backup"),
            ("auto_capture", "set.auto_capture"),
            ("merge_memory", "set.merge_memory"),
            ("merge_connectors", "set.merge_connectors"),
            ("merge_automations", "set.sync_tasks"),
            ("merge_settings", "set.merge_settings"),
            ("keep_sessions", "set.keep_sessions"),
            ("switch_login_state", "set.switch_login"),
            ("dry_run", "set.dry_run"),
        ]
        for field, key in bool_defs:
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=i18n.t(key), bg=BG, fg=TEXT, font=FONT, width=34,
                     anchor="w").pack(side="left")
            v = tk.BooleanVar(value=bool(getattr(s, field)))
            bool_vars[field] = v
            tk.Checkbutton(
                row, variable=v, bg=BG, fg=TEXT, activebackground=BG,
                activeforeground=TEXT, selectcolor=PANEL3, bd=0,
                highlightthickness=0,
            ).pack(side="left")

        # 路径信息
        info_box = tk.Frame(body, bg=PANEL, highlightthickness=1,
                            highlightbackground=BORDER_SOFT)
        info_box.pack(fill="x", pady=(14, 0))
        for line in (
            f"WorkBuddy 数据根  {paths.workbuddy_dir()}",
            f"档案库            {paths.store_dir()}",
            f"备份目录          {paths.backups_dir()}",
        ):
            tk.Label(info_box, text=line, bg=PANEL, fg=DIM2, font=FONT_MONO,
                     anchor="w").pack(fill="x", padx=10, pady=2)

        def submit():
            s.client_path = path_var.get().strip()
            for field, v in bool_vars.items():
                setattr(s, field, bool(v.get()))
            new_lang = lang_var.get()
            s.language = new_lang
            s.save()
            i18n.set_lang(new_lang)
            self.settings = s
            m.destroy()
            self.log(i18n.t("set.saved"), "ok")
            self.toast(i18n.t("set.saved"))
            self._rebuild_labels()
            self.refresh()

        m.add_actions([(i18n.t("common.save"), "primary", submit),
                       (i18n.t("common.cancel"), "ghost", m.destroy)])

    def _rebuild_labels(self) -> None:
        """语言切换后刷新静态文案。"""
        self.root.title(f"{i18n.t('app.name')} — {i18n.t('app.tagline')}")
        self.btn_capture.set_text(i18n.t("btn.capture"))
        self.btn_refresh.set_text(i18n.t("common.refresh"))
        self.btn_sync_all.set_text(i18n.t("btn.sync"))
        self.btn_backups.set_text(i18n.t("bk.title"))
        self.btn_diag.set_text("诊断" if i18n.current() == "zh" else "Diagnostics")

    def open_diagnostics(self) -> None:
        def work():
            return {
                "paths": paths.diagnostics(),
                "counts": engine.session_counts(),
                "autos": engine.automation_counts(),
                "uids": engine.discover_uids(),
                "live": profiles.live_uid(),
                "integrity": engine.integrity_check(),
                "registered": [a.uid for a in profiles.list_accounts()],
            }

        self.run_async(work, self._show_diagnostics, silent=True)

    def _show_diagnostics(self, d: dict) -> None:
        lines = ["[路径]"]
        for k, v in d["paths"].items():
            lines.append(f"  {k:<26} {v}")
        lines += [
            "",
            f"[当前登录] {d['live'] or '-'}",
            f"[完整性]   {d['integrity']}",
            "",
            f"{'uid':<40}{'Sessions':>10}{'Autos':>8}   flags",
            "-" * 88,
        ]
        for uid in d["uids"]:
            flags = []
            if uid == d["live"]:
                flags.append("CURRENT")
            flags.append("registered" if uid in d["registered"] else "UNREGISTERED")
            lines.append(
                f"{uid:<40}{d['counts'].get(uid, 0):>10}{d['autos'].get(uid, 0):>8}   {' '.join(flags)}"
            )

        m = Modal(self.root, "诊断", 760, 560)
        m.add_title("WorkBuddy 数据诊断（只读）")
        txt = tk.Text(m.body, bg=PANEL, fg=DIM, font=FONT_MONO, bd=0,
                      highlightthickness=1, highlightbackground=BORDER_SOFT, wrap="none")
        txt.pack(fill="both", expand=True, pady=(10, 0))
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        m.add_actions([(i18n.t("common.close"), "ghost", m.destroy)])


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main() -> None:
    paths.ensure_store_dirs()
    root = tk.Tk()
    try:
        # 高分屏适配
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Vertical.TScrollbar",
            background=PANEL3,
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=DIM2,
            darkcolor=PANEL3,
            lightcolor=PANEL3,
        )
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
