# -*- coding: utf-8 -*-
"""账号档案库：把 WorkBuddy 的一个登录身份固化成本地档案。

档案 = 身份快照（account-snapshot 的 primary 段）+ 账号私有存储（storage/user-{uid}-*）
      + 统计信息 + 元数据。

存放位置：~/.workbuddy-switch/accounts/{id}.json
          ~/.workbuddy-switch/accounts/{id}/private/   （私有存储副本）

安全约定（对齐 zcode-switch）：
- id 仅允许 [A-Za-z0-9-]，读写路径不可逃出档案库目录；
- 所有写入走临时文件 + 原子 rename；
- 档案里绝不保存任何 token / 密钥。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .i18n import t

_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Stats:
    """档案在磁盘上的数据规模，用于列表展示与迁移前后对比。"""

    sessions: int = 0
    memory_bytes: int = 0
    memory_lines: int = 0
    mcp_servers: int = 0
    connector_states: int = 0
    automations: int = 0
    private_files: int = 0

    @classmethod
    def from_dict(cls, d: dict | None) -> "Stats":
        d = d or {}
        return cls(
            sessions=int(d.get("sessions") or 0),
            memory_bytes=int(d.get("memory_bytes") or 0),
            memory_lines=int(d.get("memory_lines") or 0),
            mcp_servers=int(d.get("mcp_servers") or 0),
            connector_states=int(d.get("connector_states") or 0),
            automations=int(d.get("automations") or 0),
            private_files=int(d.get("private_files") or 0),
        )

    def memory_text(self) -> str:
        if self.memory_bytes <= 0:
            return "-"
        return f"{self.memory_bytes / 1024:.1f}KB"

    def mcp_text(self) -> str:
        if not self.mcp_servers and not self.connector_states:
            return "-"
        return f"{self.mcp_servers}mcp/{self.connector_states}conn"


@dataclass
class Account:
    """一个 WorkBuddy 账号档案。"""

    id: str
    name: str
    uid: str
    #: 账号所属版本（cn 国内版 / intl 国际版）。决定它的数据根、登录态
    #: 文件与客户端程序 —— 换号时一切按它寻址，绝不混用两个版本。
    edition: str = "intl"
    nickname: str = ""
    account_type: str = "personal"
    edition_type: str = ""
    is_pro: bool = False
    is_admin: bool = False
    oneid_account_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_seen_at: str = ""
    saved_at_ms: int = 0
    stats: Stats = field(default_factory=Stats)
    has_private: bool = False
    has_login_state: bool = False
    notes: str = ""

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stats"] = asdict(self.stats)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            uid=str(d.get("uid") or ""),
            edition=paths.normalize_edition(str(d.get("edition") or "")) or "intl",
            nickname=str(d.get("nickname") or ""),
            account_type=str(d.get("account_type") or "personal"),
            edition_type=str(d.get("edition_type") or ""),
            is_pro=bool(d.get("is_pro")),
            is_admin=bool(d.get("is_admin")),
            oneid_account_id=str(d.get("oneid_account_id") or ""),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            last_seen_at=str(d.get("last_seen_at") or ""),
            saved_at_ms=int(d.get("saved_at_ms") or 0),
            stats=Stats.from_dict(d.get("stats")),
            has_private=bool(d.get("has_private")),
            has_login_state=bool(d.get("has_login_state")),
            notes=str(d.get("notes") or ""),
        )

    # ---- 展示辅助 ----

    def label(self) -> str:
        """行内展示用的身份标签。"""
        return self.nickname or self.name or (self.uid[:8] if self.uid else "?")

    def display_identity(self) -> str:
        bits = []
        if self.nickname and self.nickname != self.name:
            bits.append(self.nickname)
        if self.edition_type:
            bits.append(self.edition_type)
        if self.is_pro:
            bits.append("Pro")
        return " · ".join(bits)

    def private_dir(self) -> Path:
        return paths.accounts_dir() / self.id / "private"

    def session_dir(self) -> Path:
        """登录态（Cookie）快照目录。"""
        return paths.accounts_dir() / self.id / "session"

    def profile_file(self) -> Path:
        return paths.accounts_dir() / f"{self.id}.json"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def valid_id(s: str) -> bool:
    return bool(s) and all(c in _ID_CHARS for c in s)


def atomic_write_json(path: Path, data: Any) -> None:
    """临时文件 + 原子 rename，避免半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_json_safe(path: Path) -> Any | None:
    try:
        return read_json(path)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 登录身份读取
# --------------------------------------------------------------------------


def read_live_identity(edition: str | None = None) -> dict:
    """读取指定版本（默认当前版本）当前登录身份（account-snapshot.json 的 primary 段）。

    这是判断「当前登录的是谁」的权威来源。注意不要用数据库里
    "最新 session 的 user_id" 来推断——旧账号的最后一条会话可能比当前账号更新。
    """
    with paths.use_edition(edition):
        snap = paths.account_snapshot_file()
        if not snap.exists():
            raise RuntimeError(t("err.no_snapshot", path=snap))
        data = read_json_safe(snap)
        if not isinstance(data, dict):
            raise RuntimeError(t("err.bad_snapshot", e="json parse error"))
        primary = data.get("primary")
        if not isinstance(primary, dict) or not primary.get("uid"):
            raise RuntimeError(t("err.bad_snapshot", e="missing primary.uid"))
        return primary


def write_live_identity(primary: dict, edition: str | None = None) -> None:
    """写回指定版本（默认当前版本）的登录身份快照（保留文件里其它字段）。"""
    with paths.use_edition(edition):
        snap = paths.account_snapshot_file()
        data = read_json_safe(snap) or {}
        if not isinstance(data, dict):
            data = {}
        merged = dict(data.get("primary") or {})
        merged.update(primary)
        data["primary"] = merged
        atomic_write_json(snap, data)


def live_identity_or_none(edition: str | None = None) -> dict | None:
    """指定版本当前登录身份；未登录或文件损坏返回 None。"""
    try:
        return read_live_identity(edition)
    except Exception:
        return None


def live_uid(edition: str | None = None) -> str:
    """指定版本当前登录 uid；未登录返回空串。"""
    primary = live_identity_or_none(edition)
    return str((primary or {}).get("uid") or "")


def read_login_session(edition: str | None = None) -> dict | None:
    """直接读指定版本的登录会话文件（含令牌）。

    这是「谁登录了」的一手证据：account-snapshot.json 是从它派生出来的。
    只在本地使用，调用方不得把内容写进日志。
    """
    f = paths.login_state_file(edition)
    if f is None or not f.exists():
        return None
    data = read_json_safe(f)
    return data if isinstance(data, dict) else None


def read_login_uid(edition: str | None = None) -> str:
    """从指定版本的登录会话文件读 uid（拿不到就返回空串）。"""
    sess = read_login_session(edition)
    if not sess:
        return ""
    acc = sess.get("account")
    if isinstance(acc, dict):
        return str(acc.get("uid") or "")
    return ""


def login_uid_mismatch(edition: str | None = None) -> tuple[str, str]:
    """返回 (快照 uid, 会话 uid)；一致或任一为空时视为不冲突。"""
    snap = live_uid(edition)
    sess = read_login_uid(edition)
    return snap, sess


def session_matches(uid: str, edition: str | None = None) -> bool:
    """登录会话文件里的账号是否就是 uid。"""
    sess = read_login_uid(edition)
    return bool(sess) and sess == uid


def session_expiry(acc: Account) -> dict:
    """读出该账号可用凭据的有效期信息，用于换号前判断会不会要重新登录。

    优先看档案里的快照，其次看客户端 auth 目录里的候选文件。
    返回 {expires_at(ms), refresh_expires_at(ms), source, found}。
    注意只读取时间字段，不返回任何令牌。
    """
    out = {"expires_at": 0, "refresh_expires_at": 0, "source": "", "found": False}

    def _read(path: Path, source: str) -> bool:
        data = read_json_safe(path)
        if not isinstance(data, dict):
            return False
        auth = data.get("auth")
        if not isinstance(auth, dict):
            return False
        try:
            out["expires_at"] = int(auth.get("expiresAt") or 0)
            out["refresh_expires_at"] = int(auth.get("refreshExpiresAt") or 0)
        except (TypeError, ValueError):
            return False
        out["source"] = source
        out["found"] = bool(out["expires_at"])
        return out["found"]

    # 档案快照
    snap_dir = acc.session_dir()
    if snap_dir.exists():
        files = [f for f in sorted(snap_dir.glob("*.info")) if f.is_file()]
        main = [f for f in files if "." not in f.stem]
        for f in (main + files):
            if _read(f, "archive"):
                return out

    # 客户端 auth 目录里属于该账号的候选（按账号自己的版本找）
    cand = paths.candidate_login_files(acc.edition).get(acc.uid)
    if cand is not None and _read(cand, "client"):
        return out
    return out


def session_expired(acc: Account, skew_ms: int = 0) -> bool:
    """该账号凭据是否已过期（skew_ms 可提前判定"即将过期"）。"""
    info = session_expiry(acc)
    if not info["found"]:
        return False
    exp = max(int(info["expires_at"]), int(info["refresh_expires_at"]))
    if exp <= 0:
        return False
    now_ms = int(time.time() * 1000)
    return exp - skew_ms <= now_ms


def patch_login_session_account(fields: dict, edition: str | None = None) -> bool:
    """更新登录会话文件里的账号展示字段（只动 account/accounts/allAccounts）。

    凭据级换号把目标账号的 .info 整份复制过来，**令牌来自目标档案、
    账号字段也随之一致**，所以通常不需要再打补丁。这里只在字段缺失
    （例如旧格式快照）时兜底，绝不碰 auth 段里的令牌。
    """
    f = paths.login_state_file(edition)
    if f is None or not f.exists():
        return False
    data = read_json_safe(f)
    if not isinstance(data, dict):
        return False
    # 国内版把展示字段也加密了（$wbEncrypted 信封）：改不动也不该改，
    # 原样保留（令牌和字段都是一体的，由客户端自己维护）。
    acc_node = data.get("account")
    if isinstance(acc_node, dict) and isinstance(acc_node.get("nickname"), dict):
        return False

    changed = False
    for key in ("account", "accounts", "allAccounts"):
        node = data.get(key)
        if isinstance(node, dict):
            for k, v in fields.items():
                if k in node and node.get(k) != v:
                    node[k] = v
                    changed = True
        elif isinstance(node, list):
            for item in node:
                if not isinstance(item, dict):
                    continue
                for k, v in fields.items():
                    if k in item and item.get(k) != v:
                        item[k] = v
                        changed = True
    if not changed:
        return False

    tmp = f.with_name(f.name + ".wbswitch-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    _harden(f)
    return True


# --------------------------------------------------------------------------
# 档案库 CRUD
# --------------------------------------------------------------------------


def list_accounts() -> list[Account]:
    d = paths.accounts_dir()
    if not d.exists():
        return []
    out: list[Account] = []
    for entry in sorted(d.glob("*.json")):
        raw = read_json_safe(entry)
        if isinstance(raw, dict):
            acc = Account.from_dict(raw)
            if acc.id and acc.uid:
                out.append(acc)
    out.sort(key=lambda a: (a.created_at or "", a.name))
    return out


def find_by_id(account_id: str) -> Account | None:
    for a in list_accounts():
        if a.id == account_id:
            return a
    return None


def find_by_uid(uid: str) -> Account | None:
    if not uid:
        return None
    for a in list_accounts():
        if a.uid == uid:
            return a
    return None


def load_account(account_id: str) -> Account:
    if not valid_id(account_id):
        raise RuntimeError(t("err.no_account", id=account_id))
    path = paths.accounts_dir() / f"{account_id}.json"
    raw = read_json_safe(path)
    if not isinstance(raw, dict):
        raise RuntimeError(t("err.no_account", id=account_id))
    return Account.from_dict(raw)


def save_account(acc: Account) -> None:
    paths.ensure_store_dirs()
    atomic_write_json(acc.profile_file(), acc.to_dict())


def unique_name(base: str, exclude_id: str | None = None) -> str:
    """生成不重名的档案名：base / base 2 / base 3 …"""
    base = (base or "").strip() or "Account"
    taken = {a.name.lower() for a in list_accounts() if a.id != exclude_id}
    if base.lower() not in taken:
        return base
    for n in range(2, 1000):
        cand = f"{base} {n}"
        if cand.lower() not in taken:
            return cand
    return f"{base} {uuid.uuid4().hex[:4]}"


def rename_account(account_id: str, new_name: str) -> Account:
    name = (new_name or "").strip()
    if not name:
        raise RuntimeError(t("err.name_empty"))
    if len(name) > 40:
        name = name[:40]
    acc = load_account(account_id)
    for other in list_accounts():
        if other.id != account_id and other.name.lower() == name.lower():
            raise RuntimeError(t("err.name_taken", name=name))
    acc.name = name
    acc.updated_at = now_ts()
    save_account(acc)
    return acc


def delete_account(account_id: str) -> None:
    """删除档案（含私有存储副本）。绝不触碰 WorkBuddy 本体数据。"""
    acc = load_account(account_id)
    acc.profile_file().unlink(missing_ok=True)
    parent = paths.accounts_dir() / acc.id
    if parent.exists():
        shutil.rmtree(parent, ignore_errors=True)


# --------------------------------------------------------------------------
# 档案创建
# --------------------------------------------------------------------------


def snapshot_private(acc: Account) -> int:
    """把账号私有存储复制进档案目录，返回复制文件数。"""
    src = paths.user_storage_dir(acc.uid)
    dst = acc.private_dir()
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    if not src.exists():
        acc.has_private = False
        return 0
    shutil.copytree(src, dst)
    count = sum(1 for p in dst.rglob("*") if p.is_file())
    acc.has_private = count > 0
    return count


def holds_live_session(uid: str, edition: str | None = None) -> bool:
    """该 uid 是否就是（指定版本）登录会话文件里那个账号。

    只有它才持有可用的令牌，别给其它 uid 硬拍一份空/错的登录态。
    会话文件缺失时退回身份快照判断。
    """
    if not uid:
        return False
    sess_uid = read_login_uid(edition)
    if sess_uid:
        return sess_uid == uid
    return live_uid(edition) == uid


def capture_from_identity(primary: dict, name: str | None = None,
                          edition: str | None = None) -> Account:
    """按给定身份建档案（已存在同 uid 则报错，由调用方决定是否走 update）。

    edition 决定该档案属于哪个版本（默认当前绑定版本）；建档时的统计
    与私有存储快照都在该版本的数据根上进行。
    """
    from . import engine  # 延迟导入避免循环

    uid = str(primary.get("uid") or "")
    if not uid:
        raise RuntimeError(t("err.bad_snapshot", e="missing primary.uid"))

    ed = paths.normalize_edition(edition) or paths.current_edition()

    existing = find_by_uid(uid)
    if existing is not None:
        raise RuntimeError(t("err.dup_login", name=existing.name))

    ts = now_ts()
    acc = Account(
        id=str(uuid.uuid4()),
        name=unique_name(name or str(primary.get("nickname") or uid[:8])),
        uid=uid,
        edition=ed,
        nickname=str(primary.get("nickname") or ""),
        account_type=str(primary.get("type") or "personal"),
        edition_type=str(primary.get("editionType") or ""),
        is_pro=bool(primary.get("isPro")),
        is_admin=bool(primary.get("isAdmin")),
        oneid_account_id=str(primary.get("oneidAccountId") or ""),
        created_at=ts,
        updated_at=ts,
        last_seen_at=ts,
        saved_at_ms=int(primary.get("savedAt") or 0),
    )
    with paths.use_edition(ed):
        acc.stats = engine.scan_stats(uid)
        acc.stats.private_files = snapshot_private(acc)
        if holds_live_session(uid, ed):
            acc.has_login_state = snapshot_login_state(acc) > 0
    save_account(acc)
    return acc


def capture_current(name: str | None = None, edition: str | None = None) -> Account:
    """保全指定版本（默认当前版本）的当前登录身份。

    身份快照缺失时（新客户端可能还没写过）从登录态文件兜底拼一份 ——
    国内版把昵称也加密了，拿不到就用 uid 前缀当名字。
    """
    ed = paths.normalize_edition(edition) or paths.current_edition()
    try:
        primary = read_live_identity(ed)
    except Exception:
        sess = read_login_session(ed)
        acc_info = (sess or {}).get("account") or {}
        uid = str(acc_info.get("uid") or "")
        if not uid:
            raise
        nick = acc_info.get("nickname")
        primary = {
            "uid": uid,
            "nickname": nick if isinstance(nick, str) else "",
            "type": acc_info.get("type") or "personal",
        }
    return capture_from_identity(primary, name, ed)


def refresh_from_live(account_id: str) -> Account:
    """用当前登录态刷新指定档案（要求 uid 一致，且在该档案的版本上进行）。"""
    from . import engine

    acc = load_account(account_id)
    with paths.use_edition(acc.edition):
        primary = read_live_identity()
        if str(primary.get("uid") or "") != acc.uid:
            raise RuntimeError(t("err.same"))
        acc.nickname = str(primary.get("nickname") or acc.nickname)
        acc.edition_type = str(primary.get("editionType") or acc.edition_type)
        acc.is_pro = bool(primary.get("isPro"))
        acc.is_admin = bool(primary.get("isAdmin"))
        acc.saved_at_ms = int(primary.get("savedAt") or acc.saved_at_ms)
        acc.updated_at = now_ts()
        acc.last_seen_at = now_ts()
        acc.stats = engine.scan_stats(acc.uid)
        acc.stats.private_files = snapshot_private(acc)
        if holds_live_session(acc.uid):
            acc.has_login_state = snapshot_login_state(acc) > 0
    save_account(acc)
    return acc


def touch_last_seen(account_id: str) -> None:
    acc = find_by_id(account_id)
    if acc is None:
        return
    acc.last_seen_at = now_ts()
    save_account(acc)


def auto_preserve_current(edition: str | None = None) -> str | None:
    """切换前自动保全：指定版本当前登录若不在档案库，就自动入库。

    返回新建档案的名称，未新建则返回 None。绝不丢号。
    """
    ed = paths.normalize_edition(edition) or paths.current_edition()
    uid = live_uid(ed)
    if not uid:
        return None
    if find_by_uid(uid) is not None:
        return None
    primary = read_live_identity(ed)
    auto_name = f"Auto {time.strftime('%m-%d %H%M')}"
    acc = capture_from_identity(primary, auto_name, ed)
    return acc.name


def adopt_accounts_from_auth(editions: list[str] | None = None) -> list[str]:
    """给指定版本（默认全部版本）auth 目录里出现过、但尚未建档的账号补建档案。

    客户端会把历次登录的会话留在这个目录里（含昵称等身份信息），
    据此建档后，这些账号就能直接一键切换，不必先手动登录一次。

    返回新建档案的名称列表。
    """
    created: list[str] = []
    target_eds = editions or list(paths.edition_ids())
    for ed in target_eds:
        for uid, path in paths.candidate_login_files(ed).items():
            if find_by_uid(uid) is not None:
                continue
            data = read_json_safe(path)
            if not isinstance(data, dict):
                continue
            acc_info = data.get("account")
            if not isinstance(acc_info, dict):
                continue
            nick = acc_info.get("nickname")
            primary = {
                "uid": uid,
                "nickname": nick if isinstance(nick, str) else "",
                "type": acc_info.get("type") or "personal",
                "editionType": acc_info.get("editionType") or "",
                "isPro": bool(acc_info.get("isPro")),
                "isAdmin": bool(acc_info.get("isAdmin")),
                "oneidAccountId": acc_info.get("oneidAccountId") or "",
            }
            try:
                acc = capture_from_identity(
                    primary, (primary["nickname"] or uid[:8]), ed)
            except Exception:
                continue
            created.append(acc.name)
    return created


def restore_private(acc: Account) -> int:
    """把档案里的私有存储还原到 WorkBuddy 的 storage/user-{uid}-*。

    返回还原的文件数；档案没有私有副本时返回 0。
    """
    src = acc.private_dir()
    if not src.exists():
        return 0
    dst = paths.user_storage_dir(acc.uid)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    return sum(1 for p in dst.rglob("*") if p.is_file())


# --------------------------------------------------------------------------
# 登录态（凭据级换号用）
#
# 登录会话存在 <扩展数据目录>/Data/Public/auth/<authenticationId>.info，
# 内含明文 accessToken / refreshToken。因此：
#   · 快照目录按用户权限收紧（Windows 用 icacls，POSIX 用 chmod 600）；
#   · 绝不把令牌写进日志或档案 JSON；
#   · 换号写回后必须清掉 `<file>.logged-out`，否则客户端会忽略这份会话。
# --------------------------------------------------------------------------


def _harden(path: Path, is_dir: bool = False) -> None:
    """把凭据文件/目录的权限收紧到仅当前用户可读写。失败不致命。"""
    try:
        if platform.system() == "Windows":
            import subprocess

            user = os.environ.get("USERNAME", "")
            if not user:
                return
            subprocess.run(
                ["icacls", str(path), "/inheritance:r"],
                capture_output=True, timeout=15,
            )
            grant = f"{user}:(OI)(CI)(F)" if is_dir else f"{user}:(R,W)"
            subprocess.run(
                ["icacls", str(path), "/grant:r", grant],
                capture_output=True, timeout=15,
            )
        else:
            os.chmod(path, 0o700 if is_dir else 0o600)
    except Exception:
        pass


def _adopt_login_file(acc: Account, src: Path) -> int:
    """把一份登录态文件收进档案（内部用，调用方负责判定它属于该账号）。"""
    if not src.exists():
        return 0
    staging = acc.session_dir().with_name("session.tmp")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, staging / src.name)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return 0

    dst = acc.session_dir()
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    staging.replace(dst)
    _harden(dst, is_dir=True)
    for f in dst.glob("*.info"):
        _harden(f)
    acc.has_login_state = True
    return 1


def snapshot_login_state(acc: Account) -> int:
    """把客户端**该版本**当前登录的会话复制进档案，返回复制的文件数。

    客户端运行时该文件可能被占用，写入会失败；失败时保留原快照不动，
    免得一次失败的刷新把之前存好的登录态抹掉。
    """
    src = paths.login_state_file(acc.edition)
    if src is None or not src.exists():
        acc.has_login_state = any(acc.session_dir().glob("*.info"))
        return 0
    return _adopt_login_file(acc, src)


def import_login_states(edition: str | None = None) -> dict[str, str]:
    """从客户端 auth 目录把**指定版本**的历史登录会话导入对应账号档案。

    客户端会在该目录保留历次落盘的 .info（长期有效）。把它们收进本地
    档案后，这些账号就都能免登录切换，而不只是"最后一次登录的那个"。

    只导入「文件版本 == 账号版本」的配对 —— 跨版本导入会把国内版的令牌
    塞进国际版账号的档案，换号时就会串号。

    返回 {uid: 账号名}；没有可导入的返回空 dict。
    """
    ed = paths.normalize_edition(edition) or paths.current_edition()
    found = paths.candidate_login_files(ed)
    if not found:
        return {}

    imported: dict[str, str] = {}
    for uid, src in found.items():
        acc = find_by_uid(uid)
        if acc is None or acc.edition != ed:
            continue
        try:
            if _adopt_login_file(acc, src):
                save_account(acc)
                imported[uid] = acc.name
        except Exception:
            continue
    return imported


def restore_login_state(acc: Account) -> int:
    """把档案里的登录会话写回客户端**该版本**的登录文件，返回还原的文件数。

    档案里没有快照时返回 0，调用方应据此跳过凭据切换，
    而不是把现有登录态清空。

    注意写回的文件名必须是该版本客户端读取的那个：
      · 国内版  `workbuddy-desktop.info`
      · 国际版  `workbuddy-desktop-ai.info`
    快照可能是客户端的时间戳中间产物名，原样放回去客户端不会认；
    文件名按账号版本重新命名，绝不写进另一个版本的文件。
    """
    src = acc.session_dir()
    if not src.exists():
        return 0
    files = [f for f in sorted(src.iterdir()) if f.is_file() and f.suffix == ".info"]
    if not files:
        return 0

    dst_dir = paths.login_state_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / paths.auth_file_name(acc.edition)
    paths.logout_marker_path(target).unlink(missing_ok=True)

    # 优先用主干名那份；否则取 mtime 最新的一份
    main = [f for f in files if "." not in f.stem]
    pick = main[0] if main else max(files, key=lambda p: p.stat().st_mtime)

    tmp = target.with_name(target.name + ".wbswitch-tmp")
    shutil.copy2(pick, tmp)
    tmp.replace(target)
    _harden(target)
    return 1
