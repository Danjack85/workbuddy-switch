#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""沙箱自检：在真实数据的副本上跑完整的 备份 → 同步 → 校验 → 回滚 流程。

绝不触碰真实数据：先把 WorkBuddy 数据根复制到临时目录，再用环境变量
WBSWITCH_WORKBUDDY_HOME / WBSWITCH_STORE 指向副本。

    python tests/selftest.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REAL_WB = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".workbuddy-ai"

#: 沙箱里伪造的"另一个账号"。
#: 必须是**合成的固定 uid**，不能用真实数据里出现过的 —— 否则一旦真实环境
#: 的账号归属变化（比如用户真的换了号），"旧账号"就可能和"当前账号"撞成同一个，
#: 自检会莫名其妙地失败。这里用一个不会与真实数据冲突的常量。
SANDBOX_OTHER_UID = "5a5a5a5a-1111-2222-3333-444444444444"

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))


def fake_session(uid: str, nickname: str) -> str:
    """构造一份结构与真实客户端一致的登录会话文件。

    只保留本工具用到的字段：account.uid 与 auth 段。
    """
    return json.dumps(
        {
            "account": {"uid": uid, "nickname": nickname, "type": "personal"},
            "auth": {
                "accessToken": f"FAKE-ACCESS-{uid[:8]}",
                "refreshToken": f"FAKE-REFRESH-{uid[:8]}",
                "tokenType": "Bearer",
                "expiresAt": 1821100895270,
            },
            "accounts": [{"uid": uid, "nickname": nickname}],
            "allAccounts": [{"uid": uid, "nickname": nickname}],
        },
        ensure_ascii=False,
        indent=2,
    )


# --------------------------------------------------------------------------
# 搭建沙箱
# --------------------------------------------------------------------------


def mem_template(uid: str, block: str, updated: str) -> str:
    """构造一个合法的 WorkBuddy 记忆文件。"""
    raw = json.dumps(
        {"uid": uid, "memoryBlock": block, "updatedAt": updated, "version": 0},
        ensure_ascii=False,
        indent=2,
    )
    parts = [
        "# User Memory Profile",
        f"> Last updated: {updated}",
        "> Version: 0",
        "",
        "## Memory Block",
        "",
        block,
        "",
        "---",
        "",
        "<!-- RAW_JSON_START",
        raw,
        "RAW_JSON_END -->",
        "",
    ]
    return "\n".join(parts)


def build_sandbox(root: Path) -> Path:
    """复制真实数据根的必要部分到沙箱，并造出"两个账号各有数据"的初始状态。"""
    wb = root / "workbuddy-ai"
    wb.mkdir(parents=True, exist_ok=True)

    for name in ("memory", "connectors", "storage", "tasks"):
        src = REAL_WB / name
        if src.exists():
            shutil.copytree(src, wb / name, dirs_exist_ok=True)

    for suffix in ("", "-wal", "-shm"):
        src = Path(str(REAL_WB / "workbuddy.db") + suffix)
        if src.exists():
            shutil.copy2(src, wb / src.name)

    # settings.json（含 claw.users 这种按账号隔离的段落）
    cfg = REAL_WB / "settings.json"
    if cfg.exists():
        shutil.copy2(cfg, wb / cfg.name)

    # 登录态：沙箱里造一份"当前账号"的会话文件。
    # 真实位置在扩展数据目录（沙箱里用环境变量指向临时目录）。

    # 把一部分会话改到"另一个账号"名下，模拟切换账号后的状态
    conn = sqlite3.connect(str(wb / "workbuddy.db"))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    uids = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL"
        ).fetchall()
    ]
    check("沙箱数据库至少有 1 个 uid", len(uids) >= 1, str(uids))
    cur_uid = uids[0]
    old_uid = SANDBOX_OTHER_UID
    if cur_uid == old_uid:
        # 理论上不会发生（合成 uid 不会出现在真实数据里），留个兜底
        cur_uid = uids[1] if len(uids) > 1 else cur_uid

    # 让沙箱自洽：身份快照也指向 cur_uid，避免依赖真实数据的当前登录态
    snap = wb / "storage" / "skeleton" / "account-snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        json.dumps(
            {
                "primary": {
                    "version": 1,
                    "uid": cur_uid,
                    "nickname": "current@example.com",
                    "type": "personal",
                    "editionType": "free",
                    "isPro": False,
                    "isAdmin": False,
                    "oneidAccountId": "",
                    "savedAt": 1789566843523,
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 登录会话文件：结构与真实客户端一致（account.uid + auth 段）
    auth = Path(os.environ["WBSWITCH_AUTH_DIR"])
    auth.mkdir(parents=True, exist_ok=True)
    (auth / "workbuddy-desktop-ai.info").write_text(
        fake_session(cur_uid, "current@example.com"), encoding="utf-8"
    )

    # 移 3 条会话到"另一个账号"（不够 3 条就全移，main 里按实际数量断言）
    limit = min(3, conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (cur_uid,)
    ).fetchone()[0])
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? ORDER BY created_at LIMIT ?",
        (cur_uid, limit),
    ).fetchall()]
    for sid in ids:
        conn.execute("UPDATE sessions SET user_id = ? WHERE id = ?", (old_uid, sid))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    if limit < 3:
        print(f"  [note] 当前账号只有 {limit} 条会话，按 {limit} 条做迁移断言")

    # 给旧账号写一段真实记忆内容，给当前账号留空模板
    mem_old = wb / "memory" / f"{old_uid}_memory.md"
    mem_cur = wb / "memory" / f"{cur_uid}_memory.md"
    mem_old.parent.mkdir(parents=True, exist_ok=True)
    mem_old.write_text(
        mem_template(
            old_uid,
            "- 用户偏好中文回复\n- 主力项目在 I 盘",
            "2026-09-01T00:00:00.000Z",
        ),
        encoding="utf-8",
    )
    mem_cur.write_text(
        mem_template(cur_uid, "", "2026-09-16T00:00:00.000Z"),
        encoding="utf-8",
    )

    # 连接器：给旧账号加一个当前账号没有的 server，验证深度合并
    c_old = wb / "connectors" / old_uid
    c_cur = wb / "connectors" / cur_uid
    c_old.mkdir(parents=True, exist_ok=True)
    c_cur.mkdir(parents=True, exist_ok=True)
    (c_old / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "legacy-only": {"command": "npx", "args": ["-y", "legacy-mcp"]},
                    "shared": {"command": "old-cmd"},
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (c_cur / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"shared": {"command": "new-cmd"}}}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    (c_old / ".master.key").write_bytes(b"SHOULD-NEVER-BE-COPIED")

    # settings.json 里给旧账号配一段消息渠道，给当前账号留空：验证合并
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data.setdefault("claw", {}).setdefault("users", {})[old_uid] = {
            "channels": {"wechatmp": {"enabled": True, "connectionMode": "webhook"}}
        }
        data["claw"]["users"].pop(cur_uid, None)
        (wb / "settings.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return wb


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    if not (REAL_WB / "workbuddy.db").exists():
        print(f"找不到真实数据根：{REAL_WB}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="wbs-selftest-"))
    store = tmp / "store"
    os.environ["WBSWITCH_WORKBUDDY_HOME"] = str(tmp / "workbuddy-ai")
    os.environ["WBSWITCH_STORE"] = str(store)
    os.environ["WBSWITCH_SANDBOX"] = "1"  # 禁止真实进程操作
    # 登录态在扩展数据目录，不在数据根下，单独指到沙箱里
    os.environ["WBSWITCH_AUTH_DIR"] = str(tmp / "ext" / "Data" / "Public" / "auth")

    print(f"沙箱: {tmp}")
    print("=" * 74)
    print("1. 搭建沙箱数据")
    print("=" * 74)
    build_sandbox(tmp)

    from wbswitch import client, config, engine, i18n, paths, profiles, sessions, switcher

    from wbswitch.console import make_console_safe

    make_console_safe()

    i18n.set_lang("zh")
    paths.refresh()

    check("数据根指向沙箱", str(paths.workbuddy_dir()) == str(tmp / "workbuddy-ai"),
          str(paths.workbuddy_dir()))
    check("档案库指向沙箱", str(paths.store_dir()) == str(store))

    old_uid = SANDBOX_OTHER_UID
    cur_uid = profiles.live_uid()
    check("读到当前登录 uid", bool(cur_uid), cur_uid)
    check("当前账号与沙箱另一账号不同", cur_uid != old_uid, f"{cur_uid} vs {old_uid}")

    counts = engine.session_counts()
    moved = counts.get(old_uid, 0)
    check("会话已分到两个账号名下",
          moved > 0 and counts.get(cur_uid, 0) >= 0, str(counts))
    check("另一账号拿到了迁移样本", moved == 3, f"moved={moved}")
    print()

    print("=" * 74)
    print("2. 保全当前登录（capture）")
    print("=" * 74)
    cur_acc = profiles.capture_current()
    check("档案已创建", cur_acc.id and cur_acc.uid == cur_uid, cur_acc.name)
    check("档案有统计信息", cur_acc.stats.sessions > 0, f"sessions={cur_acc.stats.sessions}")
    check("私有存储已快照", cur_acc.private_dir().exists() or not paths.user_storage_dir(cur_uid).exists())

    old_acc = profiles.capture_from_identity(
        {
            "uid": old_uid,
            "nickname": "legacy@example.com",
            "type": "personal",
            "editionType": "free",
        }
    )
    check("旧账号档案已创建", old_acc.uid == old_uid, old_acc.name)
    print()

    print("=" * 74)
    print("3. 一键同步：旧账号 → 当前账号")
    print("=" * 74)
    before = engine.session_counts()
    rep = engine.sync(old_uid, cur_uid, do_backup=True, label="selftest")

    check("备份已创建", bool(rep.backup_tag), rep.backup_tag)
    s = rep.find("sessions")
    check("会话迁移 3 条", s is not None and s.changed == 3, str(s))
    check("源账号会话归零", engine.session_counts().get(old_uid, 0) == 0)
    check("目标账号会话 = 原数 + 3",
          engine.session_counts().get(cur_uid, 0) == before.get(cur_uid, 0) + 3,
          f"{before.get(cur_uid,0)} -> {engine.session_counts().get(cur_uid,0)}")

    m = rep.find("memory")
    check("记忆合并了 2 行", m is not None and m.changed == 2, str(m))

    mem_text = paths.memory_file(cur_uid).read_text(encoding="utf-8")
    check("记忆正文包含旧账号内容", "用户偏好中文回复" in mem_text)
    check("未写入元数据碎片（无裸 uid 行）", '"uid":' not in mem_text.split("RAW_JSON_START")[0])
    check("RAW_JSON 段 uid 已改为目标账号", f'"uid": "{cur_uid}"' in mem_text)

    c = rep.find("connectors")
    check("连接器合并了 1 个 key", c is not None and c.changed == 1, str(c))
    merged_mcp = json.loads((paths.connector_dir(cur_uid) / "mcp.json").read_text(encoding="utf-8"))
    servers = merged_mcp.get("mcpServers", {})
    check("新增 legacy-only", "legacy-only" in servers)
    check("已存在的 shared 未被覆盖", servers.get("shared", {}).get("command") == "new-cmd",
          str(servers.get("shared")))
    check("未复制 .master.key",
          not (paths.connector_dir(cur_uid) / ".master.key").exists()
          or (paths.connector_dir(cur_uid) / ".master.key").read_bytes() != b"SHOULD-NEVER-BE-COPIED")

    check("数据库完整性 ok", engine.integrity_check() == "ok", engine.integrity_check())

    st = rep.find("settings")
    check("账号设置已补齐", st is not None and st.changed == 1, str(st))
    cfg_data = json.loads((paths.workbuddy_dir() / "settings.json").read_text(encoding="utf-8"))
    check("目标账号已获得 claw.users 段落",
          cur_uid in cfg_data.get("claw", {}).get("users", {}))
    print()

    print("=" * 74)
    print("4. 幂等性：再同步一次不应产生变化")
    print("=" * 74)
    rep2 = engine.sync(old_uid, cur_uid, do_backup=False, label="selftest-again")
    check("无新会话可迁", (rep2.find("sessions") or {}).changed == 0)
    check("无新记忆可并", (rep2.find("memory") or {}).changed == 0)
    check("无新连接器可并", (rep2.find("connectors") or {}).changed == 0)
    check("无新设置可补", (rep2.find("settings") or {}).changed == 0)
    print()

    print("=" * 74)
    print("5. 演练模式不写入")
    print("=" * 74)
    dry = engine.sync(old_uid, cur_uid, dry_run=True)
    check("dry-run 标记正确", dry.dry_run is True)
    check("dry-run 不产生备份", dry.backup_tag == "")
    print()

    print("=" * 74)
    print("6. 回滚")
    print("=" * 74)
    tag = rep.backup_tag
    n_backups_before = len(engine.list_backups())
    restored = engine.restore_backup(tag)
    check("回滚执行成功", "workbuddy.db" in restored, str(restored))
    check("回滚后旧账号会话恢复", engine.session_counts().get(old_uid, 0) == 3,
          str(engine.session_counts()))
    check("回滚后数据库完整性 ok", engine.integrity_check() == "ok")
    # 回归：回滚前的自动备份不能和被回滚的备份撞目录，否则回滚等于空操作
    check("回滚未覆盖原备份（tag 不再碰撞）",
          engine.find_backup(tag) is not None
          and len(engine.list_backups()) == n_backups_before + 1,
          f"{n_backups_before} -> {len(engine.list_backups())}")
    print()

    print("=" * 74)
    print("7. 一键换号（dry-run，沙箱内）")
    print("=" * 74)
    s = config.load()
    s.dry_run = True
    s.save()
    res = switcher.switch_to(old_acc.id, force=True)
    check("换号返回 switched", res.switched is True, f"already={res.already_active}")
    check("换号标记 dry-run", res.dry_run is True)
    check("dry-run 未改动身份", profiles.live_uid() == cur_uid, profiles.live_uid())

    s.dry_run = False
    s.save()
    print()

    print("=" * 74)
    print("8. 真实换号（沙箱内，进程操作已禁用）")
    print("=" * 74)
    # 给目标账号（旧号）预置一份登录态快照，模拟"它上次登录时被保全过"。
    # 没有快照的话，换号只能做到数据就位、需要重新登录——这也是一种合法状态。
    old_acc.session_dir().mkdir(parents=True, exist_ok=True)
    (old_acc.session_dir() / "workbuddy-desktop-ai.info").write_text(
        fake_session(old_uid, "legacy@example.com"), encoding="utf-8"
    )
    old_acc.has_login_state = True
    profiles.save_account(old_acc)

    auth_file = Path(os.environ["WBSWITCH_AUTH_DIR"]) / "workbuddy-desktop-ai.info"
    check("换号前登录态是当前账号",
          profiles.read_login_uid() == cur_uid, profiles.read_login_uid())

    res = switcher.switch_to(old_acc.id, force=True, restart=False)
    check("换号成功", res.switched is True)
    check("身份已写为目标账号", profiles.live_uid() == old_uid, profiles.live_uid())
    # 原登录在步骤 2 已入库，因此这里不会再新建档案；两种情况都算「未丢号」
    check(
        "原登录未丢失（已入库或自动保全）",
        bool(res.preserved_as) or profiles.find_by_uid(cur_uid) is not None,
        f"preserved_as={res.preserved_as!r} registered={profiles.find_by_uid(cur_uid) is not None}",
    )
    check("换号前自动备份", bool(res.backup_tag), res.backup_tag)
    state = switcher.build_state()
    check("状态里 active 指向目标账号", state["active_account_id"] == old_acc.id)

    # 凭据级换号：登录文件应被换成目标账号那一份
    check("换号时执行了凭据切换", res.login_state is True, str(res.warnings))
    check("登录文件里的账号已变为目标账号",
          profiles.read_login_uid() == old_uid, profiles.read_login_uid())
    check("凭据文件存在且可读", auth_file.exists())

    # 切走之前，原账号的登录态应被存进档案，否则切回去要重新登录
    check("换号时保全了原账号登录态", res.preserved_login is True, str(res.warnings))
    cur_acc_after = profiles.find_by_uid(cur_uid)
    check("原账号登录态快照内容正确",
          cur_acc_after is not None
          and cur_acc_after.has_login_state
          and cur_uid in (cur_acc_after.session_dir() / "workbuddy-desktop-ai.info").read_text("utf-8"))
    print()

    print("=" * 74)
    print("8b. 切回原账号：凭据应被还原")
    print("=" * 74)
    cur_acc = profiles.find_by_uid(cur_uid)
    check("切回前档案里有可用快照",
          cur_acc is not None and cur_acc.has_login_state)
    n = profiles.restore_login_state(cur_acc) if cur_acc is not None else 0
    check("还原登录态写入成功", n > 0, f"restored={n}")
    check("登录文件已还原为原账号",
          profiles.read_login_uid() == cur_uid, profiles.read_login_uid())
    print()

    print("=" * 74)
    print("9. 会话档案库：两账号各有会话，来回切换互不丢失")
    print("=" * 74)
    # 这是本工具的核心能力：会话按 owner 长期留存在本地，
    # 登录哪个账号就自动恢复哪个账号的会话，来回切换双向无损。
    # 先给"旧账号"造 2 条自己的会话（模拟它原本就有历史）。
    conn = sqlite3.connect(str(paths.db_path()))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    base = conn.execute("SELECT * FROM sessions LIMIT 1").fetchone()
    if base:
        tpl = dict(zip(cols, base))
        for i in (1, 2):
            row = dict(tpl)
            row["id"] = f"LEGACY-{i:04d}"
            row["user_id"] = old_uid
            row["title"] = f"旧账号会话 {i}"
            row["created_at"] = int(tpl["created_at"]) + i * 100
            conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES ("
                + ",".join(["?"] * len(cols)) + ")",
                [row.get(k) for k in cols],
            )
        conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    # 把两个账号都建档，并把现有会话收进档案库
    acc_a = profiles.find_by_uid(cur_uid) or profiles.capture_current()
    cap = sessions.capture()
    owner_map = sessions.owners()
    check("档案库记录了会话归属", len(owner_map) > 0, f"{len(owner_map)} 条")
    check("两个账号的会话都已归档",
          cur_uid in set(owner_map.values()) and old_uid in set(owner_map.values()),
          str({k[:8]: v for k, v in sessions.stats().by_owner.items()}))
    check("归档时保留了各自的 owner（未串号）",
          all(o in (cur_uid, old_uid) for o in owner_map.values()))

    def visible(uid: str) -> list[str]:
        c = sqlite3.connect(str(paths.db_path()))
        r = [x[0] for x in c.execute(
            "SELECT title FROM sessions WHERE user_id=? ORDER BY created_at", (uid,)
        ).fetchall()]
        c.close()
        return r

    a_before = visible(cur_uid)
    b_before = visible(old_uid)
    check("当前账号切换前可见会话", len(a_before) > 0, f"{len(a_before)} 条")

    # 切到旧账号：应看到旧账号自己的会话
    r_a2b = switcher.switch_to(old_acc.id, force=True, restart=False)
    b_after = visible(old_uid)
    check("切到旧账号后能看到它自己的会话", b_after == b_before,
          f"before={b_before} after={b_after}")
    check("切换报告含会话数", r_a2b.sessions_visible > 0, str(r_a2b.sessions_visible))

    # 切回：当前账号的会话必须原样回来
    r_b2a = switcher.switch_to(acc_a.id, force=True, restart=False)
    a_after = visible(cur_uid)
    check("切回后当前账号会话原样恢复", a_after == a_before,
          f"before={len(a_before)} after={len(a_after)}")
    check("来回切换后旧账号会话仍留存", len(visible(old_uid)) == len(b_before),
          f"{len(visible(old_uid))} vs {len(b_before)}")

    # 再来回一次，确认稳定（不因反复切换而漂移或丢数据）
    switcher.switch_to(old_acc.id, force=True, restart=False)
    switcher.switch_to(acc_a.id, force=True, restart=False)
    check("反复切换后会话仍完整", visible(cur_uid) == a_before,
          f"{len(visible(cur_uid))} 条")
    chk = sessions.verify_roundtrip(cur_uid)
    check("档案库与客户端一致", chk.get("ok") is True, str(chk))

    # 归属检测：此时客户端与档案库应无漂移
    d = sessions.drift()
    check("无归属漂移", d.get("ok") is True, f"{d.get('count')} 条不一致")
    print()

    print("=" * 74)
    print("9b. 归属变更（同步到当前账号）：档案库与客户端要一起改")
    print("=" * 74)
    # 「把旧号数据并到当前号」不能只改客户端 user_id —— 那样档案库 owner 会脱节，
    # 下次激活又被归位回去，会话忽有忽无。正确做法是 adopt_into 一并改归属。
    before_adopt = sessions.stats().owner_count(old_uid)
    adopted = sessions.adopt_into(old_uid, cur_uid)
    check("档案库归属已改到当前账号", adopted == before_adopt, f"adopted={adopted}")
    check("旧账号在档案库里已清零",
          sessions.stats().owner_count(old_uid) == 0,
          str(sessions.stats().by_owner))
    act = sessions.activate_for(cur_uid)
    a_now = visible(cur_uid)
    check("激活后当前账号可见会话增加",
          len(a_now) >= len(a_before) + adopted,
          f"{len(a_before)} -> {len(a_now)} (adopted {adopted})")
    check("归属变更后无漂移", sessions.drift().get("ok") is True,
          str(sessions.drift().get("count")))
    print()

    print("=" * 74)
    print("10. 状态与诊断接口")
    print("=" * 74)
    state = switcher.build_state()
    check("state 含 accounts", isinstance(state["accounts"], list) and len(state["accounts"]) >= 2)
    check("state 含 settings", isinstance(state["settings"], dict))
    check("client_path 已探测到", state["client_path_ok"] is True, state["client_path"])
    diag = paths.diagnostics()
    check("diagnostics 字段齐全", "workbuddy_home" in diag and "db_path" in diag)
    check("history 有记录", len(switcher.read_history(50)) > 0)
    print()

    print("=" * 74)
    print("11. 签到与积分（假上游，不联网）")
    print("=" * 74)
    # 这是本工具唯一会联网的部分，自检里用假上游验证逻辑正确性。
    # 真实签到活动可能未开启，成功路径无法实测，所以这里把各分支都覆盖一遍。
    from wbswitch import billing as billing_mod, upstream as up

    sess = up.Session.from_account(old_acc) or up.Session.from_account(cur_acc)
    check("能从账号档案构造会话", sess is not None and bool(sess.access_token),
          sess.label() if sess else "None")
    check("按 domain 判出版本", sess.edition.id in ("cn", "intl"),
          f"{sess.edition.id} (domain={sess.domain})")
    check("UA 含 CLI 段", "CLI/" in up.user_agent(sess.edition),
          up.user_agent(sess.edition))

    # --- 成功路径 ---
    def ok_transport(url, payload, headers, method):
        if "checkin-activity-status" in url:
            return up.Response(200, 0, "OK", {
                "active": True, "today_checked_in": False, "streak_days": 3,
                "daily_credit": 100, "today_credit": 100,
                "theme_name": "Buddy 加油站", "week_progress": [True] * 3 + [False] * 4,
            })
        if "daily-checkin" in url:
            return up.Response(200, 0, "OK", {"active": True, "today_checked_in": True,
                                              "streak_days": 4, "total_credits": 1300})
        if "get-user-resource" in url:
            return up.Response(200, 0, "OK", {"Response": {"Data": {"Accounts": [
                {"PackageCode": "c1", "PackageName": "每日额度",
                 "CycleCapacityRemainPrecise": 800, "CycleCapacitySizePrecise": 1000},
                {"PackageCode": "c2", "PackageName": "月会员",
                 "CycleCapacityRemainPrecise": 500, "CycleCapacitySizePrecise": 500},
            ]}}})
        return up.Response(404, None, "not found", None)

    b = billing_mod.Billing(up.Client(transport=ok_transport))
    st = b.checkin_status(sess)
    check("解析签到状态", st is not None and st.active and st.streak_days == 3, st.state_text() if st else "")
    check("状态文案正确", st is not None and "100" in st.state_text(), st.state_text() if st else "")
    cr = b.claim(sess)
    check("领取成功", cr.success is True, cr.text())
    cs = b.credits(sess)
    check("个人额度汇总", cs.remain == 1300 and cs.total == 1500, cs.text())
    check("积分包解析", len(cs.packs) == 2, str([p.text() for p in cs.packs]))

    # --- 幂等：今天已领 ---
    b2 = billing_mod.Billing(up.Client(
        transport=lambda *a: up.Response(200, 40002, "今日已签到", None)))
    r2 = b2.claim(sess)
    check("已签到不报错", r2.already is True and not r2.error, r2.text())

    # --- 活动未开启（真实遇到的情况）---
    b3 = billing_mod.Billing(up.Client(
        transport=lambda *a: up.Response(400, 10001, "签到活动未开启或已过期", None)))
    r3 = b3.claim(sess)
    check("活动未开启识别为业务状态", r3.inactive is True and not r3.error, r3.text())

    # --- 风控退避 ---
    seen = []
    def rl_transport(*a):
        seen.append(1)
        if len(seen) < 3:
            return up.Response(429, 11128, "Illegal API invocation", None)
        return up.Response(200, 0, "OK", {"active": True, "today_checked_in": True})
    saved = up.WAF_RETRY_DELAYS
    up.WAF_RETRY_DELAYS = (0.01, 0.01)
    b4 = billing_mod.Billing(up.Client(transport=rl_transport))
    check("风控自动退避重试", b4.checkin_status(sess) is not None and len(seen) == 3,
          f"尝试 {len(seen)} 次")
    up.WAF_RETRY_DELAYS = saved

    # --- 请求头复刻 ---
    captured = {}
    def cap_transport(url, payload, headers, method):
        captured.update(headers)
        captured["__url"] = url
        return up.Response(200, 0, "OK", {"active": True})
    billing_mod.Billing(up.Client(transport=cap_transport)).checkin_status(sess)
    need = ("Authorization", "X-User-Id", "User-Agent", "X-IDE-Type", "X-IDE-Name",
            "X-Product", "X-Agent-Intent", "X-Request-ID")
    missing_h = [k for k in need if not captured.get(k)]
    check("请求头齐全", not missing_h, f"缺 {missing_h}")
    check("签到 URL 不带 /plugin 前缀",
          "/v2/billing/meter/" in captured.get("__url", "") and "/plugin" not in captured.get("__url", ""),
          captured.get("__url", ""))

    # --- 批量：串行 + 跳过无凭据账号 ---
    class _FakeAcc:
        def __init__(self, name, uid):
            self.name, self.uid = name, uid
        def session_dir(self):
            from pathlib import Path
            return Path(str(tmp / "nonexistent"))

    rep = billing_mod.claim_all([old_acc, cur_acc], client=up.Client(transport=ok_transport))
    check("批量签到逐账号执行", len(rep.results) + len(rep.skipped) >= 1,
          rep.summary())
    check("批量结果摘要可读", "成功" in rep.summary(), rep.summary())
    print()

    print("=" * 74)
    print("12. OpenAI 兼容网关（假上游，不联网）")
    print("=" * 74)
    from wbswitch import gateway as gw_mod

    # --- SSE 聚合：content / reasoning / tool_calls / usage ---
    chunks = [
        {"id": "c1", "model": "m", "created": 1, "choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"reasoning_content": "想"}}]},
        {"choices": [{"delta": {"reasoning_content": "一下"}}]},
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "t1", "type": "function",
             "function": {"name": "get_", "arguments": '{"a"'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "time", "arguments": ":1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 9}},
    ]
    agg = gw_mod.aggregate_chunks(chunks)
    msg = agg["choices"][0]["message"]
    check("聚合 content", msg["content"] == "你好", repr(msg["content"]))
    check("聚合 reasoning_content", msg.get("reasoning_content") == "想一下",
          repr(msg.get("reasoning_content")))
    tc = (msg.get("tool_calls") or [{}])[0]
    check("tool_calls 按 index 合并且参数拼接",
          tc.get("function", {}).get("name") == "get_time"
          and tc.get("function", {}).get("arguments") == '{"a":1}',
          str(tc))
    check("usage 取最后一次", agg.get("usage") == {"total_tokens": 9}, str(agg.get("usage")))
    check("finish_reason 透传", agg["choices"][0]["finish_reason"] == "tool_calls")

    # --- 上游仅支持流式：必须强制 stream=true 并注入 system ---
    prepared = gw_mod.Gateway.prepare_body(
        {"model": "x", "stream": False, "messages": [{"role": "user", "content": "hi"}]}, "m1")
    check("强制 stream=true", prepared.get("stream") is True)
    check("模型被固定为目标模型", prepared.get("model") == "m1")
    check("注入兜底 system 消息", prepared["messages"][0]["role"] == "system",
          str(prepared["messages"][0]))
    keep = gw_mod.Gateway.prepare_body(
        {"messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]}, "m")
    check("已有 system 不重复注入", len(keep["messages"]) == 2, str(len(keep["messages"])))

    # --- 模型目录：非对话模型过滤 + 不静默回退 ---
    cat = gw_mod.ModelCatalog(verbose=lambda *_: None)
    check("内置模型经过滤", all(gw_mod.is_chat_model(m) for m in gw_mod.BUILTIN_MODELS))
    check("过滤嵌入/图像类模型",
          not gw_mod.is_chat_model({"id": "text-embedding-3", "maxOutputTokens": 99999})
          and not gw_mod.is_chat_model({"id": "sora-video", "maxOutputTokens": 99999}))
    check("过滤输出上限过小的模型",
          not gw_mod.is_chat_model({"id": "tiny", "maxOutputTokens": 100}))
    mid, near = cat.resolve("auto")
    check("auto 解析为默认模型", mid == cat.default_id(), f"{mid} / default={cat.default_id()}")
    bad, near2 = cat.resolve("no-such-model-xyz")
    check("未知模型不静默回退", bad is None, f"{bad} 建议={near2}")

    # --- 限额冷却与恢复时间解析 ---
    import time as _t

    rl = gw_mod.RateLimitStore()
    reset = gw_mod.parse_reset_at("您的使用量已超出频率限制，将在 2026-09-11 19:43:46 UTC+8 重置")
    check("从提示文本解析恢复时间", reset > 1_700_000_000, str(reset))
    expect = _t.mktime((2026, 9, 11, 19, 43, 46, 0, 0, -1))
    check("恢复时间约为给定时刻（UTC+8）", abs(reset - expect) < 5, f"{reset} vs {expect}")
    rl.mark("u1", "m1", gw_mod.RateLimit(reset_at=_t.time() + 60))
    check("限额冷却生效", rl.get("u1", "m1") is not None)
    check("冷却按账号×模型维度隔离",
          rl.get("u1", "m2") is None and rl.get("u2", "m1") is None)
    rl.clear("u1", "m1")
    check("成功后清除冷却", rl.get("u1", "m1") is None)
    check("解析不到时间时给保守兜底", gw_mod.parse_reset_at("随便一句话") < _t.time() + 400, "")

    # --- 出站白名单：令牌只会发往官方域 ---
    # 这道校验是硬约束：账号令牌随请求头发出，目标一旦可被外部影响就会泄露。
    allow = [
        "https://copilot.tencent.com/v2/chat/completions",
        "https://www.workbuddy.ai/v2/chat/completions",
    ]
    deny = [
        ("http://copilot.tencent.com/v2/chat", "http 明文"),
        ("https://www.workbuddy.ai.evil.com/x", "仿冒子域名"),
        ("https://evil.com/x", "外部域名"),
        ("https://127.0.0.1:8080/x", "本机服务"),
        ("https://169.254.169.254/latest/meta-data/", "云元数据"),
        ("https://192.168.1.1/admin", "内网地址"),
        ("file:///C:/Windows/win.ini", "file 协议"),
        ("", "空 URL"),
    ]
    allowed_ok = all(
        up.validate_outbound_url(u) == u for u in allow
    )
    check("官方域名放行", allowed_ok, str(allow))
    blocked = []
    for u, why in deny:
        try:
            up.validate_outbound_url(u)
            blocked.append(why)          # 不该通过却通过
        except up.UpstreamError:
            pass
    check("危险目标全部拒绝", not blocked, f"未拦截: {blocked}")

    # --- 网关端到端：假上游 ---
    class _FakeSSE:
        """假的上游响应：逐行吐出 SSE。"""
        def __init__(self, lines):
            self._lines = list(lines)
            self._i = 0
        def readline(self):
            if self._i >= len(self._lines):
                return b""
            line = self._lines[self._i]
            self._i += 1
            return line.encode("utf-8") if isinstance(line, str) else line
        def close(self):
            pass

    def fake_open(session, body):
        parts = []
        for piece in ("你", "好", "呀"):
            parts.append("data: " + json.dumps(
                {"id": "x", "model": body.get("model"),
                 "choices": [{"delta": {"content": piece}}]}, ensure_ascii=False) + "\n\n")
        parts.append("data: " + json.dumps(
            {"id": "x", "choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"total_tokens": 3}}, ensure_ascii=False) + "\n\n")
        parts.append("data: [DONE]\n\n")
        return _FakeSSE(parts)

    gwc = gw_mod.Gateway(gw_mod.GatewayConfig(host="127.0.0.1", port=0))
    gwc.ensure_catalog = lambda: None          # 不联网
    gwc._open_upstream = fake_open             # 假上游

    out, err = gwc.complete({"model": "auto",
                             "messages": [{"role": "user", "content": "hi"}]})
    check("网关非流式聚合成功", err is None and out is not None,
          json.dumps(err, ensure_ascii=False)[:90] if err else "")
    if out:
        check("网关返回内容正确",
              out["choices"][0]["message"]["content"] == "你好呀",
              repr(out["choices"][0]["message"]["content"]))
        check("网关返回 usage", out.get("usage") == {"total_tokens": 3}, str(out.get("usage")))

    kinds, pieces = [], []
    for k, payload in gwc.stream({"model": "auto", "stream": True,
                                  "messages": [{"role": "user", "content": "hi"}]}):
        kinds.append(k)
        if k == "chunk":
            d = (payload.get("choices") or [{}])[0].get("delta") or {}
            if d.get("content"):
                pieces.append(d["content"])
    check("网关流式产出 chunk", kinds.count("chunk") >= 3, str(kinds))
    check("流式内容完整", "".join(pieces) == "你好呀", repr("".join(pieces)))
    check("流式以 done 收尾", kinds and kinds[-1] == "done", str(kinds[-1:]))

    _o, e2 = gwc.complete({"model": "nope-xyz", "messages": [{"role": "user", "content": "x"}]})
    check("网关拒绝未知模型", e2 is not None and e2["error"]["type"] == "model_not_found",
          json.dumps(e2, ensure_ascii=False)[:90] if e2 else "")

    # --- HTTP 层：真起服务，验证路由与鉴权 ---
    # 只连本机回环的临时测试端口；下面显式断言边界，确保不会请求到别处。
    import http.client as _hc
    import threading as _th

    #: 测试只访问本机回环地址
    LOOPBACK = "127.0.0.1"

    def _request(port: int, path: str, method: str = "GET",
                 body: dict | None = None, key: str | None = None):
        """向本机测试端口发一次请求。主机被限定为回环，不涉及外部地址。"""
        if LOOPBACK != "127.0.0.1":       # 防御性断言：杜绝被改造成外发请求
            raise AssertionError("测试仅允许访问本机回环地址")
        conn = _hc.HTTPConnection(LOOPBACK, port, timeout=30)
        try:
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = "Bearer " + key
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw or "{}")
            except Exception:
                return resp.status, {}
        finally:
            conn.close()

    saved_sessions = gw_mod.load_sessions
    gw_mod.load_sessions = lambda: [("fake", sess)]
    try:
        gwc2 = gw_mod.Gateway(gw_mod.GatewayConfig(host="127.0.0.1", port=0, api_key="k1"))
        gwc2.ensure_catalog = lambda: None
        gwc2._open_upstream = fake_open
        srv = gw_mod.GatewayServer(("127.0.0.1", 0), gw_mod.make_handler(gwc2))
        test_port = int(srv.server_address[1])
        _th.Thread(target=srv.serve_forever, daemon=True).start()

        st, d = _request(test_port, "/health")
        check("HTTP /health 可用", st == 200 and d.get("ok") is True, f"{st} {str(d)[:70]}")
        st, _d = _request(test_port, "/v1/models")
        check("无 Key 访问被拒", st == 401, str(st))
        st, d = _request(test_port, "/v1/models", key="k1")
        check("带 Key 可列模型", st == 200 and isinstance(d.get("data"), list), str(st))
        st, d = _request(test_port, "/v1/chat/completions", "POST",
                         {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
                         key="k1")
        check("HTTP 对话成功", st == 200 and d.get("object") == "chat.completion", str(st))
        st, _d = _request(test_port, "/v1/chat/completions", "POST", {"model": "auto"}, key="k1")
        check("缺 messages 返回 400", st == 400, str(st))
        st, _d = _request(test_port, "/v1/nope", key="k1")
        check("未知路径返回 404", st == 404, str(st))
        srv.shutdown()
    finally:
        gw_mod.load_sessions = saved_sessions

    try:
        gw_mod.serve(gw_mod.Gateway(gw_mod.GatewayConfig(host="0.0.0.0", port=1)))
        check("非本机监听必须设 Key", False, "未拦截")
    except RuntimeError as e:
        check("非本机监听必须设 Key", "API Key" in str(e), str(e)[:60])
    except Exception as e:
        check("非本机监听必须设 Key", False, f"{type(e).__name__}: {e}")
    print()

    print("=" * 74)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 74)

    if FAIL == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        print("沙箱已清理")
    else:
        print(f"沙箱保留以便排查：{tmp}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
