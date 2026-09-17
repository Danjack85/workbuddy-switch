# -*- coding: utf-8 -*-
"""一键换号编排。

换号 = 把「当前登录账号」的本地数据完整交接给「目标账号」，并写入目标账号身份快照，
     最后重启客户端。全程带自动保全 + 全量备份 + 写入校验，绝不丢号。

与 zcode-switch 的差异（重要）：
zcode-switch 直接替换 credentials.json 就能完成换号，因为 ZCode 的登录凭据是本地文件。
WorkBuddy 的登录态由客户端持有（腾讯云 OneID），第三方工具无法伪造。
所以本工具的"一键换号"做到的是：
  ① 目标账号的历史对话 / 记忆 / 连接器 一次性全部就位；
  ② 写入目标账号身份快照，客户端重启后按目标账号加载；
  ③ 原账号数据自动入库保全，随时可切回。
如果客户端因凭据不匹配要求重新登录，登录后数据已就位，不需要再手动跑任何迁移脚本。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from . import client, config, engine, paths, profiles, sessions
from .i18n import t

# --------------------------------------------------------------------------
# 结果结构
# --------------------------------------------------------------------------


@dataclass
class SwitchResult:
    switched: bool = False
    already_active: bool = False
    name: str = ""
    preserved_as: str = ""
    killed: bool = False
    launched: bool = False
    hot: bool = False
    login_state: bool = False
    preserved_login: bool = False
    imported_logins: int = 0
    identity_written: bool = False
    needs_login: bool = False
    sessions_visible: int = 0
    sessions_restored: int = 0
    sessions_parked: int = 0
    sessions_archived: int = 0
    backup_tag: str = ""
    dry_run: bool = False
    source_uid: str = ""
    target_uid: str = ""
    report: dict | None = None
    warnings: list[str] = field(default_factory=list)

    def toast_bits(self) -> list[str]:
        bits: list[str] = []
        if self.hot:
            bits.append("hot")
        if self.killed:
            bits.append("killed")
        if self.login_state:
            bits.append("credentials")
        if self.preserved_as:
            bits.append(f"preserved: {self.preserved_as}")
        if self.launched:
            bits.append("launched")
        if self.dry_run:
            bits.append("dry-run")
        return bits

# --------------------------------------------------------------------------
# 历史流水
# --------------------------------------------------------------------------


def log_history(kind: str, payload: dict) -> None:
    try:
        paths.ensure_store_dirs()
        import json

        line = json.dumps(
            {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **payload},
            ensure_ascii=False,
        )
        with open(paths.history_file(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_history(limit: int = 50) -> list[dict]:
    import json

    f = paths.history_file()
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []
    return out[-limit:][::-1]


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------


def build_state() -> dict:
    """给 GUI/CLI 的完整状态快照。"""
    settings = config.load()
    accounts = profiles.list_accounts()
    # 「当前是谁」以登录态文件为准（那才持有令牌）；快照只是它的派生物。
    uid = profiles.read_login_uid() or profiles.live_uid()
    counts = engine.session_counts()
    active = profiles.find_by_uid(uid) if uid else None

    client_path, client_ok = client.effective_client_path(settings.client_path)

    try:
        arch = sessions.stats()
    except Exception:
        arch = sessions.SessionStats()

    # 归属漂移检测：客户端里的 user_id 与档案库 owner 不一致（通常是被外部工具
    # 或旧版搬移逻辑改过）。这会让会话"忽有忽无"，必须让用户看得见。
    try:
        drift = sessions.drift()
        drift_count = int(drift.get("count") or 0)
    except Exception:
        drift_count = 0

    rows = []
    for a in accounts:
        d = a.to_dict()
        is_active = bool(uid) and a.uid == uid
        d["is_active"] = is_active
        d["identity_text"] = a.display_identity()
        d["label"] = a.label()
        arch_n = arch.owner_count(a.uid)
        d["archived_sessions"] = arch_n
        # 「会话数」= 切到该账号后能看到的条数（这是用户真正关心的）：
        #   ① 档案库按 owner 留存的是完整真值；
        #   ② 还没归档过的当前账号，用数据库里的实时条数兜底；
        #   ③ 其余情况就是 0。
        # 不能拿档案里缓存的 stats.sessions —— 那是建档当时的快照，
        # 会话早已随换号易主，显示出来会误导。
        if arch_n:
            d["stats"]["sessions"] = arch_n
        elif is_active:
            d["stats"]["sessions"] = counts.get(a.uid, 0)
        else:
            d["stats"]["sessions"] = 0
        # 以磁盘事实为准：档案里标志说"有"但文件被删了就得如实反映
        d["has_login_state"] = any(a.session_dir().glob("*.info"))
        # 凭据有效期：让用户在换号前就知道会不会被要求重新登录
        try:
            exp = profiles.session_expiry(a)
            d["login_expires_at"] = exp["expires_at"]
            d["login_expired"] = profiles.session_expired(a)
        except Exception:
            d["login_expires_at"] = 0
            d["login_expired"] = False
        rows.append(d)

    return {
        "running": client.is_running(),
        "live_uid": uid,
        "live_logged_in": bool(uid),
        "live_identity": (profiles.read_live_identity() if uid else None),
        # 登录会话文件里的 uid：与快照不一致说明有人手工动过其中一个
        "session_uid": profiles.read_login_uid(),
        "auth_file": str(paths.login_state_file() or ""),
        "active_account_id": active.id if active else None,
        "unsaved_login": bool(uid) and active is None,
        "accounts": rows,
        "settings": asdict(settings),
        "client_path": client_path,
        "client_path_ok": client_ok,
        "workbuddy_home": str(paths.workbuddy_dir()),
        "store_dir": str(paths.store_dir()),
        "db_ok": paths.db_path().exists(),
        "backup_count": len(engine.list_backups()),
        "archived_total": arch.total,
        "session_drift": drift_count,
        "lang": settings.language or "",
    }


# --------------------------------------------------------------------------
# 一键换号
# --------------------------------------------------------------------------


def switch_to(
    account_id: str,
    *,
    force: bool = False,
    restart: bool | None = None,
    hot: bool | None = None,
) -> SwitchResult:
    """一键换号：把当前登录账号的数据交接给目标账号，并写入目标身份。"""
    settings = config.load()
    target = profiles.load_account(account_id)

    if hot is None:
        hot = settings.hot_switch
    if restart is None:
        restart = settings.restart_after_switch

    result = SwitchResult(name=target.name, target_uid=target.uid, hot=hot)
    dry_run = settings.dry_run
    result.dry_run = dry_run

    # 「当前是谁」取登录会话文件（那才持有令牌）；快照只是它的派生物。
    # 两者不一致时以会话为准，并明确告警——否则会把数据搬到错误的账号名下。
    live = profiles.live_identity_or_none()
    source_uid = str((live or {}).get("uid") or "")
    sess_uid = profiles.read_login_uid()
    if sess_uid and sess_uid != source_uid:
        result.warnings.append(
            f"snapshot uid {source_uid or '(none)'} != session uid {sess_uid}; using session"
        )
    if sess_uid:
        source_uid = sess_uid
    result.source_uid = source_uid

    # ---- 凭据可用性预检：令牌过期就必须重新登录，工具无法代为刷新 ----
    # 这里只做「过期」判断（直接读磁盘上的凭据，可靠）；
    # 「有没有凭据」要等 2b 步导入完成后才能定论，最终结论在第 5 步给出。
    if settings.switch_login_state and profiles.session_expired(target):
        result.warnings.append(t("res.token_expired", name=target.name))

    # ---- 已经是当前账号 ----
    # 仍然要激活一次：档案库里可能存着这个账号更早的会话（曾被切走），
    # 激活会把它们收回当前账号名下，表现为「历史会话自动恢复」。
    if source_uid and source_uid == target.uid:
        result.already_active = True
        if not dry_run:
            if settings.keep_sessions:
                try:
                    act = sessions.activate_for(target.uid)
                    result.sessions_visible = act.visible
                    result.sessions_restored = act.inserted
                    result.sessions_parked = act.parked
                    result.sessions_archived = act.new_archived
                    if not act.ok and act.detail:
                        result.warnings.append(f"session archive: {act.detail}")
                except Exception as e:
                    result.warnings.append(f"activate sessions failed: {e}")

            restored = profiles.restore_private(target)
            target.stats = engine.scan_stats(target.uid)
            target.stats.private_files = restored or target.stats.private_files
            target.last_seen_at = profiles.now_ts()
            profiles.save_account(target)
            # 档案缺登录态时顺手补一份，这样切回来不用重新登录
            if settings.switch_login_state and not target.has_login_state:
                try:
                    if profiles.snapshot_login_state(target):
                        profiles.save_account(target)
                except Exception as e:
                    result.warnings.append(f"refresh login state failed: {e}")
        return result

    running = client.is_running()

    # ---- 运行中且未开热切换：需要 force ----
    if running and not hot and not force and not dry_run:
        raise RuntimeError(t("err.switch_running"))

    # ---- 1. 自动保全当前登录（绝不丢号）----
    if settings.auto_capture and not dry_run:
        try:
            preserved = profiles.auto_preserve_current()
            if preserved:
                result.preserved_as = preserved
                log_history("auto-preserve", {"name": preserved, "uid": source_uid})
            # auth 目录里出现过、但还没建档的账号也补上，
            # 这样它们能直接一键切到，而不必先手动登录一次
            adopted = profiles.adopt_accounts_from_auth()
            if adopted:
                result.warnings.append(f"adopted {len(adopted)} account(s) from login files")
        except Exception as e:
            result.warnings.append(f"auto-preserve failed: {e}")

    # ---- 1b. 把客户端现有会话收进本地档案库 ----
    #      必须先归档再切换：否则切走后这一步的 client 会话已经换人了。
    if settings.keep_sessions and not dry_run:
        try:
            result.sessions_archived = sessions.capture()
        except Exception as e:
            result.warnings.append(f"archive sessions failed: {e}")

    # ---- 2. 结束客户端（非热切换）----
    if running and not hot and not dry_run:
        if not client.kill():
            raise RuntimeError(t("err.kill_timeout"))
        result.killed = True

    # ---- 2b. 客户端已退出，此刻才读得到登录文件 ----
    #      ① 把当前账号的登录态存进档案（少了这步，切回去只能重新登录）；
    #      ② 把 auth 目录里其它账号的历史会话也导入各自档案，
    #         这样它们都能免登录切换，而不只是最后一次登录的那个。
    if settings.switch_login_state and not dry_run and not client.is_running():
        src_acc = profiles.find_by_uid(source_uid) if source_uid else None
        if src_acc is not None:
            try:
                if profiles.snapshot_login_state(src_acc):
                    result.preserved_login = True
                    profiles.save_account(src_acc)
            except Exception as e:
                result.warnings.append(f"preserve login state failed: {e}")
        try:
            imported = profiles.import_login_states()
            if imported:
                result.imported_logins = len(imported)
                log_history(
                    "import-logins",
                    {"count": len(imported), "names": list(imported.values())},
                )
        except Exception as e:
            result.warnings.append(f"import login states failed: {e}")

    # ---- 3. 同步其余数据（记忆 / 连接器 / 定时任务 / 账号设置）----
    #      会话**不在这里搬**：它的归属由 sessions 档案库按 owner 管理，
    #      硬搬会让账号间来回丢数据。会话在下面的第 4c 步激活。
    if source_uid:
        report = engine.sync(
            source_uid,
            target.uid,
            do_backup=settings.auto_backup,
            do_sessions=False,
            do_memory=settings.merge_memory,
            do_connectors=settings.merge_connectors,
            do_automations=settings.merge_automations,
            do_settings=settings.merge_settings,
            dry_run=dry_run,
            label=f"switch->{target.name}",
        )
        result.report = report.to_dict()
        result.backup_tag = report.backup_tag
        result.warnings.extend(report.warnings)
    else:
        # 没有登录态：只做备份，避免"无源可迁"时报错
        if settings.auto_backup and not dry_run:
            info = engine.create_backup(target.uid, "", f"switch->{target.name}")
            result.backup_tag = info.tag

    if dry_run:
        result.switched = True
        return result

    # ---- 4a. 凭据级换号：把目标账号的登录会话写回客户端 ----
    #     必须在客户端已退出时做，否则会被运行中的客户端覆写回去。
    #     这一步成功就不必再重新登录；失败则退化为"数据已就位 + 重新登录"。
    if settings.switch_login_state:
        if client.is_running():
            result.warnings.append(t("res.credentials_kept"))
        else:
            try:
                n = profiles.restore_login_state(target)
                if n:
                    result.login_state = True
                    # 兜底：老快照可能缺账号展示字段，但绝不碰 auth 段
                    profiles.patch_login_session_account(
                        {
                            "uid": target.uid,
                            "nickname": target.nickname or target.name,
                            "type": target.account_type or "personal",
                        }
                    )
                elif target.has_login_state:
                    result.warnings.append("login state snapshot missing on disk")
                else:
                    result.warnings.append(t("res.credentials_kept"))
            except Exception as e:
                result.warnings.append(f"credential restore failed: {e}")

    # ---- 4b. 还原目标账号私有存储 ----
    try:
        profiles.restore_private(target)
    except Exception as e:
        result.warnings.append(f"restore private failed: {e}")

    # ---- 4c. 激活会话：目标账号的历史会话就位，其余账号的会话归位隐藏 ----
    #      这是「换号后会话自动恢复」的核心：会话按 owner 长期留存在本地档案库，
    #      登录哪个账号就把哪个账号的会话放到前台。
    if settings.keep_sessions:
        try:
            act = sessions.activate_for(target.uid)
            result.sessions_visible = act.visible
            result.sessions_restored = act.inserted
            result.sessions_parked = act.parked
            result.sessions_archived = act.new_archived
            if not act.ok and act.detail:
                result.warnings.append(f"session archive: {act.detail}")
        except Exception as e:
            result.warnings.append(f"activate sessions failed: {e}")

    # ---- 4d. 凭据切换回读校验：登录文件里必须确实是目标账号 ----
    #      必须在写身份快照之前做，否则可能先写了快照又发现凭据没换成功。
    if result.login_state:
        got = profiles.read_login_uid()
        if got and got != target.uid:
            result.login_state = False
            result.warnings.append(
                f"login session uid mismatch after restore: got {got}, want {target.uid}"
            )

    # ---- 5. 写入登录身份快照 ----
    # 只有把登录态真的换过去了才写身份快照，否则会出现「快照说 B、实际登录 A」
    # 这种自相矛盾的状态，下一次换号还会被误导。登录态换不过去时（目标账号
    # 没有可用凭据）就让客户端在用户登录后自己写，我们只把数据准备好。
    if result.login_state or not settings.switch_login_state:
        primary = {
            "uid": target.uid,
            "nickname": target.nickname or target.name,
            "type": target.account_type or "personal",
            "editionType": target.edition_type,
            "isPro": target.is_pro,
            "isAdmin": target.is_admin,
            "oneidAccountId": target.oneid_account_id,
            "savedAt": int(time.time() * 1000),
        }
        _write_identity_verified(primary, target.uid, retries=3 if hot else 1)
        result.identity_written = True

    # 「是否需要用户登录」= 没换成功，或换过去的凭据已过期。
    # 两者都要如实告知，否则用户会以为切完就能直接用。
    # 用户主动选择了不切登录态（--no-login）时不提示：那是他自己的决定。
    if not settings.switch_login_state:
        result.needs_login = False
    elif not result.login_state:
        result.needs_login = True
        if not any(t("res.token_expired", name=target.name) in w for w in result.warnings):
            result.warnings.append(t("res.login_required", name=target.name))
    elif profiles.session_expired(target):
        result.needs_login = True
    else:
        result.needs_login = False


    # ---- 5c. 会话激活回读校验：目标账号名下的会话数应与档案一致 ----
    if settings.keep_sessions and result.sessions_visible:
        try:
            chk = sessions.verify_roundtrip(target.uid)
            if not chk.get("ok"):
                result.warnings.append(
                    f"session verify: {chk.get('visible')}/{chk.get('expected')} visible"
                )
        except Exception as e:
            result.warnings.append(f"session verify failed: {e}")

    # ---- 6. 刷新档案统计 ----
    target.stats = engine.scan_stats(target.uid)
    target.updated_at = profiles.now_ts()
    target.last_seen_at = profiles.now_ts()
    profiles.save_account(target)

    # ---- 7. 重启客户端 ----
    if restart:
        client_path, ok = client.effective_client_path(settings.client_path)
        if ok:
            try:
                if client.launch_ok(client_path):
                    result.launched = True
            except Exception as e:
                result.warnings.append(f"launch failed: {e}")

    result.switched = True
    log_history(
        "switch",
        {
            "from": source_uid,
            "to": target.uid,
            "name": target.name,
            "backup": result.backup_tag,
            "killed": result.killed,
            "hot": result.hot,
            "login_state": result.login_state,
        },
    )
    return result


def _write_identity_verified(primary: dict, want_uid: str, retries: int = 1) -> None:
    """写入身份快照并回读校验，防止被客户端覆写。"""
    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            profiles.write_live_identity(primary)
            got = profiles.live_uid()
            if got == want_uid:
                time.sleep(0.15)
                if profiles.live_uid() == want_uid:
                    return
            last_err = RuntimeError(t("err.verify"))
        except Exception as e:
            last_err = e
        time.sleep(0.25 * (attempt + 1))
    if last_err:
        raise last_err


# --------------------------------------------------------------------------
# 仅同步（不换身份）
# --------------------------------------------------------------------------


def sync_account_to_current(
    account_id: str,
    *,
    do_memory: bool | None = None,
    do_connectors: bool | None = None,
    do_automations: bool | None = None,
    adopt_sessions: bool = True,
    dry_run: bool | None = None,
) -> dict:
    """把指定账号的本地数据并入当前登录账号。

    适用于「我已经在客户端登录了新号，把旧号的数据拿过来」。

    会话的处理方式很关键：**不能**只改客户端里的 `user_id`。那样档案库记录的
    归属会和事实脱节，下次激活时又会被"归位"回去，表现为会话忽有忽无。
    正确做法是先 `sessions.adopt_into()` 把档案库里的归属也正式改过来，
    再激活 —— 两边一致，之后切换行为稳定可预期。
    """
    settings = config.load()
    src = profiles.load_account(account_id)
    # 以登录态文件为准（那才持有令牌）
    target_uid = profiles.read_login_uid() or profiles.live_uid()
    if not target_uid:
        raise RuntimeError(t("err.no_snapshot", path=paths.account_snapshot_file()))
    if src.uid == target_uid:
        raise RuntimeError(t("err.same"))

    dry = settings.dry_run if dry_run is None else dry_run

    if dry:
        # 演练：只报会改什么，不落盘
        report = engine.sync(
            src.uid,
            target_uid,
            do_backup=False,
            do_sessions=False,
            do_memory=settings.merge_memory if do_memory is None else do_memory,
            do_connectors=settings.merge_connectors if do_connectors is None else do_connectors,
            do_automations=settings.merge_automations if do_automations is None else do_automations,
            do_settings=settings.merge_settings,
            dry_run=True,
            label=f"sync:{src.name}->current",
        )
        out = report.to_dict()
        if settings.keep_sessions and adopt_sessions:
            n = sessions.stats().owner_count(src.uid)
            out.setdefault("steps", []).append(
                {"name": "sessions", "ok": True, "changed": n, "skipped": n == 0,
                 "detail": f"[dry-run] would adopt {n} sessions"}
            )
        return out

    # ---- 1. 先归档当前客户端的会话（保住最新状态）----
    if settings.keep_sessions:
        sessions.capture()

    report = engine.sync(
        src.uid,
        target_uid,
        do_backup=settings.auto_backup,
        do_sessions=False,   # 会话不走"改 user_id"的搬法，见上面的说明
        do_memory=settings.merge_memory if do_memory is None else do_memory,
        do_connectors=settings.merge_connectors if do_connectors is None else do_connectors,
        do_automations=settings.merge_automations if do_automations is None else do_automations,
        do_settings=settings.merge_settings,
        dry_run=False,
        label=f"sync:{src.name}->current",
    )

    # ---- 2. 会话：把归属正式划到当前账号，再激活 ----
    adopt_rep = sessions.AdoptReport()
    if settings.keep_sessions:
        try:
            if adopt_sessions:
                # 三处一起改：档案库归属 + 客户端索引 + 云端归属映射
                adopt_rep = sessions.adopt_into(src.uid, target_uid)
            act = sessions.activate_for(target_uid)
            # 客户端索引的改动数由 activate_for 完成，补进报告
            adopt_rep.client_rows = act.visible
            report.add(
                engine.StepResult(
                    "sessions",
                    ok=act.ok and adopt_rep.ok(),
                    changed=act.visible,
                    skipped=act.visible == 0,
                    detail=(
                        f"adopted {adopt_rep.adopted}, visible {act.visible}"
                        + (f", cloud {adopt_rep.edge_rows}" if adopt_rep.edge_rows else "")
                        + (", cloud-db-busy" if adopt_rep.edge_db_busy else "")
                        + (f", {act.detail}" if act.detail else "")
                    ),
                )
            )
            if adopt_rep.edge_db_busy:
                report.warnings.append(
                    "云端归属映射库被客户端占用，未同步 —— 请退出客户端后重跑"
                )
            if adopt_rep.bodies_missing:
                report.warnings.append(
                    f"{adopt_rep.bodies_missing} 条会话缺少正文文件，可能点不开"
                )
        except Exception as e:
            report.add(engine.StepResult("sessions", ok=False, detail=str(e)))

    cur = profiles.find_by_uid(target_uid)
    if cur is not None:
        cur.stats = engine.scan_stats(target_uid)
        cur.updated_at = profiles.now_ts()
        cur.last_seen_at = profiles.now_ts()
        profiles.save_account(cur)

    log_history(
        "sync",
        {
            "from": src.uid,
            "to": target_uid,
            "name": src.name,
            "backup": report.backup_tag,
            "dry_run": dry,
        },
    )
    return report.to_dict()


# --------------------------------------------------------------------------
# 回滚
# --------------------------------------------------------------------------


def rollback(tag: str) -> list[str]:
    done = engine.restore_backup(tag)
    log_history("rollback", {"tag": tag, "restored": done})
    return done
