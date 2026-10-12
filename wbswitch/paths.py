# -*- coding: utf-8 -*-
"""路径探测层：按版本（国内版 / 国际版）定位 WorkBuddy 的数据根、登录态与客户端。

## 为什么必须按版本寻址

一台机器上可能同时装着两个 WorkBuddy：

| | 国内版 cn | 国际版 intl |
| --- | --- | --- |
| 产品 | WorkBuddy（www.workbuddy.cn） | WorkBuddy AI（www.workbuddy.ai） |
| 数据根 | `~/.workbuddy` | `~/.workbuddy-ai` |
| 登录态文件 | `workbuddy-desktop.info` | `workbuddy-desktop-ai.info` |
| 客户端进程 | `WorkBuddy.exe` | `WorkBuddyAI.exe` |

两个版本的登录态文件放在**同一个** auth 目录（扩展数据目录）里。按 mtime
挑最新那份的旧做法在两个版本同时使用时必然挑错，表现为「切国际版账号、
动的却是国内版的登录态」。现在所有路径都带版本：

  · `set_edition()/use_edition()` 绑定当前版本；
  · `workbuddy_dir()/db_path()/login_state_file()/candidate_login_files()`
    默认取当前版本，也可显式传 edition；
  · `detect_client_path(edition)` 按版本的 exe 名从注册表/常见位置探测。

绝不混用两个版本的数据；跨版本操作由上层（sessions/profiles）明确拒绝。

## 环境变量（测试与沙箱）

- `WBSWITCH_WORKBUDDY_HOME`        单根沙箱：覆盖任意版本的数据根
- `WBSWITCH_WORKBUDDY_HOME_CN`     国内版数据根（优先于上者）
- `WBSWITCH_WORKBUDDY_HOME_INTL`   国际版数据根（优先于上者）
- `WBSWITCH_AUTH_DIR`              登录态目录
- `WBSWITCH_STORE`                 工具自身档案库目录（默认 ~/.workbuddy-switch）
"""

from __future__ import annotations

import os
import platform
import re
from contextlib import contextmanager
from pathlib import Path

# --------------------------------------------------------------------------
# 版本（edition）
# --------------------------------------------------------------------------

#: 版本列表（展示顺序固定：国内版在前）
EDITIONS: tuple[str, ...] = ("cn", "intl")

#: 没有版本上下文时的默认版本。工具的历史数据都来自国际版数据根，
#  保持"默认=国际版"，让所有旧调用点在改造期行为不变。
DEFAULT_EDITION = "intl"

_EDITION_SPECS: dict[str, dict] = {
    "cn": {
        "label": "国内版",
        "data_dirname": ".workbuddy",
        "auth_stem": "workbuddy-desktop",
        "exe_names": ("WorkBuddy.exe",),
    },
    "intl": {
        "label": "国际版",
        "data_dirname": ".workbuddy-ai",
        "auth_stem": "workbuddy-desktop-ai",
        "exe_names": ("WorkBuddyAI.exe", "WorkBuddy AI.exe"),
    },
}

_EDITION_ALIASES: dict[str, str] = {
    "cn": "cn", "china": "cn", "mainland": "cn", "domestic": "cn",
    "workbuddy": "cn", "国内版": "cn", "国内": "cn",
    "intl": "intl", "international": "intl", "global": "intl", "ai": "intl",
    "workbuddy-ai": "intl", "workbuddyai": "intl", "en": "intl",
    "国际版": "intl", "国际": "intl",
}


def normalize_edition(value: str | None) -> str | None:
    """把各种写法收敛到 cn / intl；认不出来返回 None。"""
    return _EDITION_ALIASES.get(str(value or "").strip().lower())


def edition_ids() -> tuple[str, ...]:
    return EDITIONS


def edition_label(edition: str | None = None) -> str:
    ed = normalize_edition(edition) or current_edition()
    return str(_EDITION_SPECS[ed]["label"])


def edition_spec(edition: str | None = None) -> dict:
    ed = normalize_edition(edition) or current_edition()
    return _EDITION_SPECS[ed]


_current_edition: str = DEFAULT_EDITION


def current_edition() -> str:
    return _current_edition


def set_edition(edition: str | None) -> str:
    """绑定当前版本并重绑路径；未知/空值保持原状。返回实际生效版本。"""
    global _current_edition
    ed = normalize_edition(edition)
    if ed:
        _current_edition = ed
    _bind()
    return _current_edition


@contextmanager
def use_edition(edition: str | None):
    """临时切到某版本，出块自动还原（嵌套安全）。"""
    prev = _current_edition
    set_edition(edition)
    try:
        yield _current_edition
    finally:
        set_edition(prev)


# --------------------------------------------------------------------------
# 工具自身目录
# --------------------------------------------------------------------------

APP_NAME = "WorkBuddy Switch"
STORE_DIRNAME = ".workbuddy-switch"
#: 扩展数据目录名（登录态等共享数据放在这里，与数据根分开）
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
# WorkBuddy 数据根（按版本）
# --------------------------------------------------------------------------


def _root_for(ed: str) -> Path:
    per = _env_path(f"WBSWITCH_WORKBUDDY_HOME_{ed.upper()}")
    if per:
        return per
    generic = _env_path("WBSWITCH_WORKBUDDY_HOME")
    if generic:
        return generic
    return home_dir() / str(_EDITION_SPECS[ed]["data_dirname"])


def workbuddy_dir(edition: str | None = None) -> Path:
    """定位指定版本（默认当前版本）的数据根。"""
    ed = normalize_edition(edition) or current_edition()
    return _root_for(ed)


def has_data(edition: str | None = None) -> bool:
    """该版本的数据根里有没有客户端数据库（即这个版本是否真正用过）。"""
    return (workbuddy_dir(edition) / "workbuddy.db").exists()


# 下面这些在导入时求值一次；切换版本或环境变量变化后调用 set_edition/refresh。
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
    """在环境变量变化后重新绑定路径（保持当前版本不变）。"""
    _bind()


def db_path() -> Path:
    return _DB_PATH


def edge_sync_db_path() -> Path | None:
    """会话的云端同步映射库（`edge-sync-mapping-vN.db`，当前版本的数据根下）。

    这是「会话三件套」的第三件：`workbuddy.db` 管本地列表，这个库管
    **云端归属**（`edge_sync_mapping.msg_channel = convmsg:<uid>`）。
    只改本地 user_id 而不动它，云端会认为会话仍属于旧账号。

    版本号随客户端变化（实测 v3/v4），所以按 glob 取版本号最大的那个；
    找不到返回 None（老版本客户端没有这个库）。
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
    """会话正文所在的全部工作区目录（`projects/*/`，当前版本）。"""
    base = projects_dir()
    if not base.exists():
        return []
    return [d for d in base.iterdir() if d.is_dir()]


def find_session_body(session_id: str) -> Path | None:
    """定位某条会话的正文文件（`projects/{工作区}/{会话id}.jsonl`，当前版本）。

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
    """当前登录身份的权威来源（当前版本的数据根内）。"""
    return skeleton_dir() / "account-snapshot.json"


def settings_path() -> Path:
    """客户端全局设置文件（当前版本）。

    里面除了全局开关，还有 `claw.users.{uid}` 这种按账号隔离的段落，
    所以同步时必须一并处理，否则换号后消息渠道配置会「失踪」。
    """
    return workbuddy_dir() / "settings.json"


# --------------------------------------------------------------------------
# 登录态（两个版本共用一个 auth 目录，按文件名区分）
# --------------------------------------------------------------------------


def login_state_dir() -> Path:
    """客户端登录态目录（两个版本共用）。

    注意这个位置**不在**数据根下，而是扩展的数据目录：
      Windows  %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth
      macOS    ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth
      Linux    ~/.local/share/CodeBuddyExtension/Data/Public/auth
    目录里 `<authenticationId>.info` 就是登录会话（含 accessToken / refreshToken）。
    国内版的 id 是 `workbuddy-desktop`，国际版是 `workbuddy-desktop-ai` ——
    文件名本身就是版本标签，见 `edition_of_auth_file`。
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


def auth_stem(edition: str | None = None) -> str:
    """该版本登录态文件的主干名（`<stem>.info`）。"""
    ed = normalize_edition(edition) or current_edition()
    return str(_EDITION_SPECS[ed]["auth_stem"])


def edition_of_auth_file(name: str) -> str | None:
    """按文件名判断登录态属于哪个版本；判断不出返回 None。

    国内版 `workbuddy-desktop.info`，国际版 `workbuddy-desktop-ai.info`；
    客户端还会留下 `<stem>.<时间戳>.<pid>.<uuid>.info` 形式的中间产物。
    先匹配更长的主干名（国际版），避免前缀误判。
    """
    n = str(name or "").lower()
    for ed in sorted(EDITIONS, key=lambda e: -len(str(_EDITION_SPECS[e]["auth_stem"]))):
        stem = str(_EDITION_SPECS[ed]["auth_stem"]).lower()
        if n == f"{stem}.info" or n.startswith(f"{stem}."):
            return ed
    return None


def login_state_file(edition: str | None = None) -> Path | None:
    """该版本当前生效的登录态文件；从未登录过则返回 None。

    只认属于该版本的文件：主干名精确命中优先，否则取该版本的
    时间戳中间产物里最新的那份。两个版本的文件在同一目录里，
    绝不跨版本挑（这正是旧实现串号的地方）。
    """
    ed = normalize_edition(edition) or current_edition()
    stem = auth_stem(ed)
    d = login_state_dir()
    if not d.exists():
        return None
    exact = d / f"{stem}.info"
    if exact.is_file():
        return exact
    temps = [p for p in d.glob(f"{stem}.*.info") if p.is_file()]
    if not temps:
        return None
    return max(temps, key=lambda p: p.stat().st_mtime)


def login_state_files(edition: str | None = None) -> list[Path]:
    """该版本构成登录态的文件集（备份 / 快照用）。"""
    f = login_state_file(edition)
    return [f] if f is not None else []


def logout_marker_path(auth_file: Path) -> Path:
    """登出标记。这个文件存在时，客户端会**忽略** .info 里的会话。"""
    return Path(str(auth_file) + ".logged-out")


def auth_file_name(edition: str | None = None) -> str:
    """该版本客户端实际读取的登录态文件名。

    默认 `<stem>.info`；若目录里存在同版本、不同主干名的"干净"文件
    （将来客户端换 id 命名），以磁盘事实为准。
    """
    ed = normalize_edition(edition) or current_edition()
    stem = auth_stem(ed)
    d = login_state_dir()
    if d.exists():
        for p in sorted(d.glob("*.info")):
            if "." in p.stem:
                continue
            if p.stem != stem and edition_of_auth_file(p.name) == ed:
                return p.name
    return f"{stem}.info"


def candidate_login_files(edition: str | None = None) -> dict[str, Path]:
    """扫出该版本可用的登录态：uid -> 最新的那份文件。

    客户端保留历次落盘的 .info（含带时间戳的中间产物），里面装着令牌。
    同一个 uid 有多份时取 expiresAt 最晚的。

    **只认属于该版本的文件**（按主干名前缀区分）——两个版本的 auth 目录
    是同一个，不按前缀过滤就会把另一版本的账号带进来。
    """
    ed = normalize_edition(edition) or current_edition()
    d = login_state_dir()
    if not d.exists():
        return {}
    best: dict[str, tuple[int, Path]] = {}
    for p in sorted(d.glob("*.info")):
        if not p.is_file():
            continue
        if edition_of_auth_file(p.name) != ed:
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
    """账号私有存储目录（storage/user-{uid}-{type}，当前版本）。

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
# WorkBuddy 客户端可执行文件（按版本）
# --------------------------------------------------------------------------

_WIN_EXE_NAMES = ("WorkBuddy.exe", "workbuddy.exe", "WorkBuddy AI.exe", "WorkBuddyAI.exe")


def edition_of_exe(path_or_name: str) -> str | None:
    """按可执行文件名判断它属于哪个版本（判断不出返回 None）。"""
    base = Path(str(path_or_name or "")).name.lower()
    if not base:
        return None
    for ed in EDITIONS:
        for exe in _EDITION_SPECS[ed]["exe_names"]:
            if base == str(exe).lower():
                return ed
    return None


def process_names(edition: str | None = None) -> tuple[str, ...]:
    """该版本的进程映象名（小写，对齐 tasklist 输出口径）。

    两个版本的映象名不同（workbuddy.exe / workbuddyai.exe），
    所以「结束客户端」必须按版本指定，绝不会误杀另一个版本。
    """
    ed = normalize_edition(edition) or current_edition()
    out: list[str] = []
    for exe in _EDITION_SPECS[ed]["exe_names"]:
        n = str(exe).lower()
        if n not in out:
            out.append(n)
    return tuple(out)


def process_stems(edition: str | None = None) -> tuple[str, ...]:
    """进程名去 .exe（macOS/Linux 的 pgrep 口径）。"""
    out: list[str] = []
    for n in process_names(edition):
        stem = n[:-4] if n.endswith(".exe") else n
        if stem not in out:
            out.append(stem)
    return tuple(out)


def _registry_client_candidates() -> list[str]:
    """Windows：从注册表卸载项 / App Paths 里挖出安装路径（两个版本都收）。

    这比盲猜目录靠谱得多，尤其是用户把客户端装到非默认盘时
    （实测国内版装在 `F:\\工具类\\ai\\WB国内\\WorkBuddy`）。
    归属版本由 exe 文件名判定，这里不做筛选。
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


def client_path_candidates(edition: str | None = None) -> list[str]:
    """猜测该版本客户端可执行文件位置（注册表 > 常见路径）。"""
    ed = normalize_edition(edition) or current_edition()
    system = platform.system()
    out: list[str] = []

    for cand in _registry_client_candidates():
        if edition_of_exe(cand) == ed:
            out.append(cand)

    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        bases = [p for p in (local and f"{local}\\Programs", local, pf, pf86) if p]
        for base in bases:
            for name in ("WorkBuddy", "WB国内", "WorkBuddy AI", "WorkBuddyAI", "workbuddy"):
                for exe in _EDITION_SPECS[ed]["exe_names"]:
                    out.append(f"{base}\\{name}\\{exe}")
    elif system == "Darwin":
        names = ("WorkBuddy AI",) if ed == "intl" else ("WorkBuddy",)
        for name in names:
            out.append(f"/Applications/{name}.app/Contents/MacOS/{name}")
    else:
        names = ("workbuddy-ai",) if ed == "intl" else ("workbuddy",)
        for name in names:
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


def detect_client_path(edition: str | None = None) -> tuple[str, bool]:
    """返回该版本的 (路径, 是否存在)。"""
    cands = client_path_candidates(edition)
    for cand in cands:
        if Path(cand).exists():
            return cand, True
    return (cands[0] if cands else "", False)


# --------------------------------------------------------------------------
# 目录初始化
# --------------------------------------------------------------------------


def ensure_store_dirs() -> None:
    for d in (store_dir(), accounts_dir(), backups_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


def diagnostics() -> dict:
    """只读诊断信息（按版本分段），供 GUI/CLI 展示。"""
    out: dict = {
        "store_dir": str(store_dir()),
        "platform": platform.system(),
        "current_edition": current_edition(),
        "editions": {},
    }
    for ed in EDITIONS:
        with use_edition(ed):
            root = workbuddy_dir()
            client, client_ok = detect_client_path(ed)
            auth = login_state_file(ed)
            out["editions"][ed] = {
                "label": edition_label(ed),
                "workbuddy_home": str(root),
                "workbuddy_home_exists": root.exists(),
                "db_path": str(db_path()),
                "db_exists": db_path().exists(),
                "auth_dir": str(login_state_dir()),
                "auth_file_name": auth_file_name(ed),
                "login_state_file": str(auth) if auth else "",
                "login_state_exists": auth is not None,
                "account_snapshot": str(account_snapshot_file()),
                "account_snapshot_exists": account_snapshot_file().exists(),
                "client_path": client,
                "client_path_ok": client_ok,
                "process_names": list(process_names(ed)),
            }
    return out
