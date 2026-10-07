#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成桌面端图标（纯标准库，不需要 Pillow）。

    python desktop/make_icon.py

产出 ``desktop/assets/app.ico``（6 个尺寸，供 exe 使用）与
``desktop/assets/app.png``（256px，供 README 引用）。

图形用**距离场**绘制：每个像素算出到形状边界的距离，再换算成覆盖率，
天然带抗锯齿，所以不需要超采样——16px 的小图标也能直接按目标尺寸渲染，
不会因为"先画大再缩小"把细线糊掉。
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

DESIGN = 256.0  # 所有几何都在 256×256 的设计空间里定义
SIZES = (256, 128, 64, 48, 32, 16)

BG_TOP = (37, 99, 235)      # #2563eb
BG_BOTTOM = (6, 182, 212)   # #06b6d4
AREA_ALPHA = 0.22
LINE_ALPHA = 1.0

# 一条上升的性能曲线。x 必须单调递增——填充区域是按列求折线高度算的。
POLYLINE = ((48.0, 168.0), (92.0, 124.0), (134.0, 146.0), (176.0, 88.0), (212.0, 46.0))
LINE_HALF_W = 4.4
AREA_LEFT = 48.0
AREA_RIGHT = 212.0
AREA_BOTTOM = 202.0


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _sd_round_rect(px: float, py: float, cx: float, cy: float,
                   hw: float, hh: float, r: float) -> float:
    """圆角矩形有向距离：负数在内部。"""
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    ax, ay = max(dx, 0.0), max(dy, 0.0)
    return math.hypot(ax, ay) + min(max(dx, dy), 0.0) - r


def _sd_segment(px: float, py: float, ax: float, ay: float,
                bx: float, by: float) -> float:
    """线段（胶囊形）有向距离——圆头端点是免费的。"""
    pax, pay = px - ax, py - ay
    bax, bay = bx - ax, by - ay
    denom = bax * bax + bay * bay
    h = 0.0 if denom == 0 else _clamp01((pax * bax + pay * bay) / denom)
    return math.hypot(pax - bax * h, pay - bay * h)


def _poly_y(sx: float) -> float:
    """折线在横坐标 sx 处的高度（折线 x 单调递增，所以每列唯一）。"""
    pts = POLYLINE
    if sx <= pts[0][0]:
        return pts[0][1]
    if sx >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if x0 <= sx <= x1:
            return y0 + (y1 - y0) * (sx - x0) / (x1 - x0)
    return pts[-1][1]


def render(size: int) -> bytearray:
    """按目标尺寸直接渲染 RGBA 像素，SDF 自带抗锯齿。"""
    k = size / DESIGN          # 设计空间 → 像素 的缩放比
    px = bytearray(size * size * 4)
    # 小图标下按比例缩完的线会细到看不见，给一个下限
    line_half = max(1.0, LINE_HALF_W * k)

    for y in range(size):
        sy = (y + 0.5) / k
        row = y * size * 4
        for x in range(size):
            sx = (x + 0.5) / k

            d_bg = _sd_round_rect(sx, sy, 128.0, 128.0, 122.0, 122.0, 56.0)
            a_bg = _clamp01(0.5 - d_bg * k)
            if a_bg <= 0.0:
                continue

            t = _clamp01((sy - 6.0) / 244.0)
            r = BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t
            g = BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t
            b = BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t

            # 折线下方的面积填充（先填，再描边，让线压在填充之上）
            area = _clamp01(0.5 + (sy - _poly_y(sx)) * k)
            area *= _clamp01(0.5 + (AREA_BOTTOM - sy) * k)
            area *= _clamp01(0.5 + (sx - AREA_LEFT) * k)
            area *= _clamp01(0.5 + (AREA_RIGHT - sx) * k)
            if area > 0.0:
                wa = area * AREA_ALPHA
                r = r * (1 - wa) + 255.0 * wa
                g = g * (1 - wa) + 255.0 * wa
                b = b * (1 - wa) + 255.0 * wa

            # 折线描边
            dl = min(_sd_segment(sx, sy, POLYLINE[i][0], POLYLINE[i][1],
                                 POLYLINE[i + 1][0], POLYLINE[i + 1][1])
                     for i in range(len(POLYLINE) - 1))
            wl = _clamp01(0.5 - (dl - line_half) * k)
            if wl > 0.0:
                wa = wl * LINE_ALPHA
                r = r * (1 - wa) + 255.0 * wa
                g = g * (1 - wa) + 255.0 * wa
                b = b * (1 - wa) + 255.0 * wa

            i4 = row + x * 4
            px[i4] = int(r + 0.5)
            px[i4 + 1] = int(g + 0.5)
            px[i4 + 2] = int(b + 0.5)
            px[i4 + 3] = int(a_bg * 255.0 + 0.5)
    return px


def png_encode(size: int, rgba: bytes) -> bytes:
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def ico_encode(images: list[tuple[int, bytes]]) -> bytes:
    """ICO 容器；Vista 以后允许直接内嵌 PNG，比手搓 BMP+掩码省事。"""
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, blob in images:
        dim = 0 if size >= 256 else size  # 256 在 ICO 里记作 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        blobs += blob
        offset += len(blob)
    return head + entries + blobs


def main() -> int:
    ap = argparse.ArgumentParser(description="生成桌面端图标")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "assets"),
                    help="输出目录（默认 desktop/assets）")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    images = [(s, png_encode(s, bytes(render(s)))) for s in SIZES]
    ico = ico_encode(images)
    (out / "app.ico").write_bytes(ico)
    (out / "app.png").write_bytes(dict(images)[256])

    print(f"app.ico  {len(ico):>8,} 字节  尺寸 {', '.join(str(s) for s in SIZES)}")
    print(f"app.png  {len(dict(images)[256]):>8,} 字节  256×256")
    print(f"输出目录 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
