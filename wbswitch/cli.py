# -*- coding: utf-8 -*-
"""命令行接口（对齐 zcode-switch 的 --cli 语义，但改成子命令形式，更好用）。

用法示例：
    wbs state                     查看状态与账号列表
    wbs list                      只列账号
    wbs capture --name 工作号      保全当前登录
    wbs switch --id <id> --force   一键换号
    wbs sync --id <id>            把该账号数据同步到当前登录账号
    wbs backups                   列出备份
    wbs rollback --tag <tag>      回滚
    wbs diagnose                  只读诊断
    wbs gui                       打开图形界面
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, client, config, console, engine, i18n, paths, profiles, sessions, switcher


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------


def _out(msg: str = "") -> None:
    print(msg)


def _hr() -> None:
    _out("=" * 74)


class _Envelope:
    """机器可读输出的收集器（`--envelope` 模式）。

    被 Tauri 前端当后端引擎调用时，stdout 必须只有一个 JSON 对象 —— 否则
    调用方要在一堆人类可读文案里捞 JSON，既脆弱又难排查。

    所以这个模式下：命令的所有 print 被临时收进缓冲，`_emit_json` 的载荷
    被记下来，最后统一输出一个信封：

        {"ok": true,  "data": <命令的 --json 载荷>, "warnings": [...]}
        {"ok": false, "error": "<原因>", "data": <若已产出>}
    """

    active = False
    payload = None
    emitted = False

    @classmethod
    def reset(cls) -> None:
        cls.payload = None
        cls.emitted = False


def _emit_json(obj) -> None:
    if _Envelope.active:
        # 信封模式：不发散输出，记下来由 main() 统一封装
        _Envelope.payload = obj
        _Envelope.emitted = True
        return
    _out(json.dumps(obj, ensure_ascii=False, indent=2))


def _resolve_id(prefix: str) -> str:
    """支持用 id 前缀 / 名称 / uid 前缀定位账号。"""
    accounts = profiles.list_accounts()
    if not accounts:
        raise RuntimeError(i18n.t("cli.no_accounts"))
    for a in accounts:
        if a.id == prefix:
            return a.id
    for a in accounts:
        if a.id.startswith(prefix) or a.uid.startswith(prefix):
            return a.id
    lowered = prefix.lower()
    for a in accounts:
        if a.name.lower() == lowered:
            return a.id
    for a in accounts:
        if lowered in a.name.lower():
            return a.id
    raise RuntimeError(i18n.t("err.no_account", id=prefix))


def _account_table(rows: list[dict]) -> None:
    _out(i18n.t("cli.header", idx="#", ed="ED", name="NAME", uid="UID",
                sessions="SESSIONS", memory="MEMORY"))
    _out("  " + "-" * 96)
    for i, a in enumerate(rows, 1):
        st = a.get("stats") or {}
        mem = "-"
        if st.get("memory_bytes"):
            mem = f"{st['memory_bytes'] / 1024:.1f}KB"
        mark = "  ← " + i18n.t("state.current") if a.get("is_active") else ""
        _out(
            i18n.t(
                "cli.header",
                idx=i,
                ed=(a.get("edition_label") or a.get("edition") or "")[:6],
                name=(a.get("name") or "")[:20],
                uid=(a.get("uid") or "")[:34],
                sessions=st.get("sessions", 0),
                memory=mem,
            )
            + mark
        )


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------


def cmd_state(args) -> int:
    state = switcher.build_state()
    if args.json:
        _emit_json(state)
        return 0

    _hr()
    _out(f"{i18n.t('app.name')} v{__version__} — {i18n.t('app.tagline')}")
    _hr()
    _out()
    for ed, es in (state.get("editions") or {}).items():
        label = i18n.t(f"ed.{ed}")
        _out(f"  [{label}]")
        if es.get("running"):
            _out(f"    {i18n.t('state.running')}")
        elif es.get("live_logged_in"):
            key = "state.unsaved" if es.get("unsaved_login") else "state.safe"
            _out(f"    {i18n.t(key)}")
        else:
            _out(f"    {i18n.t('state.logged_out')}")
        _out(f"    {i18n.t('cli.current')}: {es.get('live_uid') or i18n.t('common.none')}")
        _out(f"    {i18n.t('state.ed_data')}: {es.get('home')}")
        _out(f"    {i18n.t('set.client_path')}: "
             f"{es.get('client_path') or i18n.t('common.unknown')}"
             f" ({'ok' if es.get('client_path_ok') else 'missing'})")
    _out(f"  store: {state.get('store_dir')}")
    _out()
    if not state.get("accounts"):
        _out("  " + i18n.t("cli.no_accounts"))
    else:
        _account_table(state["accounts"])
    _out()
    _out(f"  {i18n.t('bk.title')}: {state.get('backup_count')}")
    _out()
    return 0


def cmd_list(args) -> int:
    state = switcher.build_state()
    if args.json:
        _emit_json(state["accounts"])
        return 0
    if not state["accounts"]:
        _out(i18n.t("cli.no_accounts"))
        return 0
    _account_table(state["accounts"])
    return 0


def cmd_capture(args) -> int:
    """保全各版本当前登录（未入库时建档）。"""
    created = []
    known = 0
    for ed in paths.edition_ids():
        with paths.use_edition(ed):
            uid = profiles.read_login_uid() or profiles.live_uid()
            if not uid:
                continue
            if profiles.find_by_uid(uid) is not None:
                known += 1
                continue
            acc = profiles.capture_current(args.name, ed)
        created.append(acc)
        switcher.log_history("capture", {"name": acc.name, "uid": acc.uid,
                                         "edition": ed})
    if args.json:
        _emit_json([a.to_dict() for a in created])
        return 0
    if not created:
        _out(i18n.t("cli.capture_none"))
        return 0
    for acc in created:
        _out(i18n.t("res.captured", name=acc.name))
        _out(f"  uid: {acc.uid}")
        _out(f"  id : {acc.id}")
    return 0


def cmd_switch(args) -> int:
    account_id = _resolve_id(args.id)
    settings = config.load()
    touched = False
    if args.dry_run:
        settings.dry_run = True
        touched = True
    if getattr(args, "no_login", False):
        settings.switch_login_state = False
        touched = True

    # 这两个开关是一次性意图，跑完就恢复，不污染用户的持久设置。
    restore: dict[str, object] = {}
    if touched:
        saved = config.load()
        restore = {"dry_run": saved.dry_run, "switch_login_state": saved.switch_login_state}
        settings.save()
    try:
        res = switcher.switch_to(
            account_id,
            force=args.force,
            restart=(False if args.no_restart else (True if args.restart else None)),
            hot=(False if args.no_hot else (True if args.hot else None)),
        )
    finally:
        if restore:
            cur = config.load()
            for k, v in restore.items():
                setattr(cur, k, v)
            cur.save()

    if args.json:
        _emit_json(res.__dict__)
        return 0

    if res.already_active:
        _out(i18n.t("res.already", name=res.name))
        return 0

    _out(i18n.t("res.switched", name=res.name))
    if res.preserved_as:
        _out(f"  preserved: {res.preserved_as}")
    if res.preserved_login:
        _out("  login state saved for the previous account")
    if res.login_state:
        _out("  " + i18n.t("res.credentials"))
    if res.killed:
        _out("  killed: WorkBuddy")
    if res.hot:
        _out("  hot switch")
    if res.dry_run:
        _out("  [dry-run] 未写入任何数据")
    rep = res.report or {}
    for step in rep.get("steps", []):
        mark = "ok " if step.get("ok") else "ERR"
        skipped = " (skipped)" if step.get("skipped") else ""
        _out(f"  [{mark}] {step.get('name')}: {step.get('changed', 0)}{skipped} {step.get('detail', '')}")
    if res.backup_tag:
        _out("  " + i18n.t("res.backup", tag=res.backup_tag))
    if res.launched:
        _out("  WorkBuddy launched")
    for w in res.warnings:
        _out(f"  ! {w}")
    _out()
    _out("  " + i18n.t("res.restart_hint"))
    return 0


def cmd_sync(args) -> int:
    account_id = _resolve_id(args.id)
    rep = switcher.sync_account_to_current(account_id)
    if args.json:
        _emit_json(rep)
        return 0
    src = profiles.load_account(account_id)
    dst = profiles.live_uid()
    if rep.get("dry_run"):
        _out("[dry-run] " + i18n.t("res.synced", src=src.name, dst=dst[:8]))
    else:
        _out(i18n.t("res.synced", src=src.name, dst=dst[:8]))
    for step in rep.get("steps", []):
        mark = "ok " if step.get("ok") else "ERR"
        skipped = " (skipped)" if step.get("skipped") else ""
        _out(f"  [{mark}] {step.get('name')}: {step.get('changed', 0)}{skipped} {step.get('detail', '')}")
    if rep.get("backup_tag"):
        _out("  " + i18n.t("res.backup", tag=rep["backup_tag"]))
    for w in rep.get("warnings", []):
        _out(f"  ! {w}")
    _out()
    _out("  " + i18n.t("res.restart_hint"))
    return 0


def cmd_rename(args) -> int:
    account_id = _resolve_id(args.id)
    acc = profiles.rename_account(account_id, args.name)
    if getattr(args, "json", False):
        _emit_json(acc.to_dict())
        return 0
    _out(i18n.t("res.renamed", name=acc.name))
    return 0


def cmd_delete(args) -> int:
    account_id = _resolve_id(args.id)
    acc = profiles.load_account(account_id)
    if not args.yes and not getattr(args, "json", False):
        _out(i18n.t("wiz.delete_title", name=acc.name))
        _out("  " + i18n.t("wiz.delete_desc"))
        ans = input("  (y/N): ").strip().lower()
        if ans != "y":
            _out("cancelled")
            return 1
    profiles.delete_account(account_id)
    if getattr(args, "json", False):
        _emit_json({"deleted": True, "id": account_id, "name": acc.name})
        return 0
    _out(i18n.t("res.deleted", name=acc.name))
    return 0


def cmd_refresh(args) -> int:
    account_id = _resolve_id(args.id)
    acc = profiles.refresh_from_live(account_id)
    if getattr(args, "json", False):
        _emit_json(acc.to_dict())
        return 0
    _out(f"refreshed: {acc.name} ({acc.stats.sessions} sessions)")
    return 0


def cmd_backup(args) -> int:
    uid = args.uid or profiles.live_uid()
    acc = profiles.find_by_uid(uid) if uid else None
    ed = acc.edition if acc else paths.current_edition()
    info = engine.create_backup(uid, "", args.label or "manual", edition=ed)
    switcher.log_history("backup", {"tag": info.tag, "uid": uid, "edition": ed})
    if getattr(args, "json", False):
        _emit_json({**info.__dict__, "size_text": info.size_text()})
        return 0
    _out(i18n.t("bk.created", tag=info.tag))
    _out(f"  {info.size_text()}  {info.path}")
    return 0


def cmd_backups(args) -> int:
    items = engine.list_backups()
    if args.json:
        _emit_json([b.__dict__ for b in items])
        return 0
    if not items:
        _out(i18n.t("bk.empty"))
        return 0
    _out(f"{i18n.t('bk.tag'):<28}{i18n.t('bk.time'):<22}{i18n.t('bk.target'):<14}{i18n.t('bk.size')}")
    _out("-" * 82)
    for b in items:
        _out(f"{b.tag:<28}{b.created_at:<22}{(b.target_uid or '-')[:12]:<14}{b.size_text()}")
    return 0


def cmd_rollback(args) -> int:
    # 标签支持位置传参和 --tag 两种写法
    tag = args.tag or args.tag_pos
    if not tag:
        _out("缺少备份标签 / missing backup tag")
        _out("用法: wbs rollback <TAG>   或   wbs rollback --tag <TAG>")
        return 2

    if not args.yes and not getattr(args, "json", False):
        _out(i18n.t("wiz.rollback_title", tag=tag))
        _out("  " + i18n.t("wiz.rollback_desc"))
        ans = input("  (y/N): ").strip().lower()
        if ans != "y":
            _out("cancelled")
            return 1

    done = switcher.rollback(tag)
    if getattr(args, "json", False):
        _emit_json({"tag": tag, "restored": done})
        return 0
    _out(i18n.t("res.rolled_back", tag=tag))
    for d in done:
        _out(f"  restored: {d}")
    _out()
    _out("  " + i18n.t("res.restart_hint"))
    return 0


def cmd_diagnose(args) -> int:
    """只读诊断（按版本分段）。"""
    diag = paths.diagnostics()
    registered = {a.uid for a in profiles.list_accounts()}

    per_ed: dict = {}
    for ed in paths.edition_ids():
        with paths.use_edition(ed):
            per_ed[ed] = {
                "live_uid": profiles.live_uid(),
                "uids": engine.discover_uids(),
                "session_counts": engine.session_counts(),
                "automation_counts": engine.automation_counts(),
                "integrity": engine.integrity_check(),
                "archive": sessions.stats().__dict__,
            }

    if args.json:
        _emit_json({
            "paths": diag,
            "editions": per_ed,
            "registered": sorted(registered),
        })
        return 0

    _hr()
    _out("WorkBuddy 数据诊断（按版本）")
    _hr()
    for ed, es in (diag.get("editions") or {}).items():
        _out()
        _out(f"  [{i18n.t(f'ed.{ed}')}]")
        for k, v in es.items():
            if k == "label":
                continue
            _out(f"    {k:<24} {v}")
    _out()
    for ed, data in per_ed.items():
        label = i18n.t(f"ed.{ed}")
        _out(f"  [{label}] integrity={data['integrity']}  "
             f"{i18n.t('cli.current')}: {data['live_uid'] or '-'}")
        counts = data["session_counts"]
        autos = data["automation_counts"]
        uids = data["uids"]
        if uids:
            _out(f"    {'uid':<40}{'Sessions':>10}{'Memory':>10}{'Autos':>8}{'MCP':>6}  flags")
            _out("    " + "-" * 86)
        for uid in uids:
            with paths.use_edition(ed):
                mem = paths.memory_file(uid)
                mem_kb = f"{mem.stat().st_size / 1024:.1f}KB" if mem.exists() else "-"
                mcp = len(engine.read_mcp_servers(uid))
            flags = []
            if uid == data["live_uid"]:
                flags.append("CURRENT")
            flags.append("registered" if uid in registered else "UNREGISTERED")
            _out(
                f"    {uid:<40}{counts.get(uid, 0):>10}{mem_kb:>10}"
                f"{autos.get(uid, 0):>8}{mcp:>6}  {' '.join(flags)}"
            )
        st = data["archive"]
        if st.get("total"):
            _out(f"    {i18n.t('state.ed_sessions')}: {st['total']}")
    _out()
    return 0


def cmd_sessions(args) -> int:
    """查看或操作会话档案库。"""
    if getattr(args, "capture", False):
        by_ed: dict[str, int] = {}
        for ed in paths.edition_ids():
            with paths.use_edition(ed):
                by_ed[ed] = sessions.capture(ed)
        n = sum(by_ed.values())
        if getattr(args, "json", False):
            _emit_json({"captured": n, "by_edition": by_ed})
            return 0
        detail = "、".join(f"{i18n.t(f'ed.{e}')} {c}" for e, c in by_ed.items())
        _out(f"已收进会话档案库 {n} 条（{detail}）")
        return 0

    if getattr(args, "copy", None):
        # 会话复制：源账号的会话原样保留，目标账号得到独立副本（真共享）
        rep = sessions.copy_sessions(
            args.copy,
            getattr(args, "to", None),
            dry_run=getattr(args, "dry_run", False),
        )
        if getattr(args, "json", False):
            _emit_json(rep.__dict__)
            return 0 if rep.ok() else 1
        for e in rep.errors:
            _out(f"  ! {e}")
        if rep.errors:
            return 1
        src = profiles.find_by_uid(rep.source_uid)
        dst = profiles.find_by_uid(rep.target_uid)
        sname = src.name if src else rep.source_uid[:8]
        dname = dst.name if dst else rep.target_uid[:8]
        _out(f"{'[演练] ' if rep.dry_run else ''}「{sname}」→「{dname}」：{rep.text()}")
        if not rep.dry_run and rep.copied:
            _out("  " + i18n.t("sess.copy_note"))
            if rep.backup_tag:
                _out("  " + i18n.t("res.backup", tag=rep.backup_tag))
            if rep.edge_missing_db:
                _out("  ! 客户端没有云端映射库，副本未注册云端归属")
        return 0

    if getattr(args, "dedupe", None):
        # 同根源去重：每组只留内容最完整的一份，其余移除（正文先进隔离区）
        rep = sessions.dedupe_sessions(
            args.dedupe,
            dry_run=getattr(args, "dry_run", False),
            clear_error=getattr(args, "clear_error", False),
        )
        if getattr(args, "json", False):
            _emit_json(rep.__dict__)
            return 0 if rep.ok() else 1
        for e in rep.errors:
            _out(f"  ! {e}")
        if rep.errors:
            return 1
        acc = profiles.find_by_uid(rep.uid)
        name = acc.name if acc else rep.uid[:8]
        _out(f"{'[演练] ' if rep.dry_run else ''}「{name}」去重：{rep.text()}")
        if not rep.dry_run:
            if rep.backup_tag:
                _out("  " + i18n.t("res.backup", tag=rep.backup_tag))
            if rep.quarantine:
                _out(f"  被移除副本的正文已移入隔离区：{rep.quarantine}")
        return 0

    if getattr(args, "sync_cloud", False):
        # 云端归属修正：把 edge-sync 映射改回与档案库一致（默认两个版本都修）
        sel = paths.normalize_edition(getattr(args, "edition", None))
        eds = [sel] if sel else list(paths.edition_ids())
        dry = getattr(args, "dry_run", False)
        fixed = would = 0
        busy = missing = False
        for ed in eds:
            res = sessions.sync_cloud(dry_run=dry, edition=ed)
            fixed += int(res.get("fixed") or 0)
            would += int(res.get("would_fix") or 0)
            busy = busy or bool(res.get("busy"))
            missing = missing or bool(res.get("missing_db"))
        if getattr(args, "json", False):
            _emit_json({"dry_run": dry, "would_fix": would, "fixed": fixed,
                        "missing_db": missing, "busy": busy})
            return 0
        if dry:
            _out(f"[演练] 将修正 {would} 条云端归属")
        else:
            _out(f"云端归属已修正 {fixed} 条")
            if busy:
                _out("  ! 归属映射库被占用，部分未改 —— 退出客户端后重跑")
            if missing:
                _out("  ! 客户端没有云端映射库，跳过")
        return 0

    if getattr(args, "adopt", None):
        # 把某个账号的会话正式划归当前登录账号（三件套一起改）。
        # 目标账号 = 源账号所属版本的当前登录账号（版本隔离）。
        src_uid = args.adopt
        src_acc = profiles.find_by_uid(src_uid)
        src_ed = src_acc.edition if src_acc else (
            paths.normalize_edition(getattr(args, "edition", None))
            or paths.current_edition()
        )
        with paths.use_edition(src_ed):
            target_uid = profiles.read_login_uid() or profiles.live_uid()
        if not target_uid:
            _out(f"[{i18n.t(f'ed.{src_ed}')}] 没有检测到该版本的登录态，无法确定目标账号")
            return 1
        rep = sessions.adopt_into(src_uid, target_uid,
                                  dry_run=getattr(args, "dry_run", False))
        act = None
        if rep.adopted and not getattr(args, "dry_run", False):
            act = sessions.activate_for(target_uid)
            rep.client_rows = act.visible
        if getattr(args, "json", False):
            _emit_json({**rep.__dict__, "visible": act.visible if act else 0})
            return 0
        name = src_acc.name if src_acc else src_uid[:8]
        prefix = "[演练] 将" if getattr(args, "dry_run", False) else "已"
        _out(f"{prefix}把「{name}」的 {rep.adopted} 条会话划归当前账号")
        if not getattr(args, "dry_run", False):
            _out(f"  {rep.text()}")
            if rep.edge_db_busy:
                _out("  ! 云端归属映射库被客户端占用，未同步 —— 退出客户端后重跑本命令")
            if rep.bodies_missing:
                _out(f"  ! {rep.bodies_missing} 条会话缺正文文件，可能点不开")
            _out()
            _out("  " + i18n.t("sess.adopt_note"))
        return 0

    if getattr(args, "activate", None) is not None:
        uid = args.activate
        if uid in ("current", "@current", ""):
            sel = (paths.normalize_edition(getattr(args, "edition", None))
                   or paths.current_edition())
            with paths.use_edition(sel):
                uid = profiles.read_login_uid() or profiles.live_uid()
        act = sessions.activate_for(uid)
        if getattr(args, "json", False):
            _emit_json(act.__dict__)
            return 0 if act.ok else 1
        _out(f"激活会话：uid={uid[:8]}  {act.summary()}")
        if not act.ok:
            _out(f"  ! {act.detail}")
        return 0 if act.ok else 1

    # ---- 默认：状态（两个版本各自一段）----
    if getattr(args, "json", False):
        out: dict = {"editions": {}}
        for ed in paths.edition_ids():
            with paths.use_edition(ed):
                st = sessions.stats()
                drift = sessions.drift()
            out["editions"][ed] = {**st.__dict__, "drift": drift}
        _emit_json(out)
        return 0

    _hr()
    _out("会话档案库（本地留存）")
    _hr()
    _out()
    _out(f"  位置   {sessions.archive_path()}")
    for ed in paths.edition_ids():
        with paths.use_edition(ed):
            st = sessions.stats()
            drift = sessions.drift()
        _out()
        _out(f"  [{i18n.t(f'ed.{ed}')}]  {i18n.t('state.ed_sessions')}: {st.total} 条")
        for uid, n in sorted(st.by_owner.items(), key=lambda x: -x[1]):
            acc = profiles.find_by_uid(uid)
            _out(f"    {uid:<38}{n:>6}  {acc.name if acc else '(未建档)'}")
        if not drift.get("ok") and drift.get("count"):
            # 客户端里的归属与档案库不一致（被外部工具改过）：不自动改，先让你知道
            _out(f"    ! 检测到 {drift['count']} 条会话归属不一致（客户端 ≠ 档案库）")
            _out("      这通常是外部工具直接改过 user_id 造成的。")
            _out("      如需把某个账号的会话正式划归该版本当前账号：")
            _out("        python -m wbswitch.cli sessions --adopt <源账号 uid>")
        cloud = drift.get("cloud") or {}
        if cloud.get("count"):
            _out(f"    ! 云端归属有 {cloud['count']} 条与档案库不一致")
            _out("        python -m wbswitch.cli sessions --sync-cloud")
    _out()
    return 0


def cmd_kill(args) -> int:
    """结束客户端。默认检查两个版本，各杀各的（绝不混杀）。"""
    sel = paths.normalize_edition(getattr(args, "edition", None))
    eds = [sel] if sel else list(paths.edition_ids())
    results: dict[str, dict] = {}
    for ed in eds:
        if not client.is_running(ed):
            results[ed] = {"running": False, "killed": False, "detail": "not running"}
            continue
        ok = client.kill(ed)
        results[ed] = {"running": True, "killed": ok}
    if getattr(args, "json", False):
        _emit_json(results)
        return 0 if all(r.get("killed") or not r.get("running") for r in results.values()) else 1
    for ed, r in results.items():
        label = i18n.t(f"ed.{ed}")
        if not r.get("running"):
            _out(f"[{label}] WorkBuddy is not running")
        else:
            _out(f"[{label}] " + ("killed" if r.get("killed") else i18n.t("err.kill_timeout")))
    return 0 if all(r.get("killed") or not r.get("running") for r in results.values()) else 1


def cmd_launch(args) -> int:
    """启动客户端。指定 --edition 时只启动那个版本；不指定且本机只有一个
    版本用过数据时自动选它，两个版本都在用时要求明确指定。"""
    settings = config.load()
    sel = paths.normalize_edition(getattr(args, "edition", None))
    if sel:
        eds = [sel]
    else:
        eds = [e for e in paths.edition_ids() if paths.has_data(e)]
        if len(eds) > 1:
            if getattr(args, "json", False):
                _emit_json({"launched": False, "need_edition": True,
                            "editions": eds})
            else:
                _out(i18n.t("cli.launch_need_edition"))
            return 1
        if not eds:
            eds = [paths.current_edition()]

    results: dict[str, dict] = {}
    for ed in eds:
        path, ok = client.effective_client_path(settings.client_path_for(ed), ed)
        if not ok:
            results[ed] = {"launched": False, "path": path, "tried": path}
            continue
        client.launch(path)
        results[ed] = {"launched": True, "path": path}

    if getattr(args, "json", False):
        if len(eds) == 1:
            _emit_json(results[eds[0]])
        else:
            _emit_json(results)
        return 0 if all(r.get("launched") for r in results.values()) else 1
    for ed, r in results.items():
        label = i18n.t(f"ed.{ed}")
        if r.get("launched"):
            _out(f"[{label}] launched: {r.get('path')}")
        else:
            _out(f"[{label}] " + i18n.t("err.no_client"))
            _out(f"  tried: {r.get('tried')}")
    return 0 if all(r.get("launched") for r in results.values()) else 1


def cmd_history(args) -> int:
    items = switcher.read_history(args.limit)
    if args.json:
        _emit_json(items)
        return 0
    if not items:
        _out(i18n.t("log.empty"))
        return 0
    for it in items:
        _out(f"{it.get('at', ''):<20}{it.get('kind', ''):<14}{json.dumps({k: v for k, v in it.items() if k not in ('at', 'kind')}, ensure_ascii=False)}")
    return 0


def cmd_serve(args) -> int:
    """启动 OpenAI 兼容网关。

    让任何支持自定义 base_url 的 OpenAI 客户端用上你自己账号的额度。
    默认只监听 127.0.0.1（本机），不需要 API Key；监听非回环地址必须设 Key。
    """
    from . import gateway as gw_mod

    cfg = gw_mod.GatewayConfig(
        host=args.host,
        port=args.port,
        api_key=args.api_key or "",
        default_model=args.model or "auto",
    )

    if not args.quiet:
        _hr()
        _out(i18n.t("gw.title"))
        _hr()
        _out()

    gateway = gw_mod.Gateway(cfg)
    if not args.no_refresh:
        gateway.ensure_catalog()

    if args.json:
        _emit_json({
            "host": cfg.host, "port": cfg.port,
            "base_url": f"http://{cfg.host}:{cfg.port}/v1",
            "models": len(gateway.catalog.all()),
            "default_model": gateway.catalog.default_id(),
            "auth_required": bool(cfg.api_key),
        })
        return 0

    _out("  " + i18n.t("gw.base_url", url=f"http://{cfg.host}:{cfg.port}/v1"))
    _out("  " + i18n.t("gw.models", n=len(gateway.catalog.all()),
                       default=gateway.catalog.default_id()))
    _out("  " + i18n.t("gw.auth", state=i18n.t("common.yes") if cfg.api_key
                       else i18n.t("common.no")))
    _out()
    _out("  " + i18n.t("gw.usage_hint"))
    _out("  " + i18n.t("gw.stop_hint"))
    _out()

    try:
        gw_mod.serve(gateway, ready_callback=lambda srv: None)
    except KeyboardInterrupt:
        _out()
        _out("  " + i18n.t("gw.stopped"))
    except OSError as e:
        _out(f"  ! {i18n.t('gw.port_busy', port=cfg.port, err=e)}")
        return 1
    return 0


def cmd_models(args) -> int:
    """列出账号当前可用的模型。"""
    from . import gateway as gw_mod

    gateway = gw_mod.Gateway()
    gateway.ensure_catalog()
    models = gateway.catalog.all()

    if args.json:
        _emit_json([{"id": m.get("id"), "name": m.get("name"),
                     "credits": m.get("credits"),
                     "context_window": m.get("maxInputTokens"),
                     "max_output_tokens": m.get("maxOutputTokens"),
                     "default": bool(m.get("isDefault"))} for m in models])
        return 0

    _hr()
    _out(i18n.t("gw.models_title", n=len(models)))
    _hr()
    _out()
    _out(f"  {'id':<26}{'上下文':>10}{'输出':>9}  {'倍率':<16}说明")
    _out("  " + "-" * 84)
    for m in models:
        mark = " *" if m.get("isDefault") else ""
        _out(f"  {str(m.get('id')) + mark:<26}"
             f"{m.get('maxInputTokens', 0):>10}{m.get('maxOutputTokens', 0):>9}  "
             f"{str(m.get('credits') or '-'):<16}{str(m.get('descriptionZh') or '')[:22]}")
    _out()
    _out("  " + i18n.t("gw.default_mark"))
    _out()
    return 0


def cmd_login(args) -> int:
    """扫码登录新账号（不打扰当前登录）。

    两种用法：
      · 一步到位（命令行/终端）：直接跑，阻塞等待授权完成
      · 分步（图形前端）：`--start` 拿链接 → `--poll` 轮询 → `--cancel` 放弃
    """
    from . import login as login_mod

    # ---- 分步：开始 ----
    if getattr(args, "start", False):
        try:
            info = login_mod.begin(edition=args.edition or "cn")
        except Exception as e:
            if args.json:
                _emit_json({"ok": False, "error": str(e)})
                return 1
            _out(f"  ! {i18n.t('login.start_failed', err=e)}")
            return 1
        if args.json:
            _emit_json(info)
            return 0
        _out(info["auth_url"])
        return 0

    # ---- 分步：轮询 ----
    if getattr(args, "poll", False):
        res = login_mod.poll(window=args.window, name=args.name)
        if args.json:
            _emit_json(res)
        else:
            st = res.get("status")
            if st == "done":
                _out(i18n.t("login.ok", name=res["account"]["name"]))
            elif st == "pending":
                _out(i18n.t("login.waiting", sec=int(res.get("elapsed") or 0)))
            elif st == "none":
                _out(i18n.t("login.no_pending"))
            else:
                _out(res.get("error") or st)
        # "没有待完成的登录"不是错误 —— 调用本身成功了，状态由前端判断
        return 0

    # ---- 分步：取消 ----
    if getattr(args, "cancel", False):
        login_mod.cancel()
        if args.json:
            _emit_json({"ok": True})
            return 0
        _out(i18n.t("login.cancelled"))
        return 0

    # ---- 一步到位 ----
    edition = args.edition or "cn"
    if args.json:
        acc, _h = login_mod.login_and_save(
            edition=edition, open_browser=not args.no_browser,
            timeout=args.timeout, name=args.name
        )
        _emit_json({"id": acc.id, "name": acc.name, "uid": acc.uid})
        return 0

    _hr()
    _out(i18n.t("login.title"))
    _hr()
    _out()
    _out("  " + i18n.t("login.edition", ed=("国际版" if edition == "intl" else "国内版")))

    try:
        handle = login_mod.start_login(edition=edition)
    except Exception as e:
        _out(f"  ! {i18n.t('login.start_failed', err=e)}")
        return 1

    _out()
    _out("  " + i18n.t("login.open_url"))
    _out(f"  {handle.auth_url}")
    _out()
    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(handle.auth_url)
            _out("  " + i18n.t("login.browser_opened"))
        except Exception:
            pass
    _out("  " + i18n.t("login.waiting", sec=int(args.timeout)))

    def _tick(elapsed: float) -> None:
        _out(f"  … {int(elapsed)}s")

    try:
        session = login_mod.poll_login(handle, timeout=args.timeout, on_wait=_tick)
    except TimeoutError as e:
        _out()
        _out(f"  ! {e}")
        return 1
    except Exception as e:
        _out()
        _out(f"  ! {i18n.t('login.failed', err=e)}")
        return 1

    try:
        acc = login_mod.persist_login(session, args.name)
    except Exception as e:
        _out()
        _out(f"  ! {i18n.t('login.save_failed', err=e)}")
        return 1

    _out()
    _out("  " + i18n.t("login.ok", name=acc.name))
    _out(f"  uid {acc.uid}")
    _out("  id  " + acc.id)
    _out()
    _out("  " + i18n.t("login.hint"))
    switcher.log_history("login", {"name": acc.name, "uid": acc.uid, "edition": edition})
    return 0


def cmd_bodies(args) -> int:
    """会话正文归档：查看 / 收录 / 还原（两个版本一起）。"""
    if getattr(args, "archive", False):
        combined = {"checked": 0, "stored": 0, "deduped": 0,
                    "missing": 0, "bytes_added": 0}

        def _tick(i, total, sid):
            if i % 5 == 0 or i == total:
                _out(f"  … {i}/{total}")

        for ed in paths.edition_ids():
            with paths.use_edition(ed):
                if not paths.projects_dir().exists() and not paths.has_data(ed):
                    continue
                n = len(sessions.owners())
                _out(f"[{i18n.t(f'ed.{ed}')}] " + i18n.t("body.archiving", n=n))
                st = sessions.archive_bodies(on_progress=_tick)
            for k in combined:
                combined[k] += int(st.get(k) or 0)
        if args.json:
            _emit_json(combined)
            return 0
        _out()
        _out("  " + i18n.t("body.archived",
                           stored=combined["stored"], deduped=combined["deduped"],
                           missing=combined["missing"],
                           size=sessions._human(combined["bytes_added"])))
        return 0

    if getattr(args, "restore", None):
        sid = args.restore
        # 按档案归属解析版本：正文在哪个版本的工作区里，就还原到哪
        owner = sessions.owner_of(sid)
        acc = profiles.find_by_uid(owner) if owner else None
        ed = acc.edition if acc else (
            paths.normalize_edition(getattr(args, "edition", None))
            or paths.current_edition()
        )
        with paths.use_edition(ed):
            p = sessions.restore_body(sid)
        if args.json:
            _emit_json({"restored": str(p) if p else "", "edition": ed})
            return 0 if p else 1
        if p:
            _out(i18n.t("body.restored", path=p))
            return 0
        _out(i18n.t("body.not_found", sid=sid))
        return 1

    # 默认：看状态
    store = sessions.bodies_dir()
    blobs = sorted(store.glob("*.jsonl")) if store.exists() else []
    total = sum(b.stat().st_size for b in blobs)
    per: dict[str, dict] = {}
    for ed in paths.edition_ids():
        with paths.use_edition(ed):
            owner_map = sessions.owners()
            have = sum(1 for sid in owner_map if paths.find_session_body(sid) is not None)
        per[ed] = {"sessions": len(owner_map), "with_body": have}

    if args.json:
        _emit_json({"blobs": len(blobs), "bytes": total, "editions": per})
        return 0
    _hr()
    _out(i18n.t("body.title"))
    _hr()
    _out()
    _out(f"  {i18n.t('body.dir')}      {store}")
    _out(f"  {i18n.t('body.blobs')}    {len(blobs)}")
    _out(f"  {i18n.t('body.size')}     {sessions._human(total)}")
    for ed, d in per.items():
        _out(f"  [{i18n.t(f'ed.{ed}')}] {i18n.t('body.session')}  {d['sessions']}"
             f"  {i18n.t('body.withbody')} {d['with_body']}")
        if d["sessions"] and d["with_body"] < d["sessions"]:
            _out("    " + i18n.t("body.missing_hint", n=d["sessions"] - d["with_body"]))
    _out()
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


# --------------------------------------------------------------------------
# 联网命令：签到 / 积分
# --------------------------------------------------------------------------
#
# 这是本工具里唯一会访问 WorkBuddy 上游的部分；其余功能（换号、会话留存、
# 备份回滚）完全离线。签到用的是你自己账号本来就有的每日额度。
# --------------------------------------------------------------------------


def _resolve_accounts(args):
    """按 --id 过滤账号；不指定则全部。"""
    accounts = profiles.list_accounts()
    target = getattr(args, "id", None)
    if not target:
        return accounts, None
    acc = None
    for a in accounts:
        if (a.id.startswith(target) or a.uid.startswith(target)
                or a.name == target or a.name.lower() == target.lower()):
            acc = a
            break
    if acc is None:
        return [], target
    return [acc], None


def cmd_checkin(args) -> int:
    """一键签到：给账号领取每日签到积分（串行执行，避免上游风控）。"""
    from . import billing as billing_mod

    accounts, missing = _resolve_accounts(args)
    if missing is not None:
        _out(i18n.t("err.no_account", id=missing))
        return 1
    if not accounts:
        _out(i18n.t("cli.no_accounts"))
        return 1

    if args.json:
        report = billing_mod.claim_all(accounts)
        _emit_json({
            "ok": report.ok_count,
            "already": report.already_count,
            "inactive": report.inactive_count,
            "failed": report.fail_count,
            "skipped": report.skipped,
            "results": [
                {"uid": r.uid, "label": r.label, "success": r.success,
                 "already": r.already, "inactive": r.inactive,
                 "code": r.code, "message": r.message, "error": r.error,
                 "text": r.text()}
                for r in report.results
            ],
        })
        return 0

    _hr()
    _out(i18n.t("chk.title"))
    _hr()
    _out()

    def progress(i, n, name):
        _out(f"  [{i}/{n}] {name} …")

    report = billing_mod.claim_all(accounts, on_progress=progress)

    _out()
    for r in report.results:
        mark = "OK " if (r.success or r.already) else ("-- " if r.inactive else "!! ")
        _out(f"  [{mark}] {r.label:<24} {r.text()}")
    for name, why in report.skipped:
        _out(f"  [--] {name:<24} {i18n.t('chk.skipped', why=why)}")

    _out()
    _out("  " + report.summary())
    if report.inactive_count:
        _out()
        _out("  " + i18n.t("chk.inactive_hint"))
    _out()
    switcher.log_history("checkin", {
        "ok": report.ok_count, "already": report.already_count,
        "inactive": report.inactive_count, "failed": report.fail_count,
    })
    return 0


def cmd_credits(args) -> int:
    """查询账号积分 / 额度。"""
    from . import billing as billing_mod

    accounts, missing = _resolve_accounts(args)
    if missing is not None:
        _out(i18n.t("err.no_account", id=missing))
        return 1
    if not accounts:
        _out(i18n.t("cli.no_accounts"))
        return 1

    b = billing_mod.Billing()
    rows = []
    for acc in accounts:
        session = billing_mod.Session.from_account(acc)
        if session is None or not session.access_token:
            rows.append({"name": acc.name, "uid": acc.uid, "ok": False,
                         "error": "没有登录态快照"})
            continue
        try:
            st = b.checkin_status(session)
            cs = b.credits(session)
            rows.append({
                "name": acc.name, "uid": acc.uid, "ok": True,
                "edition": session.edition.id,
                "remain": cs.remain, "total": cs.total, "kind": cs.kind,
                "checkin": st.state_text() if st else "",
                "packs": [{"name": p.name, "remain": p.remain, "total": p.total}
                          for p in cs.packs],
            })
        except Exception as e:
            rows.append({"name": acc.name, "uid": acc.uid, "ok": False, "error": str(e)})

    if args.json:
        _emit_json(rows)
        return 0

    _hr()
    _out(i18n.t("cred.title"))
    _hr()
    _out()
    for r in rows:
        if not r["ok"]:
            _out(f"  {r['name']:<24} !! {r['error']}")
            continue
        _out(f"  {r['name']:<24} {r['remain']:g}/{r['total']:g}   {r['checkin']}")
        for p in r["packs"]:
            _out(f"      · {p['name']} {p['remain']:g}/{p['total']:g}")
    _out()
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wbs",
        description=f"{i18n.t('app.name')} v{__version__} — {i18n.t('cli.usage')}",
    )
    p.add_argument("--lang", choices=i18n.LANGS, help="输出语言 / output language")
    p.add_argument("--version", action="version", version=__version__)

    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("state", help="查看状态与账号列表")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_state)

    s = sub.add_parser("list", help="只列出账号")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("capture", help="保全当前登录")
    s.add_argument("--name")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_capture)

    s = sub.add_parser("switch", help="一键换号")
    s.add_argument("--id", required=True, help="账号 id / 名称 / uid 前缀")
    s.add_argument("--force", action="store_true", help="允许结束正在运行的 WorkBuddy")
    s.add_argument("--restart", action="store_true")
    s.add_argument("--no-restart", action="store_true")
    s.add_argument("--hot", action="store_true", help="热切换：不结束进程")
    s.add_argument("--no-hot", action="store_true")
    s.add_argument(
        "--no-login",
        action="store_true",
        help="不切换登录态：只搬数据，重启后由你自行登录",
    )
    s.add_argument("--dry-run", action="store_true", help="演练：只预览不写入")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_switch)

    s = sub.add_parser("sync", help="把指定账号的数据同步到当前登录账号")
    s.add_argument("--id", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("rename", help="重命名档案")
    s.add_argument("--id", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("delete", help="删除档案")
    s.add_argument("--id", required=True)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("refresh", help="用当前登录态刷新档案")
    s.add_argument("--id", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("backup", help="手动创建全量备份")
    s.add_argument("--uid", default="")
    s.add_argument("--label", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("backups", help="列出备份")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_backups)

    s = sub.add_parser("rollback", help="回滚到指定备份")
    # 标签既可以位置传参（rollback 20260916-2130），也可以 --tag 传参
    s.add_argument("tag_pos", nargs="?", default=None, metavar="TAG")
    s.add_argument("--tag", default=None)
    s.add_argument("--yes", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("diagnose", help="只读诊断")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_diagnose)

    s = sub.add_parser("sessions", help="会话档案库：查看 / 留存 / 激活 / 划归")
    s.add_argument("--capture", action="store_true", help="把客户端当前会话收进档案库")
    s.add_argument(
        "--activate",
        nargs="?",
        const="current",
        default=None,
        metavar="UID",
        help="让某账号的会话就位（默认当前登录账号）",
    )
    s.add_argument(
        "--adopt",
        default=None,
        metavar="UID",
        help="把指定账号的会话正式划归当前登录账号（三件套一起改）",
    )
    s.add_argument(
        "--copy",
        default=None,
        metavar="UID",
        help="把该账号的会话复制一份给目标账号（原会话保留，即「共享」）",
    )
    s.add_argument("--to", default=None, metavar="UID",
                   help="复制的目标账号（默认当前登录账号）")
    s.add_argument(
        "--dedupe",
        default=None,
        metavar="UID",
        help="把该账号名下同根源的重复副本去重（每组保留内容最完整的一份）",
    )
    s.add_argument("--clear-error", action="store_true",
                   help="去重时顺带把保留副本的 status='error'（红色感叹号）复位")
    s.add_argument("--sync-cloud", action="store_true",
                   help="把云端归属映射修正为与档案库一致")
    s.add_argument("--edition", choices=("cn", "intl"), default=None,
                   help="限定版本（国内版 cn / 国际版 intl）；不指定时状态视图"
                        "两个版本都显示，sync-cloud 两个版本都修")
    s.add_argument("--dry-run", action="store_true", help="只报告会改什么")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("login", help="扫码登录新账号（需联网）")
    s.add_argument("--edition", choices=("cn", "intl"), default="cn",
                   help="账号版本：cn 国内版 / intl 国际版")
    s.add_argument("--name", default=None, help="给它起个名字")
    s.add_argument("--timeout", type=float, default=300.0, help="等待授权超时（秒）")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    # 分步模式：给图形前端用（阻塞式会卡住界面）
    s.add_argument("--start", action="store_true", help="只拿授权链接，立刻返回")
    s.add_argument("--poll", action="store_true", help="轮询一小段（默认 25 秒）后返回")
    s.add_argument("--window", type=float, default=25.0, help="--poll 的单次等待秒数")
    s.add_argument("--cancel", action="store_true", help="放弃待完成的登录")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("bodies", help="会话正文归档：查看 / 收录 / 还原")
    s.add_argument("--archive", action="store_true", help="把正文收进归档（按内容去重）")
    s.add_argument("--restore", default=None, metavar="SESSION_ID",
                   help="还原某条会话的正文")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_bodies)

    s = sub.add_parser("kill", help="结束 WorkBuddy 进程（按版本，各杀各的）")
    s.add_argument("--edition", choices=("cn", "intl"), default=None,
                   help="只结束该版本；不指定时检查两个版本")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_kill)

    s = sub.add_parser("launch", help="启动 WorkBuddy（按版本）")
    s.add_argument("--edition", choices=("cn", "intl"), default=None,
                   help="启动该版本；两个版本都在用时必须指定")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("history", help="查看操作流水")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("gui", help="打开图形界面")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("checkin", help="一键签到：领取各账号每日积分（需联网）")
    s.add_argument("--id", default=None, help="只签到指定账号（id / 名称 / uid 前缀）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_checkin)

    s = sub.add_parser("credits", help="查询账号积分与额度（需联网）")
    s.add_argument("--id", default=None, help="只查指定账号")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_credits)

    s = sub.add_parser("serve", help="启动 OpenAI 兼容网关（需联网）")
    s.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    s.add_argument("--port", type=int, default=3065, help="监听端口（默认 3065）")
    s.add_argument("--api-key", default="", help="网关鉴权 Key（监听非本机时必须设置）")
    s.add_argument("--model", default=None, help="默认模型（默认 auto）")
    s.add_argument("--no-refresh", action="store_true", help="不刷新远程模型目录")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("models", help="列出账号可用的模型（需联网）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_models)

    return p


def _safe_lang(value: str | None) -> str | None:
    """把语言值收敛到白名单内，非法值一律丢弃。

    语言值来自命令行，会被拼进界面文案的查表键。`i18n.set_lang` 内部已有
    白名单校验，但这里再显式校验一次，让「外部输入 → 使用点」这条路径在
    任何静态检查下都是闭合的，不依赖被调用方的实现细节。
    """
    if value in i18n.LANGS:
        return value
    return None


def main(argv: list[str] | None = None) -> int:
    # 先兜住控制台编码：中文输出在非中文代码页的 Windows 上会直接崩
    console.make_console_safe()

    argv = list(sys.argv[1:] if argv is None else argv)

    # 信封模式：供 Tauri 前端当后端引擎调用（见 _Envelope 的说明）
    envelope = "--envelope" in argv
    if envelope:
        argv = [a for a in argv if a != "--envelope"]
        _Envelope.reset()
        _Envelope.active = True

    real_stdout = sys.stdout
    sink: "io.StringIO | None" = None
    if envelope:
        import io

        sink = io.StringIO()
        sys.stdout = sink

    # 语言：命令行 > 设置 > 系统
    settings = config.load()
    i18n.set_lang(_safe_lang(settings.language) or i18n.detect_system_lang())
    if "--lang" in argv:
        idx = argv.index("--lang")
        if idx + 1 < len(argv):
            chosen = _safe_lang(argv[idx + 1])
            if chosen:
                i18n.set_lang(chosen)

    parser = build_parser()
    args = parser.parse_args(argv)

    chosen = _safe_lang(getattr(args, "lang", None))
    if chosen:
        i18n.set_lang(chosen)

    rc = 0
    err = ""
    try:
        if not getattr(args, "func", None):
            # 无子命令时默认打开 GUI
            try:
                from .gui import main as gui_main

                gui_main()
            except Exception as e:
                # 侧车版不含 tkinter：这里给出可操作的提示而不是堆栈
                if envelope:
                    raise
                parser.print_help()
                print(f"\nGUI 启动失败: {e}")
                return 1
        else:
            rc = int(args.func(args) or 0)
    except KeyboardInterrupt:
        rc, err = 130, "cancelled"
    except Exception as e:  # noqa: BLE001
        rc, err = 1, str(e)

    if not envelope:
        if err and rc != 130:
            print(f"{i18n.t('common.failed')}: {err}", file=sys.stderr)
        return rc

    # ---- 信封模式：把缓冲的文案丢掉，只输出一个 JSON ----
    noise = sink.getvalue() if sink else ""
    sys.stdout = real_stdout
    body: dict = {
        "ok": rc == 0,
        "command": getattr(args, "func", None).__name__ if getattr(args, "func", None) else "gui",
    }
    if _Envelope.emitted:
        body["data"] = _Envelope.payload
    if not body["ok"]:
        body["error"] = err or "命令未能完成"
        # 把人类可读输出附上，方便排查（不进 UI 主流程）
        if noise.strip():
            body["detail"] = noise.strip()[:2000]

    # 必须写成 **UTF-8 字节**：调用方（Tauri/Rust）按 UTF-8 解析，
    # 而中文 Windows 上 sys.stdout 的默认编码是 GBK —— 直接 write(str)
    # 会输出 GBK 字节，对方解析就炸。所以绕过文本层，直接写字节。
    payload_bytes = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        buf = getattr(real_stdout, "buffer", None)
        if buf is not None:
            buf.write(payload_bytes)
            buf.flush()
        else:
            real_stdout.write(payload_bytes.decode("utf-8"))
            real_stdout.flush()
    except Exception:
        # 极端情况下退回到能写多少写多少，至少不要静默丢掉结果
        try:
            import os as _os

            _os.write(1, payload_bytes)
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
