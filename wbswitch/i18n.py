# -*- coding: utf-8 -*-
"""中英双语。对齐 zcode-switch 的 i18n 覆盖范围：主窗、托盘、错误提示、CLI 输出。"""

from __future__ import annotations

import json
import os
import platform

LANGS = ("zh", "en")

_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        # 通用
        "app.name": "WorkBuddy Switch",
        "app.tagline": "WorkBuddy 账号一键换号 · 本地信息自动同步",
        "common.ok": "确定",
        "common.cancel": "取消",
        "common.confirm": "确认",
        "common.save": "保存",
        "common.delete": "删除",
        "common.close": "关闭",
        "common.refresh": "刷新",
        "common.settings": "设置",
        "common.loading": "加载中…",
        "common.none": "无",
        "common.unknown": "未知",
        "common.yes": "是",
        "common.no": "否",
        "common.done": "完成",
        "common.failed": "失败",
        "common.copy": "复制",
        # 状态
        "state.running": "WorkBuddy 运行中",
        "state.safe": "登录态已保全",
        "state.unsaved": "检测到未入库登录",
        "state.logged_out": "未检测到登录态",
        "state.current": "当前",
        "state.accounts": "账号",
        # 按钮
        "btn.capture": "保全当前登录",
        "btn.switch": "一键换号",
        "btn.sync": "同步到此账号",
        "btn.rename": "重命名",
        "btn.delete": "删除",
        "btn.details": "详情",
        "btn.launch": "启动 WorkBuddy",
        "btn.kill": "结束 WorkBuddy",
        "btn.restore": "回滚到此备份",
        "btn.open_store": "打开档案库",
        "btn.reveal_backups": "打开备份目录",
        "btn.export": "导出档案",
        "btn.import": "导入档案",
        "btn.copy_cmd": "复制 CLI 命令",
        # 账号行
        "row.in_use": "使用中",
        "row.no_private": "无私有配置",
        "row.cred_saved": "登录态已存",
        "row.cred_expired": "凭据已过期",
        "row.archived": "本地留存 {n} 条",
        "row.sessions": "{n} 会话",
        "row.memory": "记忆 {v}",
        "row.mcp": "{n} 连接器",
        "row.last_seen": "最近 {t}",
        # 向导
        "wiz.switch_title": "切换到「{name}」？",
        "wiz.switch_desc": "将执行：保全当前登录 → 全量备份 → 把当前账号的本地数据同步到目标账号 → 写入登录身份 → 重启 WorkBuddy。\n备份标签：{tag}",
        "wiz.isolate_desc": "隔离模式：只切换登录身份与账号私有配置，不合并会话数据。",
        "wiz.has_snapshot": "该账号存有登录态快照，切换后应无需重新登录。",
        "wiz.no_snapshot": "该账号没有登录态快照，重启后需要重新登录一次；数据会自动就位。",
        "wiz.hot_needs_login": "热切换不会替换登录态（Cookie 被运行中的客户端占用），重启后需要重新登录。",
        "wiz.sync_title": "把「{src}」同步到当前账号？",
        "wiz.sync_desc": "会话记录、长期记忆、连接器配置会合并到当前登录账号（{dst}）。目标账号已有内容不会被覆盖。",
        "wiz.delete_title": "删除档案「{name}」？",
        "wiz.delete_desc": "只删除本工具保存的档案，不会动 WorkBuddy 磁盘上的任何数据。",
        "wiz.kill_title": "结束 WorkBuddy 进程？",
        "wiz.kill_desc": "未保存的对话可能丢失。",
        "wiz.rollback_title": "回滚到备份 {tag}？",
        "wiz.rollback_desc": "将用备份覆盖当前的数据库、记忆、连接器与账号私有存储。此操作不可撤销。",
        # 结果
        "res.switched": "已切换到「{name}」",
        "res.switch_bits": "{bits}",
        "res.already": "「{name}」已是当前账号",
        "res.synced": "已同步「{src}」→「{dst}」",
        "res.sessions": "迁移 {n} 条会话",
        "res.memory": "合并记忆 {n} 行",
        "res.connectors": "合并连接器 {n} 项",
        "res.automations": "迁移 {n} 个定时任务",
        "res.settings": "补齐 {n} 项账号设置",
        "res.sessions_visible": "该账号会话 {n} 条已就位",
        "res.sessions_restored": "从本地档案恢复 {n} 条会话",
        "res.sessions_parked": "其余 {n} 条会话已留存（切回即恢复）",
        "res.credentials": "登录态已切换（无需重新登录）",
        "res.credentials_kept": "该档案没有登录态快照，重启后可能需要重新登录",
        "res.login_required": "需要在客户端登录一次「{name}」；数据已全部就位，登录后即可看到",
        "res.token_expired": "「{name}」的登录凭据已过期，需要重新登录一次；数据已全部就位",
        "res.backup": "备份 {tag}",
        "res.restart_hint": "请重启 WorkBuddy 客户端让变更生效",
        "res.rolled_back": "已回滚到 {tag}",
        "res.captured": "已保全登录：{name}",
        "res.deleted": "已删除档案「{name}」",
        "res.renamed": "已重命名为「{name}」",
        # 错误
        "err.no_home": "找不到 WorkBuddy 数据目录（{path}）",
        "err.no_db": "找不到数据库：{path}",
        "err.no_snapshot": "找不到账号快照：{path}，请先登录 WorkBuddy",
        "err.bad_snapshot": "账号快照解析失败：{e}",
        "err.same": "源账号与目标账号相同，无需操作",
        "err.no_account": "档案不存在：{id}",
        "err.dup_login": "该登录态已存在档案「{name}」",
        "err.no_backup": "备份不存在：{tag}",
        "err.name_empty": "名称不能为空",
        "err.name_taken": "名称「{name}」已被占用",
        "err.switch_running": "WorkBuddy 正在运行，请先结束进程或开启热切换",
        "err.kill_timeout": "结束 WorkBuddy 进程超时",
        "err.verify": "写入校验失败，数据可能被客户端覆写",
        "err.no_client": "未找到 WorkBuddy 客户端，可在设置里手动指定",
        "err.busy": "有操作正在进行，请稍候",
        "err.no_sessions": "源账号没有可迁移的会话",
        # 设置
        "set.title": "设置",
        "set.language": "语言 / Language",
        "set.client_path": "WorkBuddy 客户端路径",
        "set.pick": "浏览…",
        "set.hot_switch": "热切换（不结束进程直接换号）",
        "set.restart_after": "切换后自动启动 WorkBuddy",
        "set.auto_backup": "切换前自动全量备份",
        "set.auto_capture": "自动保全当前登录（推荐开启）",
        "set.merge_memory": "同步时合并长期记忆",
        "set.merge_connectors": "同步时合并连接器配置",
        "set.sync_tasks": "同步时迁移定时任务",
        "set.merge_settings": "同步时补齐账号级设置（消息渠道等）",
        "set.keep_sessions": "会话留存本地：换号后自动恢复该账号的会话",
        "set.switch_login": "换号时一并切换登录态（凭据级换号）",
        "set.dry_run": "演练模式（只预览不写入）",
        "set.saved": "设置已保存",
        # 备份
        "bk.title": "备份与回滚",
        "bk.tag": "标签",
        "bk.time": "时间",
        "bk.target": "目标账号",
        "bk.size": "大小",
        "bk.empty": "暂无备份",
        "bk.created": "已创建备份 {tag}",
        # 日志
        "log.title": "运行日志",
        "log.empty": "暂无日志",
        # CLI
        "cli.usage": "WorkBuddy 一键换号与本地信息同步",
        "cli.current": "当前登录",
        "cli.no_accounts": "还没有任何账号档案，先执行 capture",
        "cli.header": "  {idx:<4} {name:<24} {uid:<38} {sessions:>9} {memory:>10}",
    },
    "en": {
        "app.name": "WorkBuddy Switch",
        "app.tagline": "One-click WorkBuddy account switch with local data sync",
        "common.ok": "OK",
        "common.cancel": "Cancel",
        "common.confirm": "Confirm",
        "common.save": "Save",
        "common.delete": "Delete",
        "common.close": "Close",
        "common.refresh": "Refresh",
        "common.settings": "Settings",
        "common.loading": "Loading…",
        "common.none": "none",
        "common.unknown": "unknown",
        "common.yes": "yes",
        "common.no": "no",
        "common.done": "Done",
        "common.failed": "Failed",
        "common.copy": "Copy",
        "state.running": "WorkBuddy is running",
        "state.safe": "Login preserved",
        "state.unsaved": "Unsaved login detected",
        "state.logged_out": "No login detected",
        "state.current": "current",
        "state.accounts": "accounts",
        "btn.capture": "Preserve login",
        "btn.switch": "Switch",
        "btn.sync": "Sync here",
        "btn.rename": "Rename",
        "btn.delete": "Delete",
        "btn.details": "Details",
        "btn.launch": "Launch WorkBuddy",
        "btn.kill": "Kill WorkBuddy",
        "btn.restore": "Restore this backup",
        "btn.open_store": "Open store",
        "btn.reveal_backups": "Open backups",
        "btn.export": "Export",
        "btn.import": "Import",
        "btn.copy_cmd": "Copy CLI command",
        "row.in_use": "in use",
        "row.no_private": "no private config",
        "row.cred_saved": "login saved",
        "row.cred_expired": "credentials expired",
        "row.archived": "{n} kept locally",
        "row.sessions": "{n} sessions",
        "row.memory": "memory {v}",
        "row.mcp": "{n} connectors",
        "row.last_seen": "last {t}",
        "wiz.switch_title": "Switch to “{name}”?",
        "wiz.switch_desc": "Will preserve current login → full backup → sync local data to target → write login identity → restart WorkBuddy.\nBackup tag: {tag}",
        "wiz.isolate_desc": "Isolate mode: switch identity and private config only, no session merge.",
        "wiz.has_snapshot": "A saved login session exists for this account; no re-login should be needed.",
        "wiz.no_snapshot": "No saved login session for this account; you'll need to sign in once after restart. Data will already be in place.",
        "wiz.hot_needs_login": "Hot switch does not replace the login session (cookies are locked by the running client); you'll need to sign in again.",
        "wiz.sync_title": "Sync “{src}” into current account?",
        "wiz.sync_desc": "Sessions, long-term memory and connectors will be merged into the current account ({dst}). Existing target data is preserved.",
        "wiz.delete_title": "Delete profile “{name}”?",
        "wiz.delete_desc": "Only this tool's profile is removed; WorkBuddy data on disk is untouched.",
        "wiz.kill_title": "Kill WorkBuddy process?",
        "wiz.kill_desc": "Unsaved conversations may be lost.",
        "wiz.rollback_title": "Roll back to backup {tag}?",
        "wiz.rollback_desc": "This overwrites the current database, memory, connectors and private storage. Cannot be undone.",
        "res.switched": "Switched to “{name}”",
        "res.switch_bits": "{bits}",
        "res.already": "“{name}” is already active",
        "res.synced": "Synced “{src}” → “{dst}”",
        "res.sessions": "{n} sessions migrated",
        "res.memory": "{n} memory lines merged",
        "res.connectors": "{n} connector keys merged",
        "res.automations": "{n} automations migrated",
        "res.settings": "{n} account setting(s) copied",
        "res.sessions_visible": "{n} sessions restored for this account",
        "res.sessions_restored": "{n} sessions rebuilt from local archive",
        "res.sessions_parked": "{n} other sessions kept locally (restored on switch back)",
        "res.credentials": "Login session switched (no re-login needed)",
        "res.credentials_kept": "No saved login session for this profile; re-login may be required",
        "res.login_required": "Please sign in as “{name}” once in the client. All data is already in place.",
        "res.token_expired": "Login credentials for “{name}” have expired; sign in once more. All data is already in place.",
        "res.backup": "backup {tag}",
        "res.restart_hint": "Restart WorkBuddy for changes to take effect",
        "res.rolled_back": "Rolled back to {tag}",
        "res.captured": "Login preserved: {name}",
        "res.deleted": "Deleted profile “{name}”",
        "res.renamed": "Renamed to “{name}”",
        "err.no_home": "WorkBuddy data dir not found ({path})",
        "err.no_db": "Database not found: {path}",
        "err.no_snapshot": "Account snapshot not found: {path}. Please sign in first.",
        "err.bad_snapshot": "Failed to parse account snapshot: {e}",
        "err.same": "Source and target are identical, nothing to do",
        "err.no_account": "Profile not found: {id}",
        "err.dup_login": "This login already exists as “{name}”",
        "err.no_backup": "Backup not found: {tag}",
        "err.name_empty": "Name cannot be empty",
        "err.name_taken": "Name “{name}” is taken",
        "err.switch_running": "WorkBuddy is running. Kill it first or enable hot switch.",
        "err.kill_timeout": "Timed out killing WorkBuddy",
        "err.verify": "Write verification failed, the client may have overwritten it",
        "err.no_client": "WorkBuddy client not found. Set it manually in Settings.",
        "err.busy": "Another operation is running, please wait",
        "err.no_sessions": "Source account has no sessions to migrate",
        "set.title": "Settings",
        "set.language": "Language / 语言",
        "set.client_path": "WorkBuddy client path",
        "set.pick": "Browse…",
        "set.hot_switch": "Hot switch (no process kill)",
        "set.restart_after": "Launch WorkBuddy after switch",
        "set.auto_backup": "Full backup before every switch",
        "set.auto_capture": "Auto-preserve current login (recommended)",
        "set.merge_memory": "Merge long-term memory on sync",
        "set.merge_connectors": "Merge connectors on sync",
        "set.sync_tasks": "Migrate automations on sync",
        "set.merge_settings": "Copy account-level settings on sync (channels, etc.)",
        "set.keep_sessions": "Keep sessions locally: restore this account's sessions on switch",
        "set.switch_login": "Also switch the login session (credential-level switch)",
        "set.dry_run": "Dry run (preview only, no writes)",
        "set.saved": "Settings saved",
        "bk.title": "Backups & rollback",
        "bk.tag": "Tag",
        "bk.time": "Time",
        "bk.target": "Target",
        "bk.size": "Size",
        "bk.empty": "No backups yet",
        "bk.created": "Backup created: {tag}",
        "log.title": "Activity log",
        "log.empty": "No log entries",
        "cli.usage": "WorkBuddy one-click account switch and local data sync",
        "cli.current": "Current login",
        "cli.no_accounts": "No profiles yet. Run capture first.",
        "cli.header": "  {idx:<4} {name:<24} {uid:<38} {sessions:>9} {memory:>10}",
    },
}

_current = "zh"


def detect_system_lang() -> str:
    """按系统语言猜默认语言。"""
    raw = (os.environ.get("WBSWITCH_LANG") or os.environ.get("LANG") or "").lower()
    if raw.startswith("zh"):
        return "zh"
    if platform.system() == "Windows":
        try:
            import locale

            loc = (locale.getdefaultlocale()[0] or "").lower()
            if loc.startswith("zh"):
                return "zh"
        except Exception:
            pass
    return "en" if raw and not raw.startswith("zh") else "zh"


def set_lang(lang: str | None) -> str:
    global _current
    if lang in LANGS:
        _current = lang
    return _current


def current() -> str:
    return _current


def t(key: str, **kwargs) -> str:
    """取词条并做占位符替换。缺失时回退到 key 本身，方便发现漏翻。"""
    table = _MESSAGES.get(_current) or _MESSAGES["zh"]
    text = table.get(key)
    if text is None:
        text = _MESSAGES["zh"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def load_lang_from_settings(path) -> str:
    """从 settings.json 读取语言，失败则按系统语言。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        lang = data.get("language")
        if lang in LANGS:
            return lang
    except Exception:
        pass
    return detect_system_lang()
