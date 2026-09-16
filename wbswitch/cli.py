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

from . import __version__, client, config, engine, i18n, paths, profiles, sessions, switcher


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------


def _out(msg: str = "") -> None:
    print(msg)


def _hr() -> None:
    _out("=" * 74)


def _emit_json(obj) -> None:
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


def _account_table(rows: list[dict], live_uid: str) -> None:
    _out(i18n.t("cli.header", idx="#", name="NAME", uid="UID", sessions="SESSIONS", memory="MEMORY"))
    _out("  " + "-" * 92)
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
                name=(a.get("name") or "")[:24],
                uid=(a.get("uid") or "")[:38],
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
    if state["running"]:
        _out(f"  {i18n.t('state.running')}")
    elif state["live_logged_in"]:
        key = "state.unsaved" if state["unsaved_login"] else "state.safe"
        _out(f"  {i18n.t(key)}")
    else:
        _out(f"  {i18n.t('state.logged_out')}")
    _out(f"  {i18n.t('cli.current')}: {state['live_uid'] or i18n.t('common.none')}")
    _out(f"  WorkBuddy: {state['workbuddy_home']}")
    _out(f"  {i18n.t('set.client_path')}: {state['client_path'] or i18n.t('common.unknown')}"
         f" ({'ok' if state['client_path_ok'] else 'missing'})")
    _out(f"  store: {state['store_dir']}")
    _out()
    if not state["accounts"]:
        _out("  " + i18n.t("cli.no_accounts"))
    else:
        _account_table(state["accounts"], state["live_uid"])
    _out()
    _out(f"  {i18n.t('bk.title')}: {state['backup_count']}")
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
    _account_table(state["accounts"], state["live_uid"])
    return 0


def cmd_capture(args) -> int:
    acc = profiles.capture_current(args.name)
    if args.json:
        _emit_json(acc.to_dict())
    else:
        _out(i18n.t("res.captured", name=acc.name))
        _out(f"  uid: {acc.uid}")
        _out(f"  id : {acc.id}")
    switcher.log_history("capture", {"name": acc.name, "uid": acc.uid})
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
    info = engine.create_backup(uid, "", args.label or "manual")
    switcher.log_history("backup", {"tag": info.tag, "uid": uid})
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
    diag = paths.diagnostics()
    counts = engine.session_counts()
    autos = engine.automation_counts()
    uids = engine.discover_uids()
    live = profiles.live_uid()
    registered = {a.uid for a in profiles.list_accounts()}

    if args.json:
        _emit_json(
            {
                "paths": diag,
                "live_uid": live,
                "uids": uids,
                "session_counts": counts,
                "automation_counts": autos,
                "registered": sorted(registered),
                "integrity": engine.integrity_check(),
            }
        )
        return 0

    _hr()
    _out("WorkBuddy 数据诊断")
    _hr()
    _out()
    for k, v in diag.items():
        _out(f"  {k:<26} {v}")
    _out()
    _out(f"  {i18n.t('cli.current'):<26} {live or '-'}")
    _out(f"  integrity_check            {engine.integrity_check()}")
    _out()
    _out(f"  {'uid':<40}{'Sessions':>10}{'Memory':>10}{'Autos':>8}{'MCP':>6}  flags")
    _out("  " + "-" * 88)
    for uid in uids:
        mem = paths.memory_file(uid)
        mem_kb = f"{mem.stat().st_size / 1024:.1f}KB" if mem.exists() else "-"
        mcp = len(engine.read_mcp_servers(uid))
        flags = []
        if uid == live:
            flags.append("CURRENT")
        flags.append("registered" if uid in registered else "UNREGISTERED")
        _out(
            f"  {uid:<40}{counts.get(uid, 0):>10}{mem_kb:>10}"
            f"{autos.get(uid, 0):>8}{mcp:>6}  {' '.join(flags)}"
        )
    _out()
    st = sessions.stats()
    if st.total:
        _out(f"  会话档案库（本地留存）       共 {st.total} 条")
        for uid, n in sorted(st.by_owner.items()):
            _out(f"    {uid:<38} {n:>4} 条")
        _out()
    return 0


def cmd_sessions(args) -> int:
    """查看或操作会话档案库。"""
    if getattr(args, "capture", False):
        n = sessions.capture()
        if getattr(args, "json", False):
            _emit_json({"captured": n, **sessions.stats().__dict__})
            return 0
        _out(f"已收进会话档案库 {n} 条")
        return 0

    if getattr(args, "adopt", None):
        # 把某个账号的会话正式划归当前登录账号（档案库 + 客户端一起改）
        src_uid = args.adopt
        target_uid = profiles.read_login_uid() or profiles.live_uid()
        if not target_uid:
            _out("没有检测到登录态，无法确定目标账号")
            return 1
        src_acc = profiles.find_by_uid(src_uid)
        n = sessions.adopt_into(src_uid, target_uid)
        act = sessions.activate_for(target_uid) if n else None
        if getattr(args, "json", False):
            _emit_json({"adopted": n, "visible": act.visible if act else 0})
            return 0
        name = src_acc.name if src_acc else src_uid[:8]
        _out(f"已把「{name}」的 {n} 条会话划归当前账号")
        if act:
            _out(f"  当前账号现可见 {act.visible} 条")
        return 0

    if getattr(args, "activate", None) is not None:
        uid = args.activate
        if uid in ("current", "@current", ""):
            uid = profiles.read_login_uid() or profiles.live_uid()
        act = sessions.activate_for(uid)
        if getattr(args, "json", False):
            _emit_json(act.__dict__)
            return 0 if act.ok else 1
        _out(f"激活会话：uid={uid[:8]}  {act.summary()}")
        if not act.ok:
            _out(f"  ! {act.detail}")
        return 0 if act.ok else 1

    st = sessions.stats()
    drift = sessions.drift()
    if getattr(args, "json", False):
        _emit_json({**st.__dict__, "drift": drift})
        return 0
    _hr()
    _out("会话档案库（本地留存）")
    _hr()
    _out()
    _out(f"  位置   {sessions.archive_path()}")
    _out(f"  总数   {st.total} 条")
    if st.by_owner:
        _out()
        _out(f"  {'uid':<40}{'会话数':>8}  账号")
        _out("  " + "-" * 70)
        for uid, n in sorted(st.by_owner.items(), key=lambda x: -x[1]):
            acc = profiles.find_by_uid(uid)
            _out(f"  {uid:<40}{n:>8}  {acc.name if acc else '(未建档)'}")
    if not drift.get("ok") and drift.get("count"):
        # 客户端里的归属与档案库不一致（被外部工具改过）：不自动改，先让你知道
        _out()
        _out(f"  ! 检测到 {drift['count']} 条会话归属不一致（客户端 ≠ 档案库）")
        _out("    这通常是外部工具直接改过 user_id 造成的。")
        _out("    如需把某个账号的会话正式划归当前账号：")
        _out("      python -m wbswitch.cli sessions --adopt <源账号 uid>")
    _out()
    return 0


def cmd_kill(args) -> int:
    if not client.is_running():
        if getattr(args, "json", False):
            _emit_json({"running": False, "killed": False, "detail": "not running"})
        else:
            _out("WorkBuddy is not running")
        return 0
    ok = client.kill()
    if getattr(args, "json", False):
        _emit_json({"running": True, "killed": ok})
        return 0 if ok else 1
    _out("killed" if ok else i18n.t("err.kill_timeout"))
    return 0 if ok else 1


def cmd_launch(args) -> int:
    settings = config.load()
    path, ok = client.effective_client_path(settings.client_path)
    if not ok:
        if getattr(args, "json", False):
            _emit_json({"launched": False, "path": path, "tried": path})
        else:
            _out(i18n.t("err.no_client"))
            _out(f"  tried: {path}")
        return 1
    client.launch(path)
    if getattr(args, "json", False):
        _emit_json({"launched": True, "path": path})
        return 0
    _out(f"launched: {path}")
    return 0


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


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    gui_main()
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
        help="把指定账号的会话正式划归当前登录账号（档案库与客户端一起改）",
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("kill", help="结束 WorkBuddy 进程")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_kill)

    s = sub.add_parser("launch", help="启动 WorkBuddy")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("history", help="查看操作流水")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("gui", help="打开图形界面")
    s.set_defaults(func=cmd_gui)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 语言：命令行 > 设置 > 系统
    settings = config.load()
    i18n.set_lang(settings.language or i18n.detect_system_lang())
    if "--lang" in argv:
        idx = argv.index("--lang")
        if idx + 1 < len(argv):
            i18n.set_lang(argv[idx + 1])

    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "lang", None):
        i18n.set_lang(args.lang)

    if not getattr(args, "func", None):
        # 无子命令时默认打开 GUI
        try:
            from .gui import main as gui_main

            gui_main()
            return 0
        except Exception as e:
            parser.print_help()
            print(f"\nGUI 启动失败: {e}")
            return 1

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ncancelled")
        return 130
    except Exception as e:
        print(f"{i18n.t('common.failed')}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
