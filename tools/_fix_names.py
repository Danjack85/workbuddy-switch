# -*- coding: utf-8 -*-
"""把 i18n 里的产品名统一为 WorkBuddy Switch。"""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "wbswitch" / "i18n.py"
s = p.read_text(encoding="utf-8")
n = s.count('"W·SWITCH"')
s = s.replace('"app.name": "W·SWITCH"', '"app.name": "WorkBuddy Switch"')
p.write_text(s, encoding="utf-8")
print(f"replaced {n} occurrence(s)")
