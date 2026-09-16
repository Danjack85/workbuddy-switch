# -*- coding: utf-8 -*-
"""控制台输出兜底。

本工具的输出里有大量中文。Windows 的控制台默认代码页因地区而异
（简体中文 cp936、西欧 cp1252、日文 cp932 …）。当代码页装不下某个字符时，
`print` 会抛 `UnicodeEncodeError` 直接崩掉 —— 例如在 cp1252 的机器上运行
`workbuddy-switch state`，一句中文就能让整个命令失败。

这里不强行改用 UTF-8（那在 cp936 终端上反而会变乱码），而是保留控制台原有
编码、只把错误处理改成 `replace`：装不下的字符退化为 `?`，但命令能正常跑完。
需要完整 UTF-8 输出的场景（如 CI 日志）用 `PYTHONIOENCODING=utf-8` 覆盖即可。

`reconfigure` 是 Python 3.7+ 的 TextIOWrapper 方法；被重定向成管道、
或者在冻结尾包里 stdout 为空对象时它会不可用，因此一律静默跳过。
"""

from __future__ import annotations

import sys


def make_console_safe() -> None:
    """把标准输出/错误流调成"绝不因编码崩掉"。幂等，可重复调用。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            # 不是 TextIOWrapper（管道/包装对象），或该实现不支持 reconfigure
            pass
