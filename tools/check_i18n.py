#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查中英词条是否对齐（缺键 / 多键）。

这类双语项目最容易出的问题：加了新文案只补了中文，英文界面就露出原始 key。
CI 里跑这个脚本，缺漏时直接失败。

    python tools/check_i18n.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wbswitch import i18n  # noqa: E402

from wbswitch.console import make_console_safe  # noqa: E402

make_console_safe()


def main() -> int:
    zh_table = i18n._MESSAGES.get("zh", {})
    en_table = i18n._MESSAGES.get("en", {})
    zh_keys = set(zh_table)
    en_keys = set(en_table)

    only_zh = sorted(zh_keys - en_keys)
    only_en = sorted(en_keys - zh_keys)

    print(f"zh 词条 {len(zh_keys)} 条，en 词条 {len(en_keys)} 条")

    ok = True
    if only_zh:
        ok = False
        print(f"\n[!] 英文缺少 {len(only_zh)} 个键：")
        for k in only_zh:
            print(f"    {k}")
    if only_en:
        ok = False
        print(f"\n[!] 中文缺少 {len(only_en)} 个键：")
        for k in only_en:
            print(f"    {k}")

    # 占位符一致性：同一个 key 的中英必须用同一组 {placeholder}
    import re

    ph = re.compile(r"\{(\w+)\}")
    mismatch = []
    for k in sorted(zh_keys & en_keys):
        a = set(ph.findall(zh_table[k]))
        b = set(ph.findall(en_table[k]))
        if a != b:
            mismatch.append((k, sorted(a), sorted(b)))
    if mismatch:
        ok = False
        print(f"\n[!] 占位符不一致 {len(mismatch)} 个：")
        for k, a, b in mismatch:
            print(f"    {k}: zh={a} en={b}")

    if ok:
        print("\n中英词条一致")
        return 0
    print("\n中英词条不一致")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
