# -*- coding: utf-8 -*-
"""生成应用图标（工作簿风格：深色底 + 双箭头换号符号）。

输出 icon.ico（多尺寸）与 icon.png（1024），供 PyInstaller 与发布页使用。
配色取自 GUI 主题，保证观感一致。

    python tools/make_icon.py
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

BG = (10, 10, 12)            # gui.BG
PANEL = (18, 18, 22)         # gui.PANEL
ACCENT = (91, 140, 255)      # gui.ACCENT
GREEN = (62, 207, 142)       # gui.GREEN

#: 超采样倍数，先画大图再缩小，边缘更干净
SS = 8
BASE = 1024


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return m


def arrow(d: ImageDraw.ImageDraw, cx: float, cy: float, w: float, h: float,
          color, direction: str, thickness: float) -> None:
    """画一根水平箭头。direction: 'right' | 'left'。"""
    half_w, half_h = w / 2, h / 2
    if direction == "right":
        shaft = [(cx - half_w, cy - thickness / 2), (cx + half_w - h, cy - thickness / 2),
                 (cx + half_w - h, cy + thickness / 2), (cx - half_w, cy + thickness / 2)]
        head = [(cx + half_w, cy), (cx + half_w - h, cy - half_h), (cx + half_w - h, cy + half_h)]
    else:
        shaft = [(cx - half_w + h, cy - thickness / 2), (cx + half_w, cy - thickness / 2),
                 (cx + half_w, cy + thickness / 2), (cx - half_w + h, cy + thickness / 2)]
        head = [(cx - half_w, cy), (cx - half_w + h, cy - half_h), (cx - half_w + h, cy + half_h)]
    d.polygon(shaft, fill=color)
    d.polygon(head, fill=color)


def build(size: int = BASE) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角深色底 + 内层微亮面板，呼应界面里的卡片
    inset = int(s * 0.02)
    d.rounded_rectangle((inset, inset, s - inset, s - inset),
                        radius=int(s * 0.22), fill=BG)
    pad = int(s * 0.085)
    d.rounded_rectangle((pad, pad, s - pad, s - pad),
                        radius=int(s * 0.17), fill=PANEL)

    # 两条互逆箭头 = 换号
    thick = s * 0.062
    aw = s * 0.50
    ah = s * 0.155
    cx = s / 2
    arrow(d, cx, s * 0.375, aw, ah, ACCENT, "right", thick)
    arrow(d, cx, s * 0.625, aw, ah, GREEN, "left", thick)

    img = img.resize((size, size), Image.LANCZOS)
    img.putalpha(rounded_mask(size, int(size * 0.22)))
    return img


def main() -> None:
    png = ROOT / "icon.png"
    ico = ROOT / "icon.ico"

    big = build(BASE)
    big.save(png)
    print(f"写入 {png}")

    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [build(n) for n in sizes]
    frames[0].save(ico, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[1:])
    print(f"写入 {ico}  尺寸 {sizes}")


if __name__ == "__main__":
    main()
