# -*- coding: utf-8 -*-
"""路径探测层：定位 WorkBuddy 数据根、账号隔离数据与工具自身的档案库目录。

设计要点（与 zcode-switch 的 Paths::detect 对应）：
- 数据根可通过环境变量 WBSWITCH_WORKBUDDY_HOME 覆盖，便于测试与沙箱；
- 否则在 ~/.workbuddy-ai 与 ~/.workbuddy 之间自动挑选带 workbuddy.db 的那个；
- 工具自身的数据（账号档案、备份、设置）独立放在 ~/.workbuddy-switch，
  绝不与 WorkBuddy 本体数据混放。
"""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path

# --------------------------------------------------------------------------
# 工具自身目录
# --------------------------------------------------------------------------

APP_NAME = "WorkBuddy Switch"
STORE_DIRNAME = ".workbuddy-switch"
#: 扩展数据目录名（登录态等共享数据放在这里，与 .workbuddy-ai 数据根分开）
_EXTENSION_DIRNAME = "CodeBuddyExtension"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    return Path(raw).expanduser()


def home_dir() -> Path:
    """用户主目录（Windows 优先 USERPROFILE，兼容 HOME）。"""
    return _env_path("WBSWITCH_HOME") or _env_path("USERPROFILE") or Path.home()


def store_dir() -> Path:
    """工具档案库根目录。"""
    return _env_path("WBSWITCH_STORE") or (home_dir() / STORE_DIRNAME)


def accounts_dir() -> Path:
    return store_dir() / "accounts"


def backups_dir() -> Path:
    return store_dir() / "backups"


def settings_file() -> Path:
    return store_dir() / "settings.json"


def logs_dir() -> Path:
    return store_dir() / "logs"


def history_file() -> Path:
    """操作历史（切换/同步/回滚流水）。"""
    return store_dir() / "history.jsonl"


# --------------------------------------------------------------------------
# WorkBuddy 数据根
# --------------------------------------------------------------------------

#: 候选目录名，按优先级排列。新版为 .workbuddy-ai，旧版为 .workbuddy。
_DATA_ROOT_CANDIDATES = (".workbuddy-ai", ".workbuddy")


def workbuddy_dir() -> Path:
    """定位 WorkBuddy 数据根。

    优先级：环境变量 > 含 workbuddy.db 的候选目录 > 默认 .workbuddy-ai
    """
    override = _env_path("WBSWITCH_WORKBUDDY_HOME")
    if override:
        return override
    home = home_dir()
    for name in _DATA_ROOT_CANDIDATES:
        cand = home / name
        if (cand / "workbuddy.db").exists():
            return cand
    return home / _DATA_ROOT_CANDIDATES[0]


# 下面这些在导入时求值一次即可；测试里如需切换请重新 import 或调用 refresh()。
_DB_PATH: Path
_MEMORY_DIR: Path
_CONNECTORS_DIR: Path
_STORAGE_DIR: Path
_TASKS_DIR: Path
_PROJECTS_DIR: Path
_SESSIONS_DIR: Path


def _bind() -> None:
    global _DB_PATH, _MEMORY_DIR, _CONNECTORS_DIR, _STORAGE_DIR
    global _TASKS_DIR, _PROJECTS_DIR, _SESSIONS_DIR
    root = workbuddy_dir()
    _DB_PATH = root / "workbuddy.db"
    _MEMORY_DIR = root / "memory"
    _CONNECTORS_DIR = root / "connectors"
    _STORAGE_DIR = root / "storage"
    _TASKS_DIR = root / "tasks"
    _PROJECTS_DIR = root / "projects"
    _SESSIONS_DIR = root / "sessions"


_bind()


def refresh() -> None:
    """在环境变量变化后重新绑定路径。"""
    _bind()


def db_path() -> Path:
    return _DB_PATH


def edge_sync_db_path() -> Path | None:
    """会话的云端同步映射库（`edge-sync-mapping-vN.db`）。

    这是「会话三件套」的第三件：`workbuddy.db` 管本地列表，这个库管
    **云端归属**（`edge_sync_mapping.msg_channel = convmsg:<uid>`）。
    只改本地 user_id 而不动它，云端会认为会话仍属于旧账号。

    版本号随客户端变化（实测 v3，参考实现见过 v2/v4），所以按 glob 取版本号
    最大的那个；找不到返回 None（老版本客户端没有这个库）。
    """
    root = workbuddy_dir()
    if not root.exists():
        return None
    best: tuple[int, Path] | None = None
    for p in root.glob("edge-sync-mapping*.db"):
        if not p.is_file():
            continue
        m = _EDGE_SYNC_RE.search(p.name)
        ver = int(m.group(1)) if m else 0
        if best is None or ver > best[0]:
            best = (ver, p)
    return best[1] if best else None


_EDGE_SYNC_RE = re.compile(r"edge-sync-mapping-v?(\d+)\.db$", re.IGNORECASE)


def session_body_dirs() -> list[Path]:
    """会话正文所在的全部工作区目录（`projects/*/`）。"""
    base = projects_dir()
    if not base.exists():
        return []
    return [d for d in base.iterdir() if d.is_dir()]


def find_session_body(session_id: str) -> Path | None:
    """定位某条会话的正文文件（`projects/{工作区}/{会话id}.jsonl`）。

    正文是**按工作区**存的，不随账号隔离，所以换号不需要移动它；
    但它是会话的实体，缺了会导致「列表里有、点开打不开」。
    """
    if not session_id:
        return None
    for d in session_body_dirs():
        p = d / f"{session_id}.jsonl"
        if p.is_file():
            return p
    return None


def memory_dir() -> Path:
    return _MEMORY_DIR


def connectors_dir() -> Path:
    return _CONNECTORS_DIR


def storage_dir() -> Path:
    return _STORAGE_DIR


def tasks_dir() -> Path:
    return _TASKS_DIR


def projects_dir() -> Path:
    return _PROJECTS_DIR


def sessions_dir() -> Path:
    return _SESSIONS_DIR


def skeleton_dir() -> Path:
    return _STORAGE_DIR / "skeleton"


def account_snapshot_file() -> Path:
    """当前登录身份的权威来源。"""
    return skeleton_dir() / "account-snapshot.json"


def settings_path() -> Path:
    """客户端全局设置文件。

    里面除了全局开关，还有 `claw.users.{uid}` 这种按账号隔离的段落，
    所以同步时必须一并处理，否则换号后消息渠道配置会「失踪」。
    """
    return workbuddy_dir() / "settings.json"


def login_state_dir() -> Path:
    """客户端登录态目录。

    注意这个位置**不在** WorkBuddy 数据根下，而是扩展的数据目录：
      Windows  %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth
      macOS    ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth
      Linux    ~/.local/share/CodeBuddyExtension/Data/Public/auth
    目录里 <authenticationId>.info 就是登录会话（含 accessToken / refreshToken）。
    """
    override = _env_path("WBSWITCH_AUTH_DIR")
    if override:
        return override
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or (home_dir() / "AppData" / "Local"))
    elif system == "Darwin":
        base = home_dir() / "Library" / "Application Support"
    else:
        base = _env_path("XDG_DATA_HOME") or (home_dir() / ".local" / "share")
    return base / _EXTENSION_DIRNAME / "Data" / "Public" / "auth"


def login_state_file() -> Path | None:
    """当前登录态文件；从未登录过则返回 None。

    客户端用「临时文件 + 原子重命名」落盘，残留的中间文件长这样：
      workbuddy-desktop-ai.2026-09-16T12-37-19-240Z.26984.257169c8-....info
    真正生效的那份主干名不带点，据此把中间产物排除掉。
    """
    d = login_state_dir()
    if not d.exists():
        return None
    cands = [p for p in d.glob("*.info") if p.is_file() and "." not in p.stem]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def login_state_files() -> list[Path]:
    """构成登录态的文件集（备份 / 快照用）。"""
    f = login_state_file()
    return [f] if f is not None else []


def logout_marker_path(auth_file: Path) -> Path:
    """登出标记。这个文件存在时，客户端会**忽略** .info 里的会话。"""
    return Path(str(auth_file) + ".logged-out")


def auth_file_name() -> str:
    """客户端实际读取的登录态文件名（`<authenticationId>.info`）。

    客户端用「临时文件 + 原子重命名」落盘，中间产物形如
    `workbuddy-desktop-ai.2026-09-16T12-37-19-240Z.26984.257169c8-....info`。
    真正生效的那份主干名不含点，据此识别；识别不出来时用产品默认 id。
    """
    d = login_state_dir()
    if d.exists():
        for p in sorted(d.glob("*.info")):
            if "." not in p.stem:
                return p.name
    return "workbuddy-desktop-ai.info"


def candidate_login_files() -> dict[str, Path]:
    """扫出目录里所有可用登录态：uid -> 最新的那份文件。

    客户端保留历次落盘的 .info（含带时间戳的中间产物），里面装着明文令牌。
    同一个 uid 有多份时取 expiresAt 最晚的。
    """
    d = login_state_dir()
    if not d.exists():
        return {}
    best: dict[str, tuple[int, Path]] = {}
    for p in sorted(d.glob("*.info")):
        if not p.is_file():
            continue
        data = None
        try:
            import json

            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        acc = data.get("account")
        uid = str((acc or {}).get("uid") or "") if isinstance(acc, dict) else ""
        if not uid:
            continue
        auth = data.get("auth")
        exp = 0
        if isinstance(auth, dict):
            try:
                exp = int(auth.get("expiresAt") or 0)
            except (TypeError, ValueError):
                exp = 0
        prev = best.get(uid)
        if prev is None or exp > prev[0]:
            best[uid] = (exp, p)
    return {uid: p for uid, (_, p) in best.items()}


def user_storage_dir(uid: str) -> Path:
    """账号私有存储目录（storage/user-{uid}-{type}）。

    type 可能是 personal / enterprise / team 等，用前缀匹配查找。
    """
    base = storage_dir()
    if not base.exists() or not uid:
        return base / f"user-{uid}-personal"
    prefix = f"user-{uid}-"
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            return entry
    return base / f"user-{uid}-personal"


def connector_dir(uid: str) -> Path:
    return connectors_dir() / uid


def memory_file(uid: str) -> Path:
    return memory_dir() / f"{uid}_memory.md"


# --------------------------------------------------------------------------
# WorkBuddy 客户端可执行文件
# --------------------------------------------------------------------------

_WIN_EXE_NAMES = ("WorkBuddy.exe", "workbuddy.exe", "WorkBuddy AI.exe")


def _registry_client_candidates() -> list[str]:
    """Windows：从注册表卸载项 / App Paths 里挖出安装路径。

    这比盲猜目录靠谱得多，尤其是用户把客户端装到非默认盘时。
    """
    out: list[str] = []
    if platform.system() != "Windows":
        return out
    try:
        import winreg
    except ImportError:
        return out

    def _read(hive, subkey: str) -> dict:
        vals: dict[str, str] = {}
        try:
            with winreg.OpenKey(hive, subkey) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    if isinstance(value, str):
                        vals[name] = value
                    i += 1
        except OSError:
            pass
        return vals

    # 1) 卸载项
    uninstall_roots = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in uninstall_roots:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as base:
                idx = 0
                while True:
                    try:
                        name = winreg.EnumKey(base, idx)
                    except OSError:
                        break
                    idx += 1
                    vals = _read(winreg.HKEY_LOCAL_MACHINE, f"{root}\\{name}")
                    display = (vals.get("DisplayName") or "").lower()
                    if "workbuddy" not in display and "work buddy" not in display:
                        continue
                    for key in ("DisplayIcon", "InstallLocation", "UninstallString"):
                        raw = vals.get(key) or ""
                        if not raw:
                            continue
                        raw = raw.strip('"').split(",")[0].strip('"')
                        p = Path(raw)
                        if p.suffix.lower() == ".exe":
                            out.append(str(p))
                        elif p.is_dir():
                            for exe in _WIN_EXE_NAMES:
                                out.append(str(p / exe))
        except OSError:
            pass

    # 2) App Paths
    for hive, root in (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    ):
        for exe in _WIN_EXE_NAMES:
            vals = _read(hive, f"{root}\\{exe}")
            if vals.get(""):
                out.append(vals[""])

    return out


def client_path_candidates() -> list[str]:
    """猜测 WorkBuddy 客户端可执行文件位置（注册表 > 常见路径）。"""
    system = platform.system()
    out: list[str] = list(_registry_client_candidates())

    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        bases = [p for p in (local and f"{local}\\Programs", local, pf, pf86) if p]
        for base in bases:
            for name in ("WorkBuddy", "WorkBuddy AI", "WorkBuddyAI", "workbuddy"):
                for exe in _WIN_EXE_NAMES:
                    out.append(f"{base}\\{name}\\{exe}")
    elif system == "Darwin":
        for name in ("WorkBuddy", "WorkBuddy AI"):
            out.append(f"/Applications/{name}.app/Contents/MacOS/{name}")
    else:
        for name in ("workbuddy", "WorkBuddy", "workbuddy-ai"):
            out.append(f"/usr/local/bin/{name}")
            out.append(f"/usr/bin/{name}")
            out.append(f"{home_dir()}/.local/bin/{name}")

    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result


def detect_client_path() -> tuple[str, bool]:
    """返回 (路径, 是否存在)。"""
    for cand in client_path_candidates():
        if Path(cand).exists():
            return cand, True
    cands = client_path_candidates()
    return (cands[0] if cands else "", False)


# --------------------------------------------------------------------------
# 目录初始化
# --------------------------------------------------------------------------


def ensure_store_dirs() -> None:
    for d in (store_dir(), accounts_dir(), backups_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


def diagnostics() -> dict:
    """只读诊断信息，供 GUI/CLI 展示。"""
    root = workbuddy_dir()
    client, client_ok = detect_client_path()
    auth = login_state_file()
    return {
        "workbuddy_home": str(root),
        "workbuddy_home_exists": root.exists(),
        "db_path": str(db_path()),
        "db_exists": db_path().exists(),
        "memory_dir": str(memory_dir()),
        "connectors_dir": str(connectors_dir()),
        "storage_dir": str(storage_dir()),
        "account_snapshot": str(account_snapshot_file()),
        "account_snapshot_exists": account_snapshot_file().exists(),
        "auth_dir": str(login_state_dir()),
        "login_state_file": str(auth) if auth else "",
        "login_state_exists": auth is not None,
        "store_dir": str(store_dir()),
        "client_path": client,
        "client_path_ok": client_ok,
        "platform": platform.system(),
    }
