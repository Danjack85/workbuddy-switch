# -*- coding: utf-8 -*-
"""本地会话档案库：让会话独立于「当前登录账号」而长期留存。

## 为什么需要它

WorkBuddy 的会话表用 `user_id` 做隔离，客户端**只显示当前登录账号名下的会话**。
而 `sessions.id` 是主键，一条会话只能挂在一个 `user_id` 下。于是：

- 换号后，旧账号的会话还在磁盘上，但客户端不显示 → 看起来「丢了」；
- 如果用「把会话 UPDATE 成新 uid」的搬法，会话就真的改了归属。来回切几次，
  会话会在账号间来回搬，总有一边是空的。

## 本模块的做法

1. **留存**：把客户端会话表里的每一行完整复制进本地档案库
   （`~/.workbuddy-switch/sessions.db`），并记下它**原本属于哪个账号**
   （owner_uid）。首次记录后，owner 不再随搬运而改变。
2. **还原**：登录/切换到账号 X 时，把归档里所有 owner=X 的会话写回客户端并
   标记为 X（X 立刻能看到自己的全部历史）；其余会话的 `user_id` 归位回各自
   owner（对 X 不可见，但**数据仍在**，切回去立刻恢复）。

效果：账号 A 的会话永远是 A 的，切到 B 再切回 A，会话原样回来，双向无损。

## 兼容旧数据

档案库首次见到某个会话时，只能把「当前 user_id」当作 owner —— 如果历史上被
别的工具改过归属，那次的原始归属已经无法追回。从本工具接管之后不再发生。

## SQL 约定

每条 SQL 都作为字面量直接写在 execute/executemany 调用里（补写整行的语句是
一条列数固定的完整字面量），值一律用 `?` 参数绑定，批量操作用 executemany。
没有任何拼接、格式化或 f-string 参与 SQL 文本。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: sessions 表的已知 schema。客户端 5.6.x 把表从 30 列扩到 40 列
#  （新增 transport…unread），且升级后旧字面量会让复制/补回全部拒绝执行。
#  这里按「列数 → 完整列名表 → 对应的整条 INSERT 字面量」组织：
#  PRAGMA 读出的列数与列名序列都与下表一致才放行写入，schema 再变时
#  明确报错而不是猜。
_SESSION_SCHEMA_30 = [
    "id", "cwd", "user_id", "title", "custom_title", "status",
    "created_at", "updated_at", "last_activity_at", "deleted_at",
    "is_playground", "source_mode", "is_background_automation", "mode",
    "model", "expert_id", "expert_locale", "expert_runtime_identity",
    "expert_marketplace", "permission_mode", "use_sandbox_cli",
    "project_id", "plugin_context_json", "addon_selection",
    "session_settings", "last_user_prompt_expert_selection",
    "context_window", "buddy_snapshot_id", "buddy_binding_json",
    "thought_level",
]
_SESSION_SCHEMA_40 = _SESSION_SCHEMA_30 + [
    "transport", "conversation_origin", "visibility", "group_id",
    "group_title", "agent_dirty", "agent_dirty_at", "agent_last_synced",
    "verified_at", "unread",
]
_SESSION_SCHEMAS = {30: _SESSION_SCHEMA_30, 40: _SESSION_SCHEMA_40}
_SESSION_INSERT_SQL = {
    30: "INSERT OR IGNORE INTO sessions VALUES ("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?,?)",
    40: "INSERT OR IGNORE INTO sessions VALUES ("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
}


def _match_sessions_schema(cols) -> int | None:
    """PRAGMA 列序列与已知 schema（列数 + 列名 + 顺序）完全一致时返回列数。"""
    known = _SESSION_SCHEMAS.get(len(cols))
    if known is not None and [str(c) for c in cols] == known:
        return len(cols)
    return None


@dataclass
class SessionStats:
    """档案库统计。"""

    total: int = 0
    by_owner: dict[str, int] = field(default_factory=dict)
    last_scan: str = ""

    def owner_count(self, uid: str) -> int:
        return self.by_owner.get(uid, 0)


@dataclass
class ActivateReport:
    uid: str = ""
    visible: int = 0        # 归到该账号名下、客户端将显示的会话数
    parked: int = 0         # 归位回各自 owner、对当前账号隐藏的会话数
    inserted: int = 0       # 档案里补回客户端的会话数
    new_archived: int = 0   # 本次新收进档案的会话数
    ok: bool = True
    detail: str = ""

    def summary(self) -> str:
        bits = ["visible=" + str(self.visible)]
        if self.parked:
            bits.append("parked=" + str(self.parked))
        if self.inserted:
            bits.append("restored=" + str(self.inserted))
        if self.new_archived:
            bits.append("archived=" + str(self.new_archived))
        return " ".join(bits)


def archive_path() -> Path:
    """档案库文件位置。"""
    return paths.store_dir() / "sessions.db"


def _conn() -> sqlite3.Connection:
    paths.ensure_store_dirs()
    conn = sqlite3.connect(str(archive_path()))
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS session_archive ("
        "session_id TEXT PRIMARY KEY, owner_uid TEXT NOT NULL,"
        " payload TEXT NOT NULL, first_seen TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_archive_owner"
        " ON session_archive(owner_uid)"
    )
    # 复制谱系：记录「哪条会话已经复制给过哪个账号」，防止重复复制出副本堆。
    # （参考实现的教训：丢了这层映射，每次操作都会多复制出一份重复会话。）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS copy_lineage ("
        "copy_id TEXT PRIMARY KEY, source_sid TEXT NOT NULL,"
        " source_uid TEXT NOT NULL, target_uid TEXT NOT NULL, created_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_copy_lineage_pair"
        " ON copy_lineage(source_sid, target_uid)"
    )
    return conn


def _live_conn() -> sqlite3.Connection:
    db = paths.db_path()
    if not db.exists():
        raise RuntimeError("workbuddy.db not found: " + str(db))
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _has_sessions_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    return row is not None


def _rows_as_dicts(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM sessions")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# 留存
# --------------------------------------------------------------------------


def capture() -> int:
    """把客户端当前所有会话收进档案库（幂等）。返回新记录的条数。

    owner 的判定：档案里已有记录就沿用（保住最初的归属），否则取当前 user_id。
    """
    try:
        live = _live_conn()
    except Exception:
        return 0

    try:
        if not _has_sessions_table(live):
            return 0
        rows = _rows_as_dicts(live)
    except sqlite3.Error:
        return 0
    finally:
        try:
            live.close()
        except Exception:
            pass

    if not rows:
        return 0

    arch = _conn()
    try:
        known = {
            str(r[0]): str(r[1])
            for r in arch.execute(
                "SELECT session_id, owner_uid FROM session_archive"
            ).fetchall()
        }
        new_count = 0
        stamp = _now()
        batch: list[tuple[str, str, str, str, str]] = []
        for row in rows:
            sid = str(row.get("id") or "")
            if not sid:
                continue
            payload = json.dumps(row, ensure_ascii=False, default=str)
            owner = known.get(sid)
            if owner is None:
                owner = str(row.get("user_id") or "")
                new_count += 1
            batch.append((sid, owner, payload, stamp, stamp))
        if batch:
            arch.executemany(
                "INSERT INTO session_archive"
                " (session_id, owner_uid, payload, first_seen, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " payload = excluded.payload, updated_at = excluded.updated_at",
                batch,
            )
            arch.commit()
        return new_count
    finally:
        arch.close()


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def owners() -> dict[str, str]:
    """session_id -> owner_uid。"""
    arch = _conn()
    try:
        return {
            str(r[0]): str(r[1])
            for r in arch.execute(
                "SELECT session_id, owner_uid FROM session_archive"
            ).fetchall()
        }
    finally:
        arch.close()


def archived(owner_uid: str | None = None) -> list[dict]:
    """档案里的会话（可按 owner 过滤），按创建时间排序。"""
    arch = _conn()
    try:
        if owner_uid:
            rows = arch.execute(
                "SELECT payload FROM session_archive WHERE owner_uid = ?",
                (owner_uid,),
            ).fetchall()
        else:
            rows = arch.execute("SELECT payload FROM session_archive").fetchall()
    finally:
        arch.close()

    out: list[dict] = []
    for (payload,) in rows:
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda d: int(d.get("created_at") or 0))
    return out


def stats() -> SessionStats:
    arch = _conn()
    try:
        rows = arch.execute(
            "SELECT owner_uid, COUNT(*) FROM session_archive GROUP BY owner_uid"
        ).fetchall()
        total = arch.execute("SELECT COUNT(*) FROM session_archive").fetchone()[0]
    finally:
        arch.close()
    return SessionStats(
        total=int(total),
        by_owner={str(r[0]): int(r[1]) for r in rows},
        last_scan=_now(),
    )


def owner_of(session_id: str) -> str:
    arch = _conn()
    try:
        row = arch.execute(
            "SELECT owner_uid FROM session_archive WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        arch.close()
    return str(row[0]) if row else ""


def _payload_of(session_id: str) -> dict | None:
    arch = _conn()
    try:
        row = arch.execute(
            "SELECT payload FROM session_archive WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        arch.close()
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# 会话正文归档（第一件）
# --------------------------------------------------------------------------
#
# 正文是会话的实体（`projects/{工作区}/{id}.jsonl`）。它按**工作区**存放、
# 不随账号隔离，所以换号本身不影响它；但被清理工具删掉、或工作区目录被移走时，
# 会话就会「列表里有、点开打不开」。
#
# 正文可能很大（实测单条最大 44 MB、全部 54 MB），所以：
#   · 用内容哈希做键，同一份正文只存一次（多条会话共享时天然去重）
#   · 只在内容变化时写入，避免反复复制大文件
#   · 归档失败不影响归属等其它功能，只如实记录缺失


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bodies_dir() -> Path:
    """正文归档目录（按内容哈希存放，天然去重）。"""
    return paths.store_dir() / "session-bodies"


def bodies_index_path() -> Path:
    """正文归档索引：session_id → 内容哈希 → 原工作区。

    为什么需要索引：正文是**按行存的 JSONL**，`sessionId` 字段可能出现在
    很靠后的元数据行（实测某条 13 KB 的正文里它在偏移 9032），指望"扫文件头
    认出它属于哪条会话"并不可靠。归档时顺手记下对应关系，还原时直接查，
    既准确又不用扫全部归档。
    """
    return paths.store_dir() / "session-bodies.json"


def _load_bodies_index() -> dict:
    try:
        data = json.loads(bodies_index_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_bodies_index(idx: dict) -> None:
    try:
        paths.ensure_store_dirs()
        f = bodies_index_path()
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        pass


def _human(n: int) -> str:
    """字节数转可读文本。"""
    if n <= 0:
        return "-"
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def archive_bodies(session_ids: list[str] | None = None,
                   *, on_progress=None) -> dict:
    """把会话正文收进归档目录（按内容哈希去重），并记下归属索引。

    `session_ids` 为空时归档全部已知会话。返回统计信息：

      {checked, stored, deduped, missing, bytes_added}
    """
    owner_map = owners()
    targets = session_ids if session_ids is not None else list(owner_map)
    store = bodies_dir()
    store.mkdir(parents=True, exist_ok=True)
    index = _load_bodies_index()

    stat = {"checked": 0, "stored": 0, "deduped": 0, "missing": 0, "bytes_added": 0}
    for i, sid in enumerate(targets, 1):
        if on_progress:
            on_progress(i, len(targets), sid)
        stat["checked"] += 1
        src = paths.find_session_body(sid)
        if src is None:
            stat["missing"] += 1
            continue
        try:
            digest = _sha256_file(src)
        except OSError:
            stat["missing"] += 1
            continue

        # 记索引：会话 id → 内容哈希 + 原工作区（还原时据此放回原处）
        index[sid] = {"sha256": digest, "workspace": src.parent.name,
                      "size": src.stat().st_size, "archived_at": time.strftime("%Y-%m-%d %H:%M:%S")}

        dest = store / f"{digest}.jsonl"
        if dest.exists():
            stat["deduped"] += 1
            continue
        try:
            import shutil

            shutil.copy2(src, dest)
            stat["stored"] += 1
            stat["bytes_added"] += dest.stat().st_size
        except OSError:
            stat["missing"] += 1
    _save_bodies_index(index)
    return stat


def restore_body(session_id: str, workspace: str | None = None) -> Path | None:
    """把归档里的正文还原回工作区。

    优先查索引（准），索引里没有时退回扫描归档文件（兼容手工放进来的正文）。
    `workspace` 指定目标工作区；不指定则用索引里记的原工作区，
    再不行落到第一个现有工作区。

    返回还原后的路径；归档里没有这条会话的正文时返回 None。
    """
    store = bodies_dir()
    if not store.exists():
        return None

    index = _load_bodies_index()
    entry = index.get(session_id) if isinstance(index.get(session_id), dict) else None
    blob: Path | None = None
    if entry and entry.get("sha256"):
        cand = store / f"{entry['sha256']}.jsonl"
        if cand.is_file():
            blob = cand

    if blob is None:
        # 兜底：扫描归档，看哪个文件里出现这条会话的 id
        needle = session_id.encode("utf-8")
        for cand in store.glob("*.jsonl"):
            try:
                with open(cand, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        if needle in chunk:
                            blob = cand
                            break
                if blob is not None:
                    break
            except OSError:
                continue
    if blob is None:
        return None

    target_dir = None
    if workspace:
        target_dir = paths.projects_dir() / workspace
    elif entry and entry.get("workspace"):
        cand = paths.projects_dir() / str(entry["workspace"])
        # 原工作区还在就用它，否则退回现有工作区
        if cand.parent.exists():
            target_dir = cand
    if target_dir is None:
        dirs = paths.session_body_dirs()
        if not dirs:
            return None
        target_dir = dirs[0]

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{session_id}.jsonl"
    try:
        import shutil

        shutil.copy2(blob, dest)
        return dest
    except OSError:
        return None



# --------------------------------------------------------------------------
# 还原
# --------------------------------------------------------------------------


def activate_for(uid: str) -> ActivateReport:
    """让客户端显示 uid 的会话，其余会话归位到各自 owner。

    步骤：
      1. 先 capture() —— 把客户端现状收进档案（含 owner 归属）；
      2. 档案里有、但客户端已丢失的会话 → 补写回客户端；
      3. 归属为 uid 的会话 → user_id=uid（可见）；
      4. 其余会话 → user_id=各自 owner（不可见但保留）。
    """
    rep = ActivateReport(uid=uid)
    if not uid:
        rep.ok = False
        rep.detail = "empty uid"
        return rep

    rep.new_archived = capture()
    owner_map = owners()
    if not owner_map:
        rep.ok = False
        rep.detail = "archive empty"
        return rep

    try:
        live = _live_conn()
    except Exception as e:
        rep.ok = False
        rep.detail = str(e)
        return rep

    try:
        if not _has_sessions_table(live):
            rep.ok = False
            rep.detail = "no sessions table"
            return rep

        cols_order = [r[1] for r in live.execute("PRAGMA table_info(sessions)")]
        if not cols_order:
            rep.ok = False
            rep.detail = "cannot read sessions schema"
            return rep

        live_ids = {
            str(r[0]) for r in live.execute("SELECT id FROM sessions").fetchall()
        }

        # ---- 步骤 2：补回档案里有、客户端已丢失的会话 ----
        # 整行按表声明顺序填值。INSERT 语句按列数分两个分支、各用一条
        # 完整字面量（30 列 = 5.5.x schema，40 列 = 5.6.x schema）；
        # 列数与列名序列都由 _match_sessions_schema 校验过才走到这里。
        n_cols = _match_sessions_schema(cols_order)
        if n_cols is None:
            rep.detail = (
                "sessions schema 未识别（"
                + str(len(cols_order))
                + " 列）；跳过补回，请反馈"
            )
        else:
            for sid in owner_map:
                if sid in live_ids:
                    continue
                row = _payload_of(sid)
                if not row or not row.get("id"):
                    continue
                values = [row.get(c) for c in cols_order]
                try:
                    if n_cols == 30:
                        live.execute(
                            "INSERT OR IGNORE INTO sessions VALUES ("
                            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                            "?,?,?,?,?,?,?,?,?,?)",
                            values,
                        )
                    else:  # 40 列（客户端 5.6.x）
                        live.execute(
                            "INSERT OR IGNORE INTO sessions VALUES ("
                            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            values,
                        )
                    rep.inserted += 1
                except sqlite3.Error:
                    pass

        # ---- 步骤 3：该账号的会话 → 可见 ----
        visible_pairs = [(uid, sid) for sid, o in owner_map.items() if o == uid]
        if visible_pairs:
            live.executemany(
                "UPDATE sessions SET user_id = ? WHERE id = ?", visible_pairs
            )
            rep.visible = len(visible_pairs)

        # ---- 步骤 4：其余会话 → 归位回各自 owner（对当前账号隐藏）----
        park_pairs = [(o, sid, o) for sid, o in owner_map.items() if o and o != uid]
        if park_pairs:
            cur = live.executemany(
                "UPDATE sessions SET user_id = ? WHERE id = ? AND user_id <> ?",
                park_pairs,
            )
            rep.parked = max(cur.rowcount or 0, 0)

        live.commit()
        try:
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        return rep
    except sqlite3.Error as e:
        rep.ok = False
        rep.detail = str(e)
        return rep
    finally:
        live.close()


# --------------------------------------------------------------------------
# 一致性检查
# --------------------------------------------------------------------------


def verify_roundtrip(uid: str) -> dict:
    """检查档案里 uid 的会话是否都能在客户端可见（换号后自检用）。"""
    owner_map = owners()
    want = {sid for sid, o in owner_map.items() if o == uid}
    try:
        live = _live_conn()
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    try:
        got = {
            str(r[0])
            for r in live.execute(
                "SELECT id FROM sessions WHERE user_id = ?", (uid,)
            ).fetchall()
        }
    finally:
        live.close()
    missing = sorted(want - got)
    return {
        "ok": not missing,
        "expected": len(want),
        "visible": len(got),
        "missing": missing[:20],
    }


def drift() -> dict:
    """检查客户端归属与档案库 owner 是否一致（宿主被外部工具改过时会不一致）。

    返回 {ok, drifted: {session_id: (client_uid, owner_uid)}, count, cloud}。

    `cloud` 是第三件（edge-sync 云端归属映射）的独立检查：
      {ok, count, drifted: {sid: [edge_uid, owner]}, missing_db}
    本地两处一致 ≠ 云端也一致 —— 客户端以别的账号登录运行期间会重写云端
    映射，之前只查本地就会漏报。
    """
    owner_map = owners()
    try:
        live = _live_conn()
    except Exception as e:
        return {"ok": False, "detail": str(e), "drifted": {}, "count": 0,
                "cloud": {"ok": False, "count": 0, "drifted": {}, "missing_db": False}}
    try:
        client_map = {
            str(r[0]): str(r[1])
            for r in live.execute("SELECT id, user_id FROM sessions").fetchall()
        }
    finally:
        live.close()

    out: dict[str, tuple[str, str]] = {}
    for sid, owner in owner_map.items():
        got = client_map.get(sid)
        if got is not None and got and got != owner:
            out[sid] = (got, owner)

    # ---- 第三件：云端归属 ----
    cloud_out: dict[str, list[str]] = {}
    edge_db = paths.edge_sync_db_path()
    cloud_missing = edge_db is None
    if not cloud_missing:
        try:
            conn = sqlite3.connect(str(edge_db))
            try:
                mapping = {
                    str(r[0]): str(r[1])
                    for r in conn.execute(
                        "SELECT session_id, msg_channel FROM edge_sync_mapping"
                    ).fetchall()
                }
            finally:
                conn.close()
        except sqlite3.Error:
            mapping = {}
            cloud_missing = True
        for sid, owner in owner_map.items():
            ch = mapping.get(sid)
            if not ch or not ch.startswith("convmsg:"):
                continue
            edge_uid = ch.split(":", 1)[1]
            if edge_uid and edge_uid != owner:
                cloud_out[sid] = [edge_uid, owner]

    cloud = {
        "ok": (not cloud_out) and not cloud_missing,
        "count": len(cloud_out),
        "drifted": cloud_out,
        "missing_db": cloud_missing,
    }
    return {
        "ok": (not out) and cloud["ok"],
        "drifted": out,
        "count": len(out),
        "cloud": cloud,
    }


@dataclass
class AdoptReport:
    """归属变更的结果。

    一条会话的归属存在**三个地方**，缺一不可（参考实现的说法是「数据三件套」）：

    | # | 位置 | 作用 |
    | --- | --- | --- |
    | 1 | `projects/{工作区}/{id}.jsonl` | 正文（按工作区存，不随账号变） |
    | 2 | `workbuddy.db` 的 `sessions.user_id` | 本地列表索引 |
    | 3 | `edge-sync-mapping*.db` 的 `msg_channel` | **云端归属** |

    只改第 2 处会让云端仍认为会话属于旧账号；只改第 3 处则列表里看不到。
    所以这里三处一起处理。
    """

    adopted: int = 0          # 档案库归属改动数
    client_rows: int = 0      # 客户端 sessions.user_id 改动数
    edge_rows: int = 0        # 云端归属映射改动数
    bodies_found: int = 0     # 找到了正文文件的会话数
    bodies_missing: int = 0   # 缺正文的会话数（列表可见但点不开）
    edge_db_missing: bool = False   # 客户端没有这个库（老版本）
    edge_db_busy: bool = False      # 库被占用改不了

    def ok(self) -> bool:
        return not self.edge_db_busy

    def text(self) -> str:
        bits = [f"归属 {self.adopted} 条", f"索引 {self.client_rows} 条"]
        if self.edge_rows:
            bits.append(f"云端 {self.edge_rows} 条")
        elif self.edge_db_missing:
            bits.append("云端库不存在（跳过）")
        elif self.edge_db_busy:
            bits.append("云端库被占用（未改）")
        if self.bodies_missing:
            bits.append(f"缺正文 {self.bodies_missing} 条")
        return " · ".join(bits)


def _update_edge_sync(session_ids: list[str], target_uid: str) -> tuple[int, bool, bool]:
    """改云端归属（第三件）。返回 (改动行数, 库是否不存在, 库是否被占用)。

    `msg_channel` 的格式实测是 `convmsg:<uid>`，客户端按它决定会话同步到谁的
    云端。这里只改这一列，`conversation_id` 保持原样 —— 它标识的是"这是哪条
    对话"，不承载归属语义。

    改不了不算致命：本地列表已经正确，只是云端可能仍按旧账号同步，
    所以返回状态让上层如实告知用户，而不是假装成功。
    """
    db = paths.edge_sync_db_path()
    if db is None:
        return 0, True, False
    try:
        conn = sqlite3.connect(str(db), timeout=8.0)
    except sqlite3.Error:
        return 0, False, True
    try:
        conn.execute("PRAGMA busy_timeout=8000")
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edge_sync_mapping'"
        ).fetchone():
            return 0, True, False
        channel = f"convmsg:{target_uid}"
        cur = conn.executemany(
            "UPDATE edge_sync_mapping SET msg_channel = ?"
            " WHERE session_id = ? AND msg_channel <> ?",
            [(channel, sid, channel) for sid in session_ids],
        )
        changed = cur.rowcount if isinstance(cur.rowcount, int) and cur.rowcount > 0 else 0
        conn.commit()
        return changed, False, False
    except sqlite3.Error:
        # 数据库被客户端独占时改不了；如实上报
        return 0, False, True
    finally:
        try:
            conn.close()
        except Exception:
            pass


def adopt_into(source_uid: str, target_uid: str, *, dry_run: bool = False) -> AdoptReport:
    """把 source 账号的会话**正式划归** target 账号（三处一起改）。

    这是「把旧号数据并到新号」的正确做法：光改客户端里的 user_id 会让档案库
    的 owner 记录与事实脱节，之后激活时又会被「归位」回去，表现为会话忽有忽无；
    而云端归属不同步则会让会话在云端仍记在旧账号名下。

    改动三处：
      ① 档案库 owner_uid（本工具内的权威归属）
      ② 客户端 sessions.user_id（由 activate_for 完成，这里只统计）
      ③ edge-sync 映射的 msg_channel（云端归属）
    """
    report = AdoptReport()
    if not source_uid or not target_uid or source_uid == target_uid:
        return report

    arch = _conn()
    try:
        sids = [
            str(r[0])
            for r in arch.execute(
                "SELECT session_id FROM session_archive WHERE owner_uid = ?",
                (source_uid,),
            ).fetchall()
        ]
        if not sids:
            return report
        report.adopted = len(sids)

        # 正文是否都在（缺了不影响归属，但会话点不开，要如实统计）
        try:
            for sid in sids:
                if paths.find_session_body(sid) is not None:
                    report.bodies_found += 1
                else:
                    report.bodies_missing += 1
        except Exception:
            pass

        if dry_run:
            return report

        arch.executemany(
            "UPDATE session_archive SET owner_uid = ? WHERE session_id = ?",
            [(target_uid, sid) for sid in sids],
        )
        arch.commit()
    finally:
        arch.close()

    # 第三件：云端归属
    edge_rows, db_missing, db_busy = _update_edge_sync(sids, target_uid)
    report.edge_rows = edge_rows
    report.edge_db_missing = db_missing
    report.edge_db_busy = db_busy
    return report


# --------------------------------------------------------------------------
# 会话复制（真正的「共享」）
# --------------------------------------------------------------------------
#
# 归属转移（adopt）是移动语义：转给 B 之后 A 就空了。要「两边都能用」，
# 唯一办法是给目标账号造一份**独立副本**：
#
#   ① 正文：projects/{工作区}/{新id}.jsonl —— 内容里的旧 id 全部替换成新 id
#     （正文里 sessionId 等字段引用旧 id，不替换会话打不开）；
#   ② 客户端索引：sessions 表插入新行（新 id + 目标 user_id，其余字段照抄）；
#   ③ 云端映射：edge_sync_mapping 注册新 id → convmsg:{目标 uid}；
#   ④ 附属文件：.meta.json / .file-rollback.ndjson / workspace/sessions/{id}/
#      有就一并带上；
#   ⑤ 档案库：新会话登记 owner=目标（这样换号激活时它跟着目标走）。
#
# 关键防重复机制：copy_lineage 表记录 (源会话, 目标账号) → 已有副本。
# 参考实现的教训：没有这层映射，重复执行会复制出一堆重复会话。
#
# 复制前自动全量备份；客户端必须退出（要写它独占的数据库）。


@dataclass
class CopyReport:
    source_uid: str = ""
    target_uid: str = ""
    planned: int = 0           # 源账号名下的会话总数
    would_copy: int = 0        # 演练：将要复制的条数
    copied: int = 0
    skipped_exists: int = 0    # 已经复制过（lineage 命中）
    skipped_deleted: int = 0   # 源会话已被删除
    missing_body: int = 0      # 缺正文文件（列表条目没了正文，复制出来也打不开）
    failed: int = 0
    backup_tag: str = ""
    edge_registered: int = 0
    edge_missing_db: bool = False
    dry_run: bool = False
    errors: list = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors and self.failed == 0

    def text(self) -> str:
        if self.dry_run:
            return (f"将复制 {self.would_copy} 条（已复制过 {self.skipped_exists}、"
                    f"缺正文 {self.missing_body}、已删除 {self.skipped_deleted}）")
        bits = [f"新建 {self.copied}"]
        if self.skipped_exists:
            bits.append(f"已存在 {self.skipped_exists}")
        if self.missing_body:
            bits.append(f"缺正文 {self.missing_body}")
        if self.skipped_deleted:
            bits.append(f"已删除 {self.skipped_deleted}")
        if self.failed:
            bits.append(f"失败 {self.failed}")
        return " · ".join(bits)


def _replace_id_in(src: Path, dest: Path, old: str, new: str) -> bool:
    """把文本文件里的旧会话 id 全部替换成新 id 后写到 dest。

    用字节替换：会话 id 是 ASCII UUID，字节级替换不涉及编解码歧义，
    对几十 MB 的正文也省内存。
    """
    try:
        data = src.read_bytes()
        data = data.replace(old.encode("ascii"), new.encode("ascii"))
        dest.write_bytes(data)
        return True
    except OSError:
        return False


def _copy_ws_dir(sid: str, new_sid: str) -> bool:
    """带上 workspace/sessions/{id}/ 工作区目录（有就复制，改名不改内容）。"""
    base = paths.workbuddy_dir() / "workspace" / "sessions"
    src = base / sid
    if not src.is_dir():
        return False
    dst = base / new_sid
    if dst.exists():
        return False
    try:
        import shutil

        shutil.copytree(src, dst)
        return True
    except OSError:
        return False


def copy_sessions(source_uid: str, target_uid: str | None = None, *,
                  dry_run: bool = False, on_progress=None) -> CopyReport:
    """把 source 账号名下的会话复制一份给 target（默认当前登录账号）。

    与 adopt（划归/移动）的区别：源账号的会话**原样保留**，目标拿到的是
    新 id 的独立副本 —— 两边各有一份，互不影响，这是真正的「共享」。
    """
    from . import client as client_mod, engine, profiles

    report = CopyReport(source_uid=source_uid, dry_run=dry_run)
    if not source_uid:
        report.errors.append("源账号 uid 为空")
        return report
    if not target_uid:
        target_uid = profiles.read_login_uid() or profiles.live_uid()
    report.target_uid = target_uid or ""
    if not report.target_uid:
        report.errors.append("无法确定目标账号（当前没有登录态）")
        return report
    if source_uid == report.target_uid:
        report.errors.append("源账号与目标账号相同")
        return report

    # 复制要写客户端数据库与云端映射，两者都被运行中的客户端独占
    if client_mod.is_running():
        report.errors.append(
            "WorkBuddy 正在运行：复制需要写会话数据库与云端映射，请先结束客户端"
        )
        return report

    # 最新状态先收进档案
    capture()
    sids = [sid for sid, o in owners().items() if o == source_uid]
    report.planned = len(sids)
    if not sids:
        report.errors.append("源账号名下没有已归档的会话")
        return report

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # 谱系：已复制过的 (源会话, 目标) 不再重复复制
    arch = _conn()
    try:
        existing = {
            (str(r[0]), str(r[1]))
            for r in arch.execute(
                "SELECT source_sid, target_uid FROM copy_lineage"
            ).fetchall()
        }
    except sqlite3.Error:
        existing = set()

    live = _live_conn()
    edge_conn = None
    try:
        cols = [r[1] for r in live.execute("PRAGMA table_info(sessions)")]
        # schema 按列数查表（30 列 = 5.5.x / 40 列 = 5.6.x），列名序列也要一致；
        # 不认识就拒绝复制 —— 宁可不做也不写坏客户端数据库
        n_cols = _match_sessions_schema(cols)
        if n_cols is None:
            report.errors.append(
                "客户端 sessions 表 schema 未识别（"
                + str(len(cols))
                + " 列）—— 可能客户端又改了表结构，请反馈"
            )
            return report

        edge_db = paths.edge_sync_db_path()
        report.edge_missing_db = edge_db is None
        if edge_db is not None:
            try:
                edge_conn = sqlite3.connect(str(edge_db))
                edge_conn.execute("PRAGMA busy_timeout=8000")
                # 库文件在但表不在（如全新/被清空的映射库）视同缺失
                has_table = edge_conn.execute(
                    "SELECT 1 FROM sqlite_master"
                    " WHERE type='table' AND name='edge_sync_mapping'"
                ).fetchone()
                if not has_table:
                    edge_conn.close()
                    edge_conn = None
                    report.edge_missing_db = True
            except sqlite3.Error:
                edge_conn = None
                report.edge_missing_db = True

        # ---- 第一遍（只读）：逐条判定，攒出可复制清单 ----
        jobs: list[dict] = []
        for i, sid in enumerate(sids, 1):
            if on_progress:
                on_progress(i, len(sids), sid)
            if (sid, report.target_uid) in existing:
                report.skipped_exists += 1
                continue
            row = live.execute(
                "SELECT * FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if row is None:
                report.failed += 1
                report.errors.append(f"{sid[:8]}: 客户端数据库里没有这条会话")
                continue
            rowd = dict(zip(cols, row))
            if rowd.get("deleted_at"):
                report.skipped_deleted += 1
                continue
            body = paths.find_session_body(sid)
            if body is None:
                report.missing_body += 1
                continue
            jobs.append({"sid": sid, "row": rowd, "body": body})

        report.would_copy = len(jobs)
        if dry_run:
            return report
        if not jobs:
            return report

        # ---- 备份：即将写客户端数据库，先留全量退路 ----
        info = engine.create_backup(
            report.target_uid, source_uid, "before-copy")
        report.backup_tag = info.tag

        # ---- 第二遍：写入 ----
        for job in jobs:
            sid = job["sid"]
            rowd = job["row"]
            body = job["body"]
            new_sid = str(uuid.uuid4())

            # ① 正文（内容里的旧 id 全部替换）
            if not _replace_id_in(body, body.parent / f"{new_sid}.jsonl", sid, new_sid):
                report.failed += 1
                report.errors.append(f"{sid[:8]}: 正文复制失败")
                continue

            # ② 附属文件
            for suffix in (".meta.json", ".file-rollback.ndjson"):
                aux = body.parent / f"{sid}{suffix}"
                if aux.is_file():
                    _replace_id_in(aux, aux.parent / f"{new_sid}{suffix}", sid, new_sid)

            # ③ 工作区目录
            _copy_ws_dir(sid, new_sid)

            # ④ 客户端索引：新行（新 id + 目标 user_id，其余字段原样）
            rowd["id"] = new_sid
            rowd["user_id"] = report.target_uid
            try:
                if n_cols == 30:
                    live.execute(
                        "INSERT OR IGNORE INTO sessions VALUES ("
                        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "?,?,?,?,?,?,?,?,?,?)",
                        [rowd.get(c) for c in cols],
                    )
                else:  # 40 列（客户端 5.6.x）
                    live.execute(
                        "INSERT OR IGNORE INTO sessions VALUES ("
                        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [rowd.get(c) for c in cols],
                    )
            except sqlite3.Error as e:
                report.failed += 1
                report.errors.append(f"{sid[:8]}: 写入会话索引失败（{e}）")
                continue

            # ⑤ 云端映射
            if edge_conn is not None:
                try:
                    edge_conn.execute(
                        "INSERT OR REPLACE INTO edge_sync_mapping"
                        "(session_id, conversation_id, msg_channel, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (new_sid, new_sid, f"convmsg:{report.target_uid}",
                         int(time.time() * 1000)),
                    )
                    report.edge_registered += 1
                except sqlite3.Error as e:
                    report.errors.append(f"{new_sid[:8]}: 云端映射注册失败（{e}）")

            # ⑥⑦ 档案登记 + 谱系
            payload = json.dumps(rowd, ensure_ascii=False, default=str)
            arch.execute(
                "INSERT INTO session_archive"
                "(session_id, owner_uid, payload, first_seen, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " payload = excluded.payload, updated_at = excluded.updated_at",
                (new_sid, report.target_uid, payload, stamp, stamp),
            )
            arch.execute(
                "INSERT INTO copy_lineage"
                "(copy_id, source_sid, source_uid, target_uid, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (new_sid, sid, source_uid, report.target_uid, stamp),
            )
            report.copied += 1

        live.commit()
        arch.commit()
        if edge_conn is not None:
            edge_conn.commit()
        try:
            live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        return report
    finally:
        try:
            live.close()
        except Exception:
            pass
        try:
            arch.close()
        except Exception:
            pass
        if edge_conn is not None:
            try:
                edge_conn.close()
            except Exception:
                pass


def sync_cloud(*, dry_run: bool = False) -> dict:
    """把云端归属映射修正为与档案库 owner 一致。

    专治「本地两处一致、云端还写着旧账号」—— 这种不一致漂移检测以前
    查不到，客户端会在云端继续按旧账号同步那些会话。
    """
    owner_map = owners()
    groups: dict[str, list[str]] = {}
    for sid, o in owner_map.items():
        if o:
            groups.setdefault(o, []).append(sid)

    edge_db = paths.edge_sync_db_path()
    out = {"dry_run": dry_run, "would_fix": 0, "fixed": 0,
           "missing_db": edge_db is None, "busy": False}
    if edge_db is None:
        return out

    # 现状统计（顺带作为演练结果）
    try:
        conn = sqlite3.connect(str(edge_db))
        try:
            mapping = {
                str(r[0]): str(r[1])
                for r in conn.execute(
                    "SELECT session_id, msg_channel FROM edge_sync_mapping"
                ).fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error:
        out["busy"] = True
        return out

    for uid, sids in groups.items():
        channel = f"convmsg:{uid}"
        out["would_fix"] += sum(
            1 for s in sids
            if s in mapping and mapping[s] != channel
        )

    if dry_run:
        return out

    for uid, sids in groups.items():
        n, miss, busy = _update_edge_sync(sids, uid)
        out["fixed"] += n
        out["missing_db"] = out["missing_db"] or miss
        out["busy"] = out["busy"] or busy
    return out

