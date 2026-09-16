# -*- coding: utf-8 -*-
"""工具设置：~/.workbuddy-switch/settings.json

与 zcode-switch 的 Settings 结构对齐，去掉与 WorkBuddy 无关的项
（没有 OAuth 登录、没有额度/领取），换成 WorkBuddy 场景需要的开关。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from . import paths, profiles


@dataclass
class Settings:
    # 客户端
    client_path: str = ""
    # 行为
    hot_switch: bool = False          # 热切换：不结束进程直接换号
    restart_after_switch: bool = True  # 切换后自动启动 WorkBuddy
    close_to_tray: bool = True
    # 安全
    auto_backup: bool = True          # 切换前自动全量备份
    auto_capture: bool = True         # 自动保全当前登录
    # 同步范围
    merge_memory: bool = True
    merge_connectors: bool = True
    merge_automations: bool = True
    merge_settings: bool = True        # settings.json 里按账号隔离的段落
    # 会话档案库：会话按账号长期留存本地，登录某账号时自动恢复该账号的会话
    keep_sessions: bool = True
    # 凭据级换号：把目标账号的 Cookie 写回客户端，省掉重新登录
    switch_login_state: bool = True
    # 演练
    dry_run: bool = False
    # 界面
    language: str = ""

    @classmethod
    def load(cls) -> "Settings":
        raw = profiles.read_json_safe(paths.settings_file())
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        paths.ensure_store_dirs()
        profiles.atomic_write_json(paths.settings_file(), asdict(self))

    def set_language(self, lang: str) -> None:
        self.language = lang
        self.save()


def load() -> Settings:
    return Settings.load()


def save(s: Settings) -> None:
    s.save()
