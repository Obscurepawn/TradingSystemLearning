#!/usr/bin/env python3
"""Generate figures for 程序性能建模的科学方法.md.

Outputs SVG + PNG (via rsvg-convert) into this directory.
Style mirrors the existing hft assets: white background, PingFang/Noto CJK,
ink #15243b, blue/orange/green/red semantic palette.

All numbers drawn here are the model inputs/outputs derived in the article
(Intel Skylake client @ 4.0 GHz unless stated). Nothing here is measured.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent

FONT = '"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif'
INK = "#15243b"
MUTED = "#64748b"
FAINT = "#94a3b8"

BLUE = "#2563eb"; BLUE_BG = "#eff6ff"; BLUE_BD = "#93c5fd"; BLUE_LT = "#bfdbfe"
ORANGE = "#c2410c"; ORANGE_BG = "#fff7ed"; ORANGE_BD = "#fdba74"; ORANGE_LT = "#fed7aa"
GREEN = "#059669"; GREEN_BG = "#f0fdf4"; GREEN_BD = "#6ee7b7"; GREEN_LT = "#a7f3d0"
RED = "#dc2626"; RED_BG = "#fef2f2"; RED_BD = "#fca5a5"; RED_LT = "#fecaca"
PURPLE = "#7c3aed"; PURPLE_BG = "#f5f3ff"; PURPLE_BD = "#c4b5fd"; PURPLE_LT = "#ddd6fe"
GRAY_BG = "#f8fafc"; GRAY_BD = "#e5e7eb"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self, w: int, h: int, title: str, subtitle: str = ""):
        self.w, self.h = w, h
        self.p: list[str] = []
        self.p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">')
        self.p.append(
            "<defs>"
            f'<marker id="ab" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 Z" fill="{BLUE}"/></marker>'
            f'<marker id="ao" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 Z" fill="{ORANGE}"/></marker>'
            f'<marker id="ag" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8 Z" fill="{MUTED}"/></marker>'
            "</defs>")
        self.p.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>')
        self.p.append(f'<text x="44" y="50" font-family={FONT!r} font-size="27" font-weight="600" fill="{INK}">{esc(title)}</text>')
        if subtitle:
            self.p.append(f'<text x="44" y="82" font-family={FONT!r} font-size="16" fill="{MUTED}">{esc(subtitle)}</text>')

    def text(self, x, y, s, size=16, bold=False, color=INK, anchor="start"):
        fw = "600" if bold else "400"
        self.p.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family={FONT!r} font-size="{size}" font-weight="{fw}" '
            f'fill="{color}" text-anchor="{anchor}">{esc(s)}</text>')

    def rect(self, x, y, w, h, fill="#fff", bd=GRAY_BD, bw=1.5, rx=8, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        stroke = f'stroke="{bd}" stroke-width="{bw}"' if bd else 'stroke="none"'
        self.p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" {stroke}{d}/>')

    def line(self, x1, y1, x2, y2, color=FAINT, bw=1.6, dash=None, marker=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if marker else ""
        self.p.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{bw}"{d}{m}/>')

    def path(self, d, fill="none", color=BLUE, bw=2, dash=None, marker=None):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#{marker})"' if marker else ""
        self.p.append(f'<path d="{d}" fill="{fill}" stroke="{color}" stroke-width="{bw}"{da}{m}/>')

    def save(self, name):
        self.p.append("</svg>")
        sp = HERE / f"{name}.svg"
        sp.write_text("\n".join(self.p), encoding="utf-8")
        r = subprocess.run(["rsvg-convert", "-w", "1360", "-o", str(HERE / f"{name}.png"), str(sp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"rsvg failed {name}: {r.stderr}")
        print(f"ok {name}")


def log_map(v, vmin, vmax, a, b):
    return a + (math.log10(v) - math.log10(vmin)) / (math.log10(vmax) - math.log10(vmin)) * (b - a)


# ---------------------------------------------------------------- fig 1
def fig_method_loop():
    s = Svg(1280, 480, "性能建模的闭环：先算出预测值，再用测量去证伪它",
            "预测与实测相差超过 20%，通常不是测量出错，而是模型漏了一项：回到第②步把它找出来")
    boxes = [
        ("①", "界定问题", ["单次延迟还是吞吐？", "机器、频率、输入分布", "缓存是冷还是热"]),
        ("②", "读汇编、数操作", ["以编译器输出为准", "按端口/资源归类", "找循环携带依赖"]),
        ("③", "定位数据层级", ["工作集对比各级缓存", "顺序 / 随机 / 依赖", "页大小与 TLB"]),
        ("④", "查代价表", ["延迟、倒数吞吐", "带宽、并发上限", "误预测等惩罚"]),
        ("⑤", "合成并预测", ["max(依赖链, 资源)", "+ Σ 事件 × 惩罚", "给区间和主导项"]),
        ("⑥", "测量对照", ["cycles、IPC", "miss、误预测计数", "主导项是否吻合"]),
    ]
    x, y, w, h, gap = 40, 120, 180, 170, 24
    for i, (num, title, lines) in enumerate(boxes):
        bx = x + i * (w + gap)
        fill, bd, col = (GREEN_BG, GREEN_BD, GREEN) if i == 5 else (BLUE_BG, BLUE_BD, BLUE)
        s.rect(bx, y, w, h, fill, bd, 1.8, 10)
        s.text(bx + 14, y + 36, num, 22, True, col)
        s.text(bx + 44, y + 36, title, 18, True, INK)
        for j, ln in enumerate(lines):
            s.text(bx + 14, y + 78 + j * 28, ln, 15, color=INK)
        if i < 5:
            s.line(bx + w + 2, y + h / 2, bx + w + gap - 3, y + h / 2, BLUE, 2.2, marker="ab")
    x6 = x + 5 * (w + gap) + w / 2
    x2 = x + 1 * (w + gap) + w / 2
    yb, yl = y + h, 362
    s.path(f"M{x6:.1f},{yb + 2} L{x6:.1f},{yl} L{x2:.1f},{yl} L{x2:.1f},{yb + 12}",
           color=ORANGE, bw=2.2, dash="7 5", marker="ao")
    s.text((x2 + x6) / 2, yl - 12, "偏差 > 20%：回到②，常见漏项是频率、TLB、端口冲突、编译器改写了代码",
           16, True, ORANGE, "middle")
    s.text((x2 + x6) / 2, 420, "偏差 ≤ 20% 且主导项吻合：这个模型可以拿来比较方案、做设计取舍",
           16, True, GREEN, "middle")
    s.save("pm-01-method-loop")


# ---------------------------------------------------------------- fig 2
def fig_latency_ladder():
    s = Svg(1280, 800, "基本操作的代价阶梯（Intel Skylake 客户端，4.0 GHz）",
            "对数横轴，单位为核心周期（1 ns = 4 周期）；浅色段表示随条件变化的范围，数据来源见正文代价表")
    C, M, B, S, O = (BLUE, BLUE_LT), (GREEN, GREEN_LT), (ORANGE, ORANGE_LT), (RED, RED_LT), (PURPLE, PURPLE_LT)
    rows = [
        ("整数加 / 比较", 1, 1, C, "1 周期 · 0.25 ns"),
        ("64 位整数乘", 3, 3, C, "3 周期 · 0.75 ns"),
        ("浮点加 / 乘 / FMA", 4, 4, C, "4 周期 · 1 ns"),
        ("L1 命中", 4, 5, M, "4–5 周期 · 1–1.25 ns"),
        ("L2 命中", 12, 12, M, "12 周期 · 3 ns"),
        ("双精度浮点除", 13, 14, C, "13–14 周期 · 3.3–3.5 ns"),
        ("分支误预测", 16.5, 20, B, "16.5–20 周期 · 4.1–5 ns"),
        ("lock xadd（无竞争）", 18, 18, S, "18 周期 · 4.5 ns"),
        ("64 位整数除", 35, 88, C, "35–88 周期 · 8.8–22 ns"),
        ("L3 命中", 42, 42, M, "42 周期 · 10.5 ns"),
        ("核间缓存行传递", 80, 200, S, "20–50 ns（桌面 / 服务器）"),
        ("DRAM（2 MB 页）", 246, 255, M, "246–255 周期 · 61–64 ns"),
        ("系统调用（getpid 级）", 380, 1120, O, "95–280 ns"),
        ("线程上下文切换", 4800, 8800, O, "1.2–2.2 µs"),
    ]
    x0, x1 = 300, 1010
    vmin, vmax = 0.5, 10000
    y0, rh = 118, 38
    X = lambda v: log_map(v, vmin, vmax, x0, x1)
    for t in [1, 10, 100, 1000, 10000]:
        xx = X(t)
        s.line(xx, y0 - 6, xx, y0 + len(rows) * rh, GRAY_BD, 1.2)
        s.text(xx, y0 + len(rows) * rh + 24, f"{t:,}", 14, color=MUTED, anchor="middle")
    s.text((x0 + x1) / 2, y0 + len(rows) * rh + 50, "核心周期（对数轴）", 15, color=MUTED, anchor="middle")
    for i, (lab, lo, hi, (dark, light), note) in enumerate(rows):
        yy = y0 + i * rh
        s.text(x0 - 14, yy + 24, lab, 16, color=INK, anchor="end")
        s.rect(x0, yy + 8, X(lo) - x0, 22, dark, None, 0, 3)
        if hi > lo:
            s.rect(X(lo), yy + 8, X(hi) - X(lo), 22, light, None, 0, 3)
        s.text(X(hi) + 10, yy + 25, note, 14, color=MUTED)
    ly = 768
    items = [("计算", BLUE), ("访存", GREEN), ("分支", ORANGE), ("同步", RED), ("操作系统", PURPLE)]
    lx = 300
    for name, col in items:
        s.rect(lx, ly - 13, 18, 16, col, None, 0, 3)
        s.text(lx + 26, ly, name, 15, color=INK)
        lx += 120
    s.save("pm-02-latency-ladder")


# ---------------------------------------------------------------- fig 3
def fig_composition():
    s = Svg(1280, 600, "三种合成方式：依赖链相加，独立操作取最紧资源，内存请求靠并发摊薄",
            "同样是“很多次操作”，能否重叠执行决定了总时间是相加还是取最大值")
    panels = [(40, "① 依赖链：延迟相加"), (460, "② 独立操作：按吞吐重叠"), (880, "③ 内存级并行：Little 定律")]
    for px, title in panels:
        s.rect(px, 106, 380, 300, GRAY_BG, GRAY_BD, 1.2, 10)
        s.text(px + 18, 138, title, 18, True, INK)
    # panel 1: 4 dependent adds, 4 cycles each
    px = 40 + 24
    for i in range(4):
        s.rect(px + i * 76, 162 + i * 40, 72, 28, BLUE, None, 0, 4)
        s.text(px + i * 76 + 36, 162 + i * 40 + 19, "fadd", 13, True, "#ffffff", "middle")
        if i < 3:
            s.line(px + i * 76 + 72, 176 + i * 40, px + (i + 1) * 76 + 2, 176 + (i + 1) * 40, MUTED, 1.4, marker="ag")
    s.line(px, 334, px + 310, 334, MUTED, 1.4, marker="ag")
    s.text(px + 310, 352, "时间", 13, color=MUTED, anchor="end")
    s.text(60, 380, "s += a[i]：每次等上一次结果", 15, color=INK)
    s.text(60, 400, "4 次 × 4 周期 = 16 周期", 15, True, BLUE)
    # panel 2: 8 independent adds, 2 ports -> start every 0.5 cycle
    px = 460 + 24
    for i in range(8):
        s.rect(px + i * 9.5, 160 + i * 20, 76, 15, GREEN, None, 0, 3)
    s.line(px, 334, px + 310, 334, MUTED, 1.4, marker="ag")
    s.text(px + 310, 352, "时间", 13, color=MUTED, anchor="end")
    s.text(px + 170, 200, "每个仍需 4 周期", 14, color=MUTED)
    s.text(px + 170, 222, "但每周期能开始 2 个", 14, color=MUTED)
    s.text(480, 380, "8 次独立加法：2 个浮点端口", 15, color=INK)
    s.text(480, 400, "约 4 + 3.5 = 7.5 周期，稳态 0.5 周期/次", 15, True, GREEN)
    # panel 3: 10 concurrent misses
    px = 880 + 24
    for i in range(10):
        s.rect(px + i * 3, 158 + i * 16, 300, 12, ORANGE, None, 0, 3)
    s.line(px, 334, px + 330, 334, MUTED, 1.4, marker="ag")
    s.text(px + 330, 352, "时间", 13, color=MUTED, anchor="end")
    s.text(900, 380, "10 个独立 miss 同时在途，每个 64 ns", 15, color=INK)
    s.text(900, 400, "平均 64 ÷ 10 ≈ 6.4 ns/次", 15, True, ORANGE)
    # formula
    s.rect(40, 430, 1200, 140, BLUE_BG, BLUE_BD, 1.6, 10)
    s.text(640, 474, "T ≈ max( 关键依赖链 , 前端宽度 , 各执行端口 , 各级带宽 ) + Σ 事件次数 × 单次惩罚",
           21, True, INK, "middle")
    s.text(640, 512, "并发上限 = min( 程序里真正独立的请求数 , 填充缓冲区数 ≈ 10–12 , ROB / load buffer 能容纳的请求数 )",
           16, color=INK, anchor="middle")
    s.text(640, 546, "分支误预测、系统调用、上下文切换会清空或打断流水线，不能被重叠，只能相加",
           16, color=MUTED, anchor="middle")
    s.save("pm-03-composition")


# ---------------------------------------------------------------- fig 4
def fig_sum_levels():
    s = Svg(1280, 640, "例 1 预测：数组求和每个元素的周期数，瓶颈随数据规模迁移",
            "Skylake 4.0 GHz 模型计算值；对数纵轴；浅色段为区间上沿。同一段源码，-ffast-math 的收益从 16× 缩到 3.3×")
    x0, x1, yt, yb = 120, 1220, 130, 500
    vmin, vmax = 0.1, 10
    Y = lambda v: yb - log_map(v, vmin, vmax, 0, yb - yt)
    for t, lab in [(0.1, "0.1"), (0.25, "0.25"), (0.5, "0.5"), (1, "1"), (2, "2"), (4, "4"), (10, "10")]:
        yy = Y(t)
        s.line(x0, yy, x1, yy, GRAY_BD, 1)
        s.text(x0 - 10, yy + 5, lab, 13, color=MUTED, anchor="end")
    s.text(x0 - 60, yt - 18, "周期 / 元素", 14, color=MUTED)
    groups = [
        ("L1：16 KB（n = 2 048）", [(4, 4), (0.25, 0.25), (0.125, 0.125)], "依赖链 / 加载端口", "16×"),
        ("L2：128 KB（n = 16 384）", [(4, 4), (0.25, 0.30), (0.20, 0.25)], "L2→L1 带宽", "13–16×"),
        ("L3：4 MB（n = 2^19）", [(4, 4), (0.4625, 0.4625), (0.4625, 0.4625)], "L3 带宽", "8.6×"),
        ("DRAM：1 GiB（n = 2^27）", [(4, 4), (1.23, 1.23), (1.23, 1.23)], "DRAM 带宽 26 GB/s", "3.3×"),
    ]
    series = [("严格顺序（-O2）", ORANGE, ORANGE_LT), ("4 累加器 SIMD（-O3 -ffast-math）", BLUE, BLUE_LT),
              ("8 累加器 SIMD（手写）", GREEN, GREEN_LT)]
    gw = (x1 - x0) / 4
    bw = 62
    for gi, (gname, vals, neck, speed) in enumerate(groups):
        gx = x0 + gi * gw + (gw - 3 * bw - 2 * 12) / 2
        for si, (lo, hi) in enumerate(vals):
            bx = gx + si * (bw + 12)
            dark, light = series[si][1], series[si][2]
            s.rect(bx, Y(lo), bw, yb - Y(lo), dark, None, 0, 3)
            if hi > lo:
                s.rect(bx, Y(hi), bw, Y(lo) - Y(hi), light, None, 0, 3)
            lab = f"{lo:g}" if hi == lo else f"{lo:g}–{hi:g}"
            s.text(bx + bw / 2, Y(hi) - 8, lab, 13, True, dark, "middle")
        cx = x0 + gi * gw + gw / 2
        s.text(cx, yb + 28, gname, 15, True, INK, "middle")
        s.text(cx, yb + 52, f"主导项：{neck}", 14, color=MUTED, anchor="middle")
        s.text(cx, yb + 76, f"严格 → 4 累加器：{speed}", 14, True, BLUE, "middle")
    lx = 160
    for name, col, _ in series:
        s.rect(lx, 104, 18, 14, col, None, 0, 3)
        s.text(lx + 26, 116, name, 14, color=INK)
        lx += 330
    s.save("pm-04-sum-levels")


# ---------------------------------------------------------------- fig 5
def fig_binary_search():
    s = Svg(1280, 600, "例 5 预测：二分查找每次查询的周期数，分支与无分支在 L3 附近交叉",
            "Skylake 4.0 GHz，随机查询、热点上层常驻缓存的理想化假设；对数纵轴")
    x0, x1, yt, yb = 130, 1220, 130, 460
    vmin, vmax = 40, 2000
    Y = lambda v: yb - log_map(v, vmin, vmax, 0, yb - yt)
    for t in [50, 100, 200, 500, 1000, 2000]:
        yy = Y(t)
        s.line(x0, yy, x1, yy, GRAY_BD, 1)
        s.text(x0 - 10, yy + 5, f"{t:,}", 13, color=MUTED, anchor="end")
    s.text(x0 - 70, yt - 18, "周期 / 次查询", 14, color=MUTED)
    groups = [
        ("N = 2^12（16 KB，全部在 L1）", 84, 129, "无分支快 1.5×", BLUE),
        ("N = 2^20（4 MB，落在 L3 内）", 309, 299.5, "基本持平", MUTED),
        ("N = 2^24（64 MB，超出 L3）", 1037, 692.5, "有分支快 1.5×：推测执行把 miss 重叠了", ORANGE),
    ]
    gw = (x1 - x0) / 3
    bw = 90
    for gi, (gname, nb, br, verdict, vcol) in enumerate(groups):
        gx = x0 + gi * gw + (gw - 2 * bw - 20) / 2
        for si, (v, col) in enumerate([(nb, BLUE), (br, ORANGE)]):
            bx = gx + si * (bw + 20)
            s.rect(bx, Y(v), bw, yb - Y(v), col, None, 0, 4)
            s.text(bx + bw / 2, Y(v) - 26, f"{v:g} 周期", 14, True, col, "middle")
            s.text(bx + bw / 2, Y(v) - 8, f"≈ {v / 4:.0f} ns", 13, color=MUTED, anchor="middle")
        cx = x0 + gi * gw + gw / 2
        s.text(cx, yb + 30, gname, 15, True, INK, "middle")
        s.text(cx, yb + 56, verdict, 15, True, vcol, "middle")
    lx = 420
    for name, col in [("无分支（cmov）：每层 = 访存延迟 + 2", BLUE), ("有分支：每层 ≈ 0.5 ×（访存延迟 + 16.5）", ORANGE)]:
        s.rect(lx, 104, 18, 14, col, None, 0, 3)
        s.text(lx + 26, 116, name, 14, color=INK)
        lx += 400
    s.save("pm-05-binary-search")


# ---------------------------------------------------------------- fig 6
def fig_tick_path():
    s = Svg(1280, 520, "例 6 预测：同一段行情处理代码，热路径约 42 ns，冷路径约 218 ns",
            "关键路径上各类代价的周期数（Skylake 4.0 GHz）；冷路径里内存 miss 与跨核交接合计占 91%")
    x0, scale = 150, 1.05
    cats = [("内存访问", GREEN), ("计算（解析、哈希、除法、microprice）", BLUE), ("分支误预测", ORANGE), ("跨核交接", RED)]
    bars = [
        ("热路径", [57, 31, 0, 80], "168 周期 ≈ 42 ns"),
        ("冷路径", [594, 62, 16.5, 200], "872.5 周期 ≈ 218 ns"),
    ]
    for bi, (name, vals, total) in enumerate(bars):
        yy = 150 + bi * 120
        s.text(x0 - 16, yy + 32, name, 18, True, INK, "end")
        cx = x0
        for ci, v in enumerate(vals):
            if v <= 0:
                continue
            w = v * scale
            s.rect(cx, yy, w, 48, cats[ci][1], None, 0, 4)
            if w >= 46:
                s.text(cx + w / 2, yy + 30, f"{v:g}", 15, True, "#ffffff", "middle")
            cx += w
        s.text(cx + 14, yy + 31, total, 16, True, INK)
    # small-segment callouts for cold bar
    cold_y = 270
    s.text(x0 + (594 + 62 + 8) * scale, cold_y + 72, "误预测 16.5", 13, color=ORANGE, anchor="middle")
    lx = 150
    for name, col in cats:
        s.rect(lx, 408, 18, 16, col, None, 0, 3)
        s.text(lx + 26, 421, name, 15, color=INK)
        lx += 70 + len(name) * 15
    s.text(150, 470, "冷路径 = 报文缓冲 DRAM、ID 表 L3、订单簿 L3、仓位 DRAM、64 位除法、信号分支猜错一次、服务器级跨核 50 ns",
           15, color=MUTED)
    s.text(150, 496, "热路径 = 报文缓冲 L3、ID 表与订单簿与仓位都在 L1、乘以预存倒数、分支全部猜对、桌面级跨核 20 ns",
           15, color=MUTED)
    s.save("pm-06-tick-path")


if __name__ == "__main__":
    fig_method_loop()
    fig_latency_ladder()
    fig_composition()
    fig_sum_levels()
    fig_binary_search()
    fig_tick_path()
