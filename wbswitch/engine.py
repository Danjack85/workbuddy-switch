# -*- coding: utf-8 -*-
"""同步引擎：把 A 账号的本地数据交接给 B 账号。

覆盖 WorkBuddy 里所有按 user_id 隔离的数据：

| 数据            | 位置                                  | 隔离方式            | 策略          |
|-----------------|---------------------------------------|---------------------|---------------|
| 会话记录        | workbuddy.db `sessions.user_id`       | 字段                | UPDATE 重打标 |
| 定时任务        | workbuddy.db `automations.owner_user_id` | 字段             | UPDATE 重打标 |
| 长期记忆        | memory/{uid}_memory.md                | 文件名              | Memory Block 智能合并 |
| 连接器配置      | connectors/{uid}/mcp.json             | 目录                | JSON 深度合并 |
| 账号私有存储    | storage/user-{uid}-*/                 | 目录                | 档案级快照还原 |

安全规则（继承 workbuddy-account-migrate 的教训）：
1. 迁移前必须全量备份，不可跳过；
2. 源 ≠ 目标；
3. 记忆按「Memory Block」正文合并，绝不把 `> Last updated:` / `"uid": ...`
   这类元数据碎片当内容追加进去（上游脚本的坑）；
4. 连接器深度合并，目标已有 key 不动；
5. 绝不复制 `.master.key` 与 connector-states.json 里的 encryption / accountIdentityKey
   —— 那是按 user_id 绑定的密钥材料，复制会破坏目标账号的解密能力；
6. 迁移前后各做一次 WAL checkpoint，并校验源 user_id 归零；
7. 写入全部走临时文件 + 原子 rename。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths, profiles
from .i18n import t

# --------------------------------------------------------------------------
# 报告结构
# --------------------------------------------------------------------------


@dataclass
class StepResult:
    name: str
    ok: bool = True
    changed: int = 0
    detail: str = ""
    skipped: bool = False


@dataclass
class SyncReport:
    source_uid: str = ""
    target_uid: str = ""
    dry_run: bool = False
    steps: list[StepResult] = field(default_factory=list)
    backup_tag: str = ""
    warnings: list[str] = field(default_factory=list)
    sessions_before: int = 0
    sessions_after: int = 0

    def add(self, step: StepResult) -> None:
        self.steps.append(step)

    def find(self, name: str) -> StepResult | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def total_changed(self) -> int:
        return sum(s.changed for s in self.steps)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_changed"] = self.total_changed()
        return d

    def summary_bits(self) -> list[str]:
        """给 UI 的一行摘要。"""
        bits: list[str] = []
        s = self.find("sessions")
        if s and s.changed:
            bits.append(t("res.sessions", n=s.changed))
        m = self.find("memory")
        if m and m.changed:
            bits.append(t("res.memory", n=m.changed))
        c = self.find("connectors")
        if c and c.changed:
            bits.append(t("res.connectors", n=c.changed))
        a = self.find("automations")
        if a and a.changed:
            bits.append(t("res.automations", n=a.changed))
        st = self.find("settings")
        if st and st.changed:
            bits.append(t("res.settings", n=st.changed))
        if self.backup_tag:
            bits.append(t("res.backup", tag=self.backup_tag))
        return bits


# --------------------------------------------------------------------------
# 只读扫描
# --------------------------------------------------------------------------


def _db() -> sqlite3.Connection:
    if not paths.db_path().exists():
        raise RuntimeError(t("err.no_db", path=paths.db_path()))
    conn = sqlite3.connect(str(paths.db_path()))
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """表里是否有某列。

    用表值 PRAGMA 函数，表名走**参数绑定**。`PRAGMA table_info(x)` 那种写法必须
    把标识符拼进语句，是没必要的注入面；pragma_table_info 接受绑定参数，
    因此这里不含任何字符串拼接（需要 SQLite 3.16+，Python 3.9+ 远高于此）。
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def session_counts() -> dict[str, int]:
    """各 uid 的会话数。数据库不可用时返回空 dict。"""
    try:
        conn = _db()
    except Exception:
        return {}
    try:
        if not _table_exists(conn, "sessions"):
            return {}
        rows = conn.execute(
            "SELECT user_id, COUNT(*) FROM sessions WHERE user_id IS NOT NULL GROUP BY user_id"
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows if r[0]}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def automation_counts() -> dict[str, int]:
    try:
        conn = _db()
    except Exception:
        return {}
    try:
        if not _table_exists(conn, "automations"):
            return {}
        if not _column_exists(conn, "automations", "owner_user_id"):
            return {}
        rows = conn.execute(
            "SELECT owner_user_id, COUNT(*) FROM automations "
            "WHERE owner_user_id IS NOT NULL AND deleted_at IS NULL GROUP BY owner_user_id"
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows if r[0]}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 长期记忆：解析 / 渲染 / 合并
# --------------------------------------------------------------------------

_RAW_START = "<!-- RAW_JSON_START"
_RAW_END = "RAW_JSON_END -->"
_BLOCK_HEAD = "## Memory Block"


@dataclass
class MemoryDoc:
    block: str = ""
    raw: dict | None = None
    version: int = 0
    updated_at: str = ""
    is_template: bool = True


def parse_memory_text(text: str) -> MemoryDoc:
    doc = MemoryDoc()
    # RAW_JSON 段
    i, j = text.find(_RAW_START), text.find(_RAW_END)
    if i != -1 and j != -1 and j > i:
        seg = text[i + len(_RAW_START) : j]
        seg = seg.lstrip("->").strip()
        try:
            raw = json.loads(seg)
            if isinstance(raw, dict):
                doc.raw = raw
                doc.version = int(raw.get("version") or 0)
                doc.updated_at = str(raw.get("updatedAt") or "")
                doc.block = str(raw.get("memoryBlock") or "")
        except Exception:
            doc.raw = None
    # 正文段（RAW_JSON 缺失时回退到 markdown 正文）
    if not doc.block:
        h = text.find(_BLOCK_HEAD)
        if h != -1:
            body = text[h + len(_BLOCK_HEAD) :]
            cut = body.find("---")
            if cut == -1:
                cut = body.find(_RAW_START)
            if cut != -1:
                body = body[:cut]
            doc.block = body.strip()
    if not doc.updated_at:
        m = re.search(r">\s*Last updated:\s*(\S+)", text)
        if m:
            doc.updated_at = m.group(1)
    doc.block = (doc.block or "").strip()
    doc.is_template = not doc.block
    return doc


def read_memory(uid: str) -> MemoryDoc:
    f = paths.memory_file(uid)
    if not f.exists():
        return MemoryDoc()
    try:
        return parse_memory_text(f.read_text(encoding="utf-8"))
    except Exception:
        return MemoryDoc()


def render_memory(doc: MemoryDoc, uid: str, updated_at: str | None = None) -> str:
    ts = updated_at or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    raw = {
        "uid": uid,
        "memoryBlock": doc.block,
        "updatedAt": ts,
        "version": doc.version,
    }
    return (
        "# User Memory Profile\n"
        f"> Last updated: {ts}\n"
        f"> Version: {doc.version}\n"
        "\n"
        f"{_BLOCK_HEAD}\n"
        "\n"
        f"{doc.block}\n"
        "\n"
        "---\n"
        "\n"
        f"{_RAW_START}\n"
        f"{json.dumps(raw, ensure_ascii=False, indent=2)}\n"
        f"{_RAW_END}\n"
    )


def memory_line_count(uid: str) -> int:
    return len([l for l in read_memory(uid).block.splitlines() if l.strip()])


# --------------------------------------------------------------------------
# 连接器
# --------------------------------------------------------------------------

#: 这些是按键绑定的密钥材料，跨账号复制会破坏解密能力，永不迁移。
_CONNECTOR_FORBIDDEN_FILES = {".master.key"}
_CONNECTOR_FORBIDDEN_KEYS = {
    "encryption",
    "accountidentitykey",
    "account_identity_key",
    "masterkey",
    "master_key",
}


def read_mcp_servers(uid: str) -> dict:
    f = paths.connector_dir(uid) / "mcp.json"
    data = profiles.read_json_safe(f)
    if isinstance(data, dict):
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            return servers
    return {}


def connector_state_count(uid: str) -> int:
    f = paths.connector_dir(uid) / "connector-states.json"
    data = profiles.read_json_safe(f)
    return len(data) if isinstance(data, dict) else 0


def deep_merge(source: Any, target: Any) -> Any:
    """深度合并：target 已有的 key 一律保留不动，只补 source 独有的。"""
    if isinstance(source, dict) and isinstance(target, dict):
        for k, v in source.items():
            if k not in target:
                target[k] = v
            else:
                target[k] = deep_merge(v, target[k])
        return target
    return target


def _strip_forbidden(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_forbidden(v)
            for k, v in obj.items()
            if k.lower().replace("_", "") not in {s.replace("_", "") for s in _CONNECTOR_FORBIDDEN_KEYS}
        }
    if isinstance(obj, list):
        return [_strip_forbidden(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# 备份
# --------------------------------------------------------------------------


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def human_size(n: int) -> str:
    if n <= 0:
        return "-"
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n}B"


@dataclass
class BackupInfo:
    tag: str
    path: str
    created_at: str = ""
    target_uid: str = ""
    source_uid: str = ""
    label: str = ""
    size_bytes: int = 0
    has_db: bool = False
    has_memory: bool = False
    has_connectors: bool = False
    has_storage: bool = False
    has_settings: bool = False
    has_login_state: bool = False
    has_sessions_archive: bool = False

    def size_text(self) -> str:
        return human_size(self.size_bytes)


def _unique_backup_dir(base_tag: str) -> tuple[str, Path]:
    """给备份找一个不冲突的目录名。

    tag 只精确到秒，同一秒内的两次备份（典型场景：回滚前自动备份，恰好与
    被回滚的那个备份同秒同 uid）会落到同一目录。那样回滚会先把要恢复的
    备份覆盖掉、再从被覆盖的内容恢复，等于什么都没回滚。所以这里必须去重。
    """
    root = paths.backups_dir()
    if not (root / base_tag).exists():
        return base_tag, root / base_tag
    for n in range(2, 1000):
        cand = f"{base_tag}-{n}"
        if not (root / cand).exists():
            return cand, root / cand
    raise RuntimeError(f"backup tag exhausted: {base_tag}")


def create_backup(
    target_uid: str,
    source_uid: str = "",
    label: str = "",
    tag: str | None = None,
) -> BackupInfo:
    """全量备份数据库、记忆、连接器、账号私有存储与定时任务文件。

    备份是迁移的前置条件，任何失败都会抛出异常阻止后续写入。
    """
    paths.ensure_store_dirs()
    ts = time.strftime("%Y%m%d%H%M%S")
    tag, dest = _unique_backup_dir(tag or f"{ts}_{(target_uid or 'none')[:8]}")
    dest.mkdir(parents=True, exist_ok=True)

    info = BackupInfo(
        tag=tag,
        path=str(dest),
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        target_uid=target_uid,
        source_uid=source_uid,
        label=label,
    )

    # 数据库（含 WAL/SHM，保证备份可用）
    try:
        conn = _db()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(paths.db_path()) + suffix)
        if src.exists():
            shutil.copy2(src, dest / src.name)
            info.has_db = True

    for name, flag in (
        ("memory", "has_memory"),
        ("connectors", "has_connectors"),
        ("storage", "has_storage"),
        ("tasks", ""),
    ):
        src = paths.workbuddy_dir() / name
        if src.exists():
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
            if flag:
                setattr(info, flag, True)

    # 客户端全局设置（含按账号隔离的 claw.users.{uid} 段）
    cfg = paths.settings_path()
    if cfg.exists():
        shutil.copy2(cfg, dest / cfg.name)
        info.has_settings = True

    # 会话档案库：这是"换号后会话自动恢复"的本钱，必须跟备份一起走
    arch = paths.store_dir() / "sessions.db"
    if arch.exists():
        shutil.copy2(arch, dest / arch.name)
        info.has_sessions_archive = True

    # 登录态（凭据文件）。客户端运行时可能被占用，失败不致命：
    # 备份仍可用，只是回滚后需要重新登录。
    sess_dst = dest / "session"
    copied_login = 0
    for src in paths.login_state_files():
        try:
            if src.exists():
                sess_dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, sess_dst / src.name)
                copied_login += 1
        except OSError:
            pass
    info.has_login_state = copied_login > 0

    meta = asdict(info)
    meta["size_bytes"] = _dir_size(dest)
    meta["app"] = "workbuddy-switch"
    profiles.atomic_write_json(dest / "meta.json", meta)

    info.size_bytes = meta["size_bytes"]
    return info


def list_backups() -> list[BackupInfo]:
    d = paths.backups_dir()
    if not d.exists():
        return []
    out: list[BackupInfo] = []
    for entry in d.iterdir():
        if not entry.is_dir():
            continue
        meta = profiles.read_json_safe(entry / "meta.json") or {}
        info = BackupInfo(
            tag=str(meta.get("tag") or entry.name),
            path=str(entry),
            created_at=str(meta.get("created_at") or ""),
            target_uid=str(meta.get("target_uid") or ""),
            source_uid=str(meta.get("source_uid") or ""),
            label=str(meta.get("label") or ""),
            size_bytes=int(meta.get("size_bytes") or 0),
            has_db=bool(meta.get("has_db")),
            has_memory=bool(meta.get("has_memory")),
            has_connectors=bool(meta.get("has_connectors")),
            has_storage=bool(meta.get("has_storage")),
            has_settings=bool(meta.get("has_settings")),
            has_login_state=bool(meta.get("has_login_state")),
            has_sessions_archive=bool(meta.get("has_sessions_archive")),
        )
        if not info.size_bytes:
            info.size_bytes = _dir_size(entry)
        out.append(info)
    out.sort(key=lambda b: b.tag, reverse=True)
    return out


def find_backup(tag: str) -> BackupInfo | None:
    for b in list_backups():
        if b.tag == tag:
            return b
    return None


def restore_backup(tag: str) -> list[str]:
    """用备份覆盖当前数据。返回已还原的项目列表。"""
    info = find_backup(tag)
    if info is None:
        raise RuntimeError(t("err.no_backup", tag=tag))
    src = Path(info.path)
    done: list[str] = []

    # 还原前先把当前状态也备份一次，避免"回滚把回滚本身弄丢"
    create_backup(info.target_uid, label=f"pre-rollback {tag}")

    db_src = src / "workbuddy.db"
    if db_src.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(paths.db_path()) + suffix)
            p.unlink(missing_ok=True)
        shutil.copy2(db_src, paths.db_path())
        done.append("workbuddy.db")

    for name in ("memory", "connectors", "storage", "tasks"):
        s = src / name
        if not s.exists():
            continue
        d = paths.workbuddy_dir() / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(s, d)
        done.append(name)

    cfg_src = src / paths.settings_path().name
    if cfg_src.exists():
        try:
            shutil.copy2(cfg_src, paths.settings_path())
            done.append("settings.json")
        except OSError:
            pass

    # 会话档案库回滚（这是会话留存的根，回滚时一并还原）
    arch_src = src / "sessions.db"
    if arch_src.exists():
        try:
            paths.ensure_store_dirs()
            shutil.copy2(arch_src, paths.store_dir() / "sessions.db")
            done.append("sessions.db")
        except OSError:
            pass

    # 登录态：客户端已退出时才写得进去，失败不阻断其余还原。
    sess_src = src / "session"
    if sess_src.exists():
        dst = paths.login_state_dir()
        restored = 0
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for f in sess_src.iterdir():
                if f.is_file():
                    # 先清登出标记，否则客户端会忽略刚还原的会话
                    paths.logout_marker_path(dst / f.name).unlink(missing_ok=True)
                    shutil.copy2(f, dst / f.name)
                    restored += 1
        except OSError:
            restored = 0
        if restored:
            done.append("session")

    return done


# --------------------------------------------------------------------------
# 各步骤迁移
# --------------------------------------------------------------------------


def merge_sessions(source_uid: str, target_uid: str, dry_run: bool = False) -> StepResult:
    if source_uid == target_uid:
        return StepResult("sessions", ok=True, skipped=True, detail="same uid")
    try:
        conn = _db()
    except Exception as e:
        return StepResult("sessions", ok=False, detail=str(e))

    try:
        if not _table_exists(conn, "sessions"):
            return StepResult("sessions", ok=False, detail="no sessions table")
        # 迁移前 checkpoint，确保读到最新数据
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

        count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (source_uid,)
        ).fetchone()[0]
        if not count:
            return StepResult("sessions", ok=True, skipped=True, changed=0, detail=t("err.no_sessions"))

        if dry_run:
            return StepResult(
                "sessions", ok=True, changed=int(count), detail=f"[dry-run] would move {count}"
            )

        cur = conn.cursor()
        cur.execute(
            "UPDATE sessions SET user_id = ? WHERE user_id = ?", (target_uid, source_uid)
        )
        migrated = cur.rowcount
        conn.commit()

        # 迁移后再 checkpoint，确保落盘（上游脚本的教训：不 checkpoint 重启后会丢）
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

        remaining = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (source_uid,)
        ).fetchone()[0]
        if remaining:
            return StepResult(
                "sessions",
                ok=False,
                changed=int(migrated),
                detail=f"source still has {remaining} sessions",
            )
        return StepResult("sessions", ok=True, changed=int(migrated), detail="verified")
    except sqlite3.Error as e:
        return StepResult("sessions", ok=False, detail=str(e))
    finally:
        conn.close()


def merge_automations(source_uid: str, target_uid: str, dry_run: bool = False) -> StepResult:
    if source_uid == target_uid:
        return StepResult("automations", ok=True, skipped=True, detail="same uid")
    try:
        conn = _db()
    except Exception as e:
        return StepResult("automations", ok=False, detail=str(e))
    try:
        if not _table_exists(conn, "automations") or not _column_exists(
            conn, "automations", "owner_user_id"
        ):
            return StepResult("automations", ok=True, skipped=True, detail="not user-scoped")
        count = conn.execute(
            "SELECT COUNT(*) FROM automations WHERE owner_user_id = ? AND deleted_at IS NULL",
            (source_uid,),
        ).fetchone()[0]
        if not count:
            return StepResult("automations", ok=True, skipped=True)
        if dry_run:
            return StepResult(
                "automations", ok=True, changed=int(count), detail=f"[dry-run] would move {count}"
            )
        cur = conn.cursor()
        cur.execute(
            "UPDATE automations SET owner_user_id = ? WHERE owner_user_id = ?",
            (target_uid, source_uid),
        )
        moved = cur.rowcount
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        return StepResult("automations", ok=True, changed=int(moved))
    except sqlite3.Error as e:
        return StepResult("automations", ok=False, detail=str(e))
    finally:
        conn.close()


def merge_memory(source_uid: str, target_uid: str, dry_run: bool = False) -> StepResult:
    if source_uid == target_uid:
        return StepResult("memory", ok=True, skipped=True, detail="same uid")

    src = read_memory(source_uid)
    if not src.block:
        # 上游脚本在这里会把 "uid": "..." 之类元数据当内容追加，必须跳过
        return StepResult("memory", ok=True, skipped=True, detail="source memory block empty")

    dst_path = paths.memory_file(target_uid)
    dst = read_memory(target_uid)

    src_lines = [l for l in src.block.splitlines() if l.strip()]
    dst_set = {l for l in dst.block.splitlines() if l.strip()}
    new_lines = [l for l in src_lines if l not in dst_set]

    if not new_lines:
        return StepResult("memory", ok=True, skipped=True, detail="already merged")

    if dry_run:
        return StepResult(
            "memory", ok=True, changed=len(new_lines), detail=f"[dry-run] {len(new_lines)} lines"
        )

    stamp = time.strftime("%Y-%m-%d %H:%M")
    marker = f"<!-- merged from {source_uid[:8]} at {stamp} -->"
    body = dst.block.strip()
    addition = "\n".join([marker, *new_lines])
    merged_block = f"{body}\n\n{addition}" if body else addition

    dst.block = merged_block
    dst.version = max(dst.version, src.version)
    text = render_memory(dst, target_uid)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.with_name(dst_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dst_path)

    # 顺手保留一份合并前的目标记忆，便于人工比对
    if body:
        bak = dst_path.with_suffix(".md.bak")
        try:
            bak.write_text(render_memory(MemoryDoc(block=body, version=dst.version), target_uid), encoding="utf-8")
        except Exception:
            pass

    return StepResult("memory", ok=True, changed=len(new_lines))


def merge_connectors(source_uid: str, target_uid: str, dry_run: bool = False) -> StepResult:
    if source_uid == target_uid:
        return StepResult("connectors", ok=True, skipped=True, detail="same uid")

    src_dir = paths.connector_dir(source_uid)
    if not src_dir.exists():
        return StepResult("connectors", ok=True, skipped=True, detail="no source dir")

    dst_dir = paths.connector_dir(target_uid)
    src_mcp = profiles.read_json_safe(src_dir / "mcp.json")
    if not isinstance(src_mcp, dict):
        return StepResult("connectors", ok=True, skipped=True, detail="no source mcp.json")

    src_servers = src_mcp.get("mcpServers")
    if not isinstance(src_servers, dict) or not src_servers:
        return StepResult("connectors", ok=True, skipped=True, detail="no servers")

    dst_mcp = profiles.read_json_safe(dst_dir / "mcp.json")
    if not isinstance(dst_mcp, dict):
        dst_mcp = {"mcpServers": {}}
    dst_servers = dst_mcp.get("mcpServers")
    if not isinstance(dst_servers, dict):
        dst_servers = {}
        dst_mcp["mcpServers"] = dst_servers

    added = [k for k in src_servers if k not in dst_servers]
    if not added:
        return StepResult("connectors", ok=True, skipped=True, detail="no new keys")

    if dry_run:
        return StepResult(
            "connectors", ok=True, changed=len(added), detail=f"[dry-run] {added[:5]}"
        )

    merged = deep_merge(_strip_forbidden(src_servers), dst_servers)
    dst_mcp["mcpServers"] = merged
    dst_dir.mkdir(parents=True, exist_ok=True)
    profiles.atomic_write_json(dst_dir / "mcp.json", dst_mcp)

    # 注意：.master.key 与 connector-states.json 里的 encryption / accountIdentityKey
    # 是按 uid 绑定的密钥材料，故意不迁移。
    return StepResult("connectors", ok=True, changed=len(added), detail=f"{added[:5]}")


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def merge_settings(source_uid: str, target_uid: str, dry_run: bool = False) -> StepResult:
    """把 settings.json 里按账号隔离的段落从 source 复制到 target。

    客户端的 `claw.users.{uid}` 是账号级的消息渠道配置。缺了它，换号后
    微信/企业微信等渠道会表现为「未配置」，需要重新绑定。
    这里的合并策略是：只在 target 还没有该 uid 段落时补齐，绝不覆盖已有配置。
    """
    if source_uid == target_uid:
        return StepResult("settings", ok=True, skipped=True, detail="same uid")

    path = paths.settings_path()
    data = profiles.read_json_safe(path)
    if not isinstance(data, dict):
        return StepResult("settings", ok=True, skipped=True, detail="no settings.json")

    claw = data.get("claw")
    if not isinstance(claw, dict):
        return StepResult("settings", ok=True, skipped=True, detail="no claw section")
    users = claw.get("users")
    if not isinstance(users, dict):
        return StepResult("settings", ok=True, skipped=True, detail="no claw.users")

    src_block = users.get(source_uid)
    if not isinstance(src_block, dict) or not src_block:
        return StepResult("settings", ok=True, skipped=True, detail="no source user block")
    if target_uid in users:
        return StepResult("settings", ok=True, skipped=True, detail="target already configured")

    if dry_run:
        return StepResult("settings", ok=True, changed=1, detail="[dry-run] would copy claw.users block")

    users[target_uid] = src_block
    claw["users"] = users
    data["claw"] = claw
    profiles.atomic_write_json(path, data)
    return StepResult("settings", ok=True, changed=1, detail="claw.users block copied")


def sync(
    source_uid: str,
    target_uid: str,
    *,
    do_backup: bool = True,
    do_sessions: bool = True,
    do_memory: bool = True,
    do_connectors: bool = True,
    do_automations: bool = True,
    do_settings: bool = True,
    dry_run: bool = False,
    label: str = "",
) -> SyncReport:
    """把 source_uid 的本地数据交接给 target_uid。

    注意 do_sessions 默认开启只适用于「把某账号的数据并入当前账号」这种一次性
    交接。一键换号**不应**用它来搬会话：会话的正确归属由 sessions 档案库按
    owner 管理（见 sessions.activate_for），搬移会让账号间来回丢数据。
    """
    report = SyncReport(source_uid=source_uid, target_uid=target_uid, dry_run=dry_run)

    if not source_uid or not target_uid:
        report.warnings.append("empty uid")
        return report
    if source_uid == target_uid:
        report.warnings.append(t("err.same"))
        return report

    counts = session_counts()
    report.sessions_before = counts.get(target_uid, 0)

    if do_backup and not dry_run:
        info = create_backup(target_uid, source_uid, label or "sync")
        report.backup_tag = info.tag

    if do_sessions:
        report.add(merge_sessions(source_uid, target_uid, dry_run))
    if do_automations:
        report.add(merge_automations(source_uid, target_uid, dry_run))
    if do_memory:
        report.add(merge_memory(source_uid, target_uid, dry_run))
    if do_connectors:
        report.add(merge_connectors(source_uid, target_uid, dry_run))
    if do_settings:
        report.add(merge_settings(source_uid, target_uid, dry_run))

    if not dry_run:
        after = session_counts()
        report.sessions_after = after.get(target_uid, 0)
        # 只有真的搬过会话才谈得上"源还剩下多少"；换号时会话由档案库管理，不在此列
        if do_sessions and after.get(source_uid, 0) > 0:
            report.warnings.append(f"source uid still has {after[source_uid]} sessions")

    for step in report.steps:
        if not step.ok:
            report.warnings.append(f"{step.name}: {step.detail}")

    return report


# --------------------------------------------------------------------------
# 数据发现与统计
# --------------------------------------------------------------------------


def discover_uids() -> list[str]:
    """扫描所有已知 uid（数据库、记忆文件、连接器目录、账号私有存储）。"""
    found: set[str] = set()
    found.update(session_counts().keys())
    found.update(automation_counts().keys())

    mdir = paths.memory_dir()
    if mdir.exists():
        for f in mdir.glob("*_memory.md"):
            uid = f.stem[: -len("_memory")]
            if uid:
                found.add(uid)

    cdir = paths.connectors_dir()
    if cdir.exists():
        for d in cdir.iterdir():
            if d.is_dir() and d.name not in ("default", "skills") and "-" in d.name:
                found.add(d.name)

    sdir = paths.storage_dir()
    if sdir.exists():
        for d in sdir.iterdir():
            if d.is_dir() and d.name.startswith("user-"):
                rest = d.name[len("user-") :]
                parts = rest.rsplit("-", 1)
                if len(parts) == 2:
                    found.add(parts[0])

    return sorted(u for u in found if u)


def scan_stats(uid: str) -> profiles.Stats:
    """只读统计某账号的数据规模。"""
    st = profiles.Stats()
    st.sessions = session_counts().get(uid, 0)
    st.automations = automation_counts().get(uid, 0)

    mem = paths.memory_file(uid)
    if mem.exists():
        try:
            st.memory_bytes = mem.stat().st_size
        except OSError:
            pass
        st.memory_lines = memory_line_count(uid)

    st.mcp_servers = len(read_mcp_servers(uid))
    st.connector_states = connector_state_count(uid)

    priv = paths.user_storage_dir(uid)
    if priv.exists():
        st.private_files = sum(1 for p in priv.rglob("*") if p.is_file())
    return st


def integrity_check() -> str:
    try:
        conn = _db()
    except Exception as e:
        return f"error: {e}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"
    except sqlite3.Error as e:
        return f"error: {e}"
    finally:
        conn.close()
