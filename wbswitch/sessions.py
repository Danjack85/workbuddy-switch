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
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: sessions 表当前有 30 列（见客户端建表语句）。补写缺失行时按声明顺序填值。
_SESSIONS_COLUMN_COUNT = 30


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
        # 整行按表声明顺序填值，语句是一条固定列数的完整字面量。
        if len(cols_order) == _SESSIONS_COLUMN_COUNT:
            for sid in owner_map:
                if sid in live_ids:
                    continue
                row = _payload_of(sid)
                if not row or not row.get("id"):
                    continue
                values = [row.get(c) for c in cols_order]
                try:
                    live.execute(
                        "INSERT OR IGNORE INTO sessions VALUES ("
                        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                        "?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                    rep.inserted += 1
                except sqlite3.Error:
                    pass
        else:
            rep.detail = (
                "schema drift (" + str(len(cols_order)) + " cols); skipped restore-back"
            )

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

    返回 {ok, drifted: {session_id: (client_uid, owner_uid)}, count}。
    """
    owner_map = owners()
    try:
        live = _live_conn()
    except Exception as e:
        return {"ok": False, "detail": str(e), "drifted": {}, "count": 0}
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
    return {"ok": not out, "drifted": out, "count": len(out)}


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

