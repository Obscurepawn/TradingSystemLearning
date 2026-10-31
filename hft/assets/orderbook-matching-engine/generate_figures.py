#!/usr/bin/env python3
"""Regenerate the figures used by 高性能OrderBook与MatchingEngine研究.md.

Usage:  python3 hft/assets/orderbook-matching-engine/generate_figures.py
Needs:  rsvg-convert on PATH; a CJK font such as PingFang SC / Noto Sans CJK SC.
Output: <name>.svg and <name>.png next to this script.
"""
from __future__ import annotations

import math
import subprocess
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCALE = 1.5  # PNG width = SVG width * SCALE

INK = "#0f172a"
TEXT = "#1e293b"
MUTED = "#64748b"
LINE = "#cbd5e1"
PANEL = "#f8fafc"
BLUE, BLUE_BG, BLUE_BD = "#2563eb", "#eff6ff", "#93c5fd"
TEAL, TEAL_BG, TEAL_BD = "#0f766e", "#f0fdfa", "#5eead4"
ORANGE, ORANGE_BG, ORANGE_BD = "#ea580c", "#fff7ed", "#fdba74"
GRAY = "#94a3b8"

FONT = '"PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif'
MONO = '"SF Mono", Menlo, Consolas, "PingFang SC", monospace'


def text_width(s: str, size: float) -> float:
    return sum(size if ord(c) > 0x2E7F else size * 0.56 for c in s)


class Fig:
    def __init__(self, w: int, h: int, title: str, subtitle: str = ""):
        self.w, self.h = w, h
        self.out: list[str] = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
            "<defs>",
        ]
        for name, color in {"blue": BLUE, "teal": TEAL, "orange": ORANGE, "gray": GRAY, "ink": TEXT}.items():
            self.out.append(
                f'<marker id="m-{name}" markerUnits="userSpaceOnUse" markerWidth="12" markerHeight="10" '
                f'refX="11" refY="5" orient="auto"><path d="M0,0 L12,5 L0,10 Z" fill="{color}"/></marker>'
            )
        self.out.append("</defs>")
        self.rect(0, 0, w, h, "#ffffff", "none", 0)
        self.text(48, 58, title, 30, INK, 700)
        if subtitle:
            self.text(48, 94, subtitle, 18, MUTED)

    # primitives -----------------------------------------------------------
    def rect(self, x, y, w, h, fill="#ffffff", stroke=LINE, rx=10, sw=1.5, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.out.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d}/>'
        )

    def text(self, x, y, s, size=17, color=TEXT, weight=400, anchor="start", family=FONT):
        self.out.append(
            f'<text x="{x}" y="{y}" font-family=\'{family}\' font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" fill="{color}">{escape(str(s))}</text>'
        )

    def line(self, pts, color=LINE, sw=1.5, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        path = "M " + " L ".join(f"{x},{y}" for x, y in pts)
        self.out.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{sw}"{d}/>')

    def arrow(self, pts, color="blue", sw=2.2, dash=None):
        c = {"blue": BLUE, "teal": TEAL, "orange": ORANGE, "gray": GRAY, "ink": TEXT}[color]
        d = f' stroke-dasharray="{dash}"' if dash else ""
        path = "M " + " L ".join(f"{x},{y}" for x, y in pts)
        self.out.append(
            f'<path d="{path}" fill="none" stroke="{c}" stroke-width="{sw}" '
            f'stroke-linejoin="round" marker-end="url(#m-{color})"{d}/>'
        )

    def tag(self, x, y, s, size=15, color=TEXT, bg="#ffffff", anchor="middle", weight=400):
        w = text_width(s, size) + 14
        x0 = x - w / 2 if anchor == "middle" else (x if anchor == "start" else x - w)
        self.rect(x0, y - size - 3, w, size + 10, bg, "none", 4)
        tx = x0 + w / 2
        self.text(tx, y, s, size, color, weight, "middle")

    def box(self, x, y, w, h, title, lines=(), fill="#ffffff", stroke=LINE, tcolor=INK,
            tsize=20, lsize=16, lcolor=MUTED, sw=1.6, top_bar=None):
        self.rect(x, y, w, h, fill, stroke, 10, sw)
        if top_bar:
            self.out.append(f'<rect x="{x}" y="{y}" width="{w}" height="6" rx="3" fill="{top_bar}"/>')
        n = len(lines)
        block = tsize + (n * (lsize + 9) if n else 0)
        ty = y + (h - block) / 2 + tsize - 2
        self.text(x + w / 2, ty, title, tsize, tcolor, 700, "middle")
        for i, s in enumerate(lines):
            self.text(x + w / 2, ty + 10 + (i + 1) * (lsize + 9) - 4, s, lsize, lcolor, 400, "middle")

    def save(self, name: str):
        self.out.append("</svg>")
        svg = ROOT / f"{name}.svg"
        png = ROOT / f"{name}.png"
        svg.write_text("\n".join(self.out), encoding="utf-8")
        subprocess.run(
            ["rsvg-convert", "-w", str(int(self.w * SCALE)), "-o", str(png), str(svg)], check=True
        )
        print(f"wrote {png.name}")


# ---------------------------------------------------------------------------
# 1. 贯穿全文的例子
# ---------------------------------------------------------------------------
def fig_running_example():
    f = Fig(1600, 870, "贯穿全文的例子：一张买单吃掉两张卖单",
            "价格时间优先、以被动订单价格成交；新命令 D = 买入 250 股，限价 10.02 元")

    def chip(x, y, label, kind="ask", w=118):
        fill, stroke, color = {
            "ask": (BLUE_BG, BLUE_BD, TEXT),
            "bid": (TEAL_BG, TEAL_BD, TEXT),
            "hot": (ORANGE_BG, ORANGE, ORANGE),
        }[kind]
        f.rect(x, y, w, 40, fill, stroke, 8, 1.6)
        f.text(x + w / 2, y + 27, label, 18, color, 600, "middle")

    def panel(x0, title, after: bool):
        f.rect(x0, 130, 660, 470, PANEL, LINE, 14)
        f.text(x0 + 24, 168, title, 22, INK, 700)
        rows = [("10.02", "ask"), ("10.01", "ask"), None, ("10.00", "bid"), ("9.99", "bid")]
        y = 196
        for row in rows:
            if row is None:
                f.line([(x0 + 24, y + 20), (x0 + 636, y + 20)], GRAY, 1.4, "6 6")
                f.tag(x0 + 330, y + 26, "价差 0.01 元", 14, MUTED, PANEL)
                y += 44
                continue
            price, side = row
            best = price in ("10.01", "10.00")
            f.text(x0 + 30, y + 34, "卖" if side == "ask" else "买", 17, MUTED)
            stroke = (ORANGE if side == "ask" else TEAL) if best else LINE
            f.rect(x0 + 70, y + 4, 110, 48, "#ffffff", stroke, 8, 2.2 if best else 1.4)
            f.text(x0 + 125, y + 35, price, 20, INK, 700, "middle", MONO)
            qx = x0 + 210
            if price == "10.02":
                chip(qx, y + 8, "C 100")
            elif price == "10.01":
                if after:
                    chip(qx, y + 8, "B 50", "hot")
                    f.text(qx + 136, y + 34, "A 已成交移出；B 剩 50，仍在队头", 15, ORANGE)
                else:
                    chip(qx, y + 8, "A 100")
                    chip(qx + 132, y + 8, "B 200")
                    f.text(qx + 268, y + 34, "← 队头在左", 14, MUTED)
            elif price == "10.00":
                chip(qx, y + 8, "E 300", "bid")
            else:
                chip(qx, y + 8, "F 500", "bid")
            y += 66
        f.text(x0 + 24, 560, "最优卖价 10.01  ·  最优买价 10.00", 17, TEXT, 600)
        f.text(x0 + 24, 584, "橙框 / 青框 = 当前最优档", 14, MUTED)

    panel(60, "撮合前", False)
    panel(880, "撮合后", True)

    f.arrow([(732, 365), (868, 365)], "orange", 4)
    f.tag(800, 335, "D 买 250", 17, ORANGE, "#ffffff", weight=700)
    f.tag(800, 400, "限价 10.02", 15, MUTED)

    f.rect(60, 630, 1480, 200, "#ffffff", LINE, 14)
    f.text(84, 668, "这一条命令产生的回报（按顺序）", 20, INK, 700)
    items = [
        ("①", "Accepted", "D 已被接受"),
        ("②", "Execution", "D ↔ A，100 股 @ 10.01"),
        ("③", "Execution", "D ↔ B，150 股 @ 10.01"),
    ]
    for i, (n, kind, desc) in enumerate(items):
        x = 84 + i * 485
        f.rect(x, 690, 455, 74, BLUE_BG if i else PANEL, BLUE_BD if i else LINE, 10)
        f.text(x + 22, 735, n, 24, BLUE, 700)
        f.text(x + 62, 722, kind, 18, INK, 700, family=MONO)
        f.text(x + 62, 748, desc, 16, TEXT)
    f.text(84, 805, "D 剩余 0，不进入订单簿；C 仍为 100 股。若随后成功撤销 B，撤掉的是剩余的 50 股。", 16, MUTED)
    f.save("book-running-example")


# ---------------------------------------------------------------------------
# 2. 交易机构的行情簿：反向 vector
# ---------------------------------------------------------------------------
def fig_reversed_vector():
    f = Fig(1600, 880, "交易机构的行情簿：把最优价放在 vector 末尾",
            "以买侧为例，典型股票每侧约 1,000 个价位；思路来自 Optiver 在 CppCon 2024 的分享，图为示意")

    def cells(x0, y, values, idx, hot=None, w=100):
        for i, (v, k) in enumerate(zip(values, idx)):
            x = x0 + i * w
            is_hot = hot is not None and i == hot
            fill = "#ffffff" if v != "…" else PANEL
            f.rect(x, y, w, 56, ORANGE_BG if is_hot else fill, ORANGE if is_hot else LINE, 0, 1.4)
            f.text(x + w / 2, y + 36, v, 19, ORANGE if is_hot else INK, 600, "middle", MONO)
            f.text(x + w / 2, y + 82, k, 14, MUTED, 400, "middle", MONO)

    # panel A
    f.rect(48, 128, 1090, 278, PANEL, LINE, 14)
    f.text(72, 164, "直觉布局：最优价在下标 0", 21, INK, 700)
    vals = ["10.00", "9.99", "9.98", "9.97", "…", "9.03", "9.02", "9.01"]
    idx = ["0", "1", "2", "3", "…", "997", "998", "999"]
    cells(250, 212, vals, idx, hot=0)
    f.rect(78, 212, 112, 56, ORANGE_BG, ORANGE, 8, 2)
    f.text(134, 248, "10.01", 19, ORANGE, 700, "middle", MONO)
    f.arrow([(190, 240), (246, 240)], "orange", 2.6)
    f.text(134, 296, "新的最优价", 14, ORANGE, 400, "middle")
    f.arrow([(300, 196), (1040, 196)], "orange", 2.2)
    f.tag(670, 192, "插入下标 0：后面约 1,000 个元素全部后移一格", 15, ORANGE, PANEL)
    f.text(72, 340, "最优档被吃光时要整体前移，出现更优价时要整体后移；", 16, TEXT)
    f.text(72, 368, "而这两件事恰好是最频繁的更新 → 搬移成本集中出现在延迟分布的尾部。", 16, TEXT)

    # panel B
    f.rect(48, 430, 1090, 278, PANEL, LINE, 14)
    f.text(72, 466, "反转布局：最优价在末尾", 21, INK, 700)
    vals = ["9.01", "9.02", "9.03", "…", "9.97", "9.98", "9.99", "10.00"]
    idx = ["0", "1", "2", "…", "996", "997", "998", "999"]
    cells(90, 514, vals, idx, hot=None)
    f.rect(890, 514, 100, 56, ORANGE_BG, ORANGE, 0, 2, "6 4")
    f.text(940, 550, "10.01", 19, ORANGE, 700, "middle", MONO)
    f.text(940, 596, "1000", 14, ORANGE, 400, "middle", MONO)
    f.arrow([(940, 488), (940, 510)], "orange", 2.4)
    f.text(940, 478, "新最优价 push_back：其他元素一个都不动", 15, ORANGE, 600, "middle")
    f.arrow([(880, 626), (540, 626)], "blue", 2.4)
    f.tag(710, 650, "查找：从末尾向前线性扫描，多数更新几步内命中", 15, BLUE, PANEL)
    f.text(72, 690, "删除最优档 = pop_back；价格与数量可再拆成两个数组，扫描时只读价格。", 16, TEXT)

    # chart
    f.rect(1170, 128, 382, 580, "#ffffff", LINE, 14)
    f.text(1192, 164, "更新落在哪一档（示意）", 19, INK, 700)
    f.text(1192, 190, "纵轴为对数刻度", 14, MUTED)
    base_y, left = 640, 1215
    f.line([(left - 10, base_y), (1530, base_y)], GRAY, 1.4)
    f.line([(left - 10, base_y), (left - 10, 215)], GRAY, 1.4)
    for k in range(10):
        hgt = 400 * math.exp(-0.32 * k)
        x = left + k * 31
        f.rect(x, base_y - hgt, 22, hgt, BLUE_BG if k else ORANGE_BG, BLUE_BD if k else ORANGE, 2, 1.2)
        f.text(x + 11, base_y + 20, str(k), 13, MUTED, 400, "middle", MONO)
    f.text(1370, base_y + 46, "距最优价的档位", 14, MUTED, 400, "middle")
    f.text(1530, 250, "多数更新发生在", 14, TEXT, 400, "end")
    f.text(1530, 270, "最优价附近", 14, TEXT, 400, "end")

    # evolution strip
    f.text(48, 752, "Optiver 分享中的迭代顺序（同一份一周真实行情回放）", 19, INK, 700)
    steps = [
        ("std::map", "节点分散，缓存局部性差"),
        ("排序 vector + lower_bound", "局部性好，但搬移造成长尾"),
        ("反转 vector", "长尾消失"),
        ("无分支二分查找", "分支误预测约减半，IPC 1.4→1.6"),
        ("从末尾线性查找", "最快，分布最窄"),
    ]
    for i, (t, d) in enumerate(steps):
        x = 48 + i * 304
        hot = i == 4
        f.rect(x, 772, 276, 84, ORANGE_BG if hot else BLUE_BG, ORANGE if hot else BLUE_BD, 10, 1.6)
        f.text(x + 138, 806, t, 17, ORANGE if hot else INK, 700, "middle", MONO if i != 3 else FONT)
        f.text(x + 138, 836, d, 14, MUTED, 400, "middle")
        if i < 4:
            f.arrow([(x + 278, 814), (x + 300, 814)], "gray", 2)
    f.save("reversed-vector")


# ---------------------------------------------------------------------------
# 3. 交易所侧：价位数组 + 位图 + 订单池 + ID 索引
# ---------------------------------------------------------------------------
def fig_dense_ladder():
    f = Fig(1600, 980, "交易所撮合簿：价位数组、非空位图、订单池与 ID 索引",
            "前收盘价 10.00 元、±10% 的主板股票：卖侧下标 i = 价格(分) − 900，共 201 档；买侧结构相同、独立维护")

    # row labels
    labels = [
        (196, "非空位图", "nonempty[]，1 位 / 档"),
        (318, "价位数组", "Level[201]，32 B / 档"),
        (590, "订单池", "OrderHot[]，64 B / 槽"),
        (800, "空闲链与", "ID 索引"),
    ]
    for y, a, b in labels:
        f.text(52, y, a, 20, INK, 700)
        f.text(52, y + 26, b, 15, MUTED)

    # level cells layout: (label, width, kind)
    cols = [
        ("0", 130), ("…", 70), ("100", 170), ("101", 170), ("102", 170), ("103", 170), ("…", 70), ("200", 130),
    ]
    xs = []
    x = 300
    for _, w in cols:
        xs.append((x, w))
        x += w

    # summary line + bit labels + bits
    f.text(300, 136, "上层摘要 summary 的第 1 位 = 1：word 1（第 64–127 档）里至少有一档非空", 15, MUTED)
    bitinfo = {"0": ("0", "w0·0"), "100": ("0", "w1·36"), "101": ("1", "w1·37"),
               "102": ("1", "w1·38"), "103": ("0", "w1·39"), "200": ("0", "w3·8")}
    for (lab, _), (x0, w) in zip(cols, xs):
        if lab == "…":
            f.rect(x0, 176, w, 46, PANEL, LINE, 0, 1.2)
            f.text(x0 + w / 2, 206, "…", 18, MUTED, 400, "middle")
            continue
        bit, pos = bitinfo[lab]
        on = bit == "1"
        f.text(x0 + w / 2, 166, pos, 13, MUTED, 400, "middle", MONO)
        f.rect(x0, 176, w, 46, BLUE_BG if on else "#ffffff", BLUE if on else LINE, 0, 1.6 if on else 1.2)
        f.text(x0 + w / 2, 207, bit, 22, BLUE if on else MUTED, 700, "middle", MONO)

    # level cells
    ly, lh = 246, 150
    content = {
        "0": ("9.00", ["跌停价", "空"]),
        "100": ("10.00", ["空（卖侧）"]),
        "101": ("10.01", ["head 17 · tail 63", "qty 300 · n 2"]),
        "102": ("10.02", ["head 90 · tail 90", "qty 100 · n 1"]),
        "103": ("10.03", ["空"]),
        "200": ("11.00", ["涨停价", "空"]),
    }
    for (lab, _), (x0, w) in zip(cols, xs):
        if lab == "…":
            f.rect(x0, ly, w, lh, PANEL, LINE, 0, 1.2)
            f.text(x0 + w / 2, ly + 80, "…", 18, MUTED, 400, "middle")
            continue
        price, lines = content[lab]
        best = lab == "101"
        filled = lab in ("101", "102")
        f.rect(x0, ly, w, lh, BLUE_BG if filled else "#ffffff", ORANGE if best else (BLUE_BD if filled else LINE),
               0, 3 if best else 1.3)
        f.text(x0 + w / 2, ly + 32, price, 21, INK, 700, "middle", MONO)
        f.text(x0 + w / 2, ly + 56, f"i = {lab}", 14, MUTED, 400, "middle", MONO)
        for k, s in enumerate(lines):
            f.text(x0 + w / 2, ly + 86 + k * 24, s, 15, TEXT if filled else MUTED, 400, "middle", MONO)
        if best:
            f.text(x0 + w / 2, ly + 138, "best_ask", 15, ORANGE, 700, "middle", MONO)

    # pool strip
    py, ph = 556, 92
    slots = [
        ("0–4", 80, "gap"), ("5", 110, "free"), ("6–8", 60, "gap"), ("9", 110, "free"), ("…", 60, "gap"),
        ("17", 160, "A"), ("18–62", 60, "gap"), ("63", 160, "B"), ("…", 40, "gap"), ("90", 160, "C"),
        ("…", 140, "gap"),
    ]
    centers = {}
    x = 300
    for lab, w, kind in slots:
        if kind == "gap":
            f.rect(x, py, w, ph, PANEL, LINE, 0, 1.2)
            f.text(x + w / 2, py + 52, lab, 13, MUTED, 400, "middle", MONO)
        elif kind == "free":
            f.rect(x, py, w, ph, "#ffffff", GRAY, 0, 1.4, "5 4")
            f.text(x + w / 2, py + 38, f"槽 {lab}", 16, MUTED, 600, "middle")
            f.text(x + w / 2, py + 64, "空闲", 15, MUTED, 400, "middle")
        else:
            info = {"A": ("seq 101 · 100", "gen 3"), "B": ("seq 104 · 200", "gen 7"), "C": ("seq 109 · 100", "gen 2")}[kind]
            f.rect(x, py, w, ph, BLUE_BG, BLUE_BD, 0, 1.6)
            f.text(x + w / 2, py + 30, f"槽 {lab} · {kind}", 17, INK, 700, "middle")
            f.text(x + w / 2, py + 56, info[0], 14, TEXT, 400, "middle", MONO)
            f.text(x + w / 2, py + 78, info[1], 14, MUTED, 400, "middle", MONO)
        centers[lab] = (x, w)
        x += w

    def cx(lab, off=0.5):
        x0, w = centers[lab]
        return x0 + w * off

    lvl = {lab: xw for (lab, _), xw in zip(cols, xs)}
    bot = ly + lh
    # Level 101 head -> slot 17
    hx = lvl["101"][0] + 30
    f.arrow([(hx, bot), (hx, 470), (cx("17"), 470), (cx("17"), py - 2)], "blue")
    f.tag(hx + 34, 446, "head", 14, BLUE, "#ffffff")
    # Level 101 tail -> slot 63
    tx = lvl["101"][0] + 140
    f.arrow([(tx, bot), (tx, 500), (cx("63"), 500), (cx("63"), py - 2)], "blue")
    f.tag(tx + 30, 488, "tail", 14, BLUE, "#ffffff")
    # Level 102 head = tail -> slot 90
    qx = lvl["102"][0] + 130
    f.arrow([(qx, bot), (qx, 440), (cx("90"), 440), (cx("90"), py - 2)], "blue")
    f.tag(qx + 70, 432, "head = tail", 14, BLUE, "#ffffff")

    # FIFO links below the strip (17 <-> 63)
    a_l, a_r = cx("17", 0.3), cx("17", 0.7)
    b_l, b_r = cx("63", 0.3), cx("63", 0.6)
    f.arrow([(a_r, py + ph), (a_r, py + ph + 34), (b_l, py + ph + 34), (b_l, py + ph + 4)], "blue", 2)
    f.tag((a_r + b_l) / 2, py + ph + 40, "next", 14, BLUE, "#ffffff")
    f.arrow([(b_r, py + ph), (b_r, py + ph + 64), (a_l, py + ph + 64), (a_l, py + ph + 4)], "blue", 2)
    f.tag((a_r + b_l) / 2, py + ph + 70, "prev", 14, BLUE, "#ffffff")

    # free list (left)
    f.text(300, 800, "free_head", 16, MUTED, 700, family=MONO)
    f.arrow([(cx("5", 0.25), 782), (cx("5", 0.25), py + ph + 4)], "gray", 2, "6 5")
    f.arrow([(cx("5", 0.75), py + ph), (cx("5", 0.75), py + ph + 46), (cx("9"), py + ph + 46), (cx("9"), py + ph + 4)],
            "gray", 2, "6 5")
    f.text(300, 826, "回收的槽位挂回空闲链，新单从链头取", 14, MUTED)

    # ID index box -> slot 63
    bx, by, bw, bh = 1100, 770, 440, 120
    f.box(bx, by, bw, bh, "订单 ID 索引（开放寻址哈希）",
          ["(会话 S1, 客户单号 7) → {slot 63, gen 7}", "撤 B：查表 → 比较 gen → 摘链"],
          fill=TEAL_BG, stroke=TEAL_BD, tsize=18, lsize=15, lcolor=TEXT)
    ix = cx("63", 0.85)
    f.arrow([(bx, by + 50), (ix, by + 50), (ix, py + ph + 4)], "teal", 2.2)

    f.text(300, 938, "价位数组按价格寻址，订单池按槽位寻址。A→B 是逻辑上的 FIFO 顺序，两张订单在内存中并不相邻。", 16, TEXT)
    f.save("dense-ladder-order-pool")


# ---------------------------------------------------------------------------
# 4. 序号进度：执行、提交、发布
# ---------------------------------------------------------------------------
def fig_cursors():
    f = Fig(1600, 900, "撮合与日志并行时，发布进度取两者的较小值",
            "输入 ring 上的四个序号进度（输出闸门方案）。输入闸门方案中，撮合线程本身等待日志提交，executed 不会超过 committed")

    x0, y0, w, h = 200, 236, 120, 70
    for i in range(10):
        seq = 1195 + i
        x = x0 + i * w
        if seq <= 1199:
            fill, stroke = ORANGE_BG, ORANGE_BD
        elif seq == 1200:
            fill, stroke = BLUE_BG, BLUE
        else:
            fill, stroke = "#ffffff", LINE
        f.rect(x, y0, w, h, fill, stroke, 0, 1.6)
        f.text(x + w / 2, y0 + 44, f"#{seq}", 19, INK, 600, "middle", MONO)

    def c(seq, off=0.0):
        return x0 + (seq - 1195) * w + w / 2 + off

    f.text(200, 172, "← 更早的槽位已被所有消费者越过，定序器可以复用", 15, MUTED)
    # above: sequencer and executed
    f.arrow([(c(1204), 196), (c(1204), y0 - 4)], "gray", 2.4)
    f.text(c(1204), 160, "定序器已写入", 16, TEXT, 600, "middle")
    f.text(c(1204), 184, "#1204", 16, MUTED, 400, "middle", MONO)
    f.arrow([(c(1200), 196), (c(1200), y0 - 4)], "blue", 2.6)
    f.text(c(1200), 160, "撮合线程已执行", 16, BLUE, 700, "middle")
    f.text(c(1200), 184, "executed = #1200", 15, BLUE, 400, "middle", MONO)
    # below: committed and publishable
    f.arrow([(c(1199, -22), 352), (c(1199, -22), y0 + h + 4)], "teal", 2.6)
    f.text(c(1199, -36), 358, "日志已提交 committed = #1199", 16, TEAL, 700, "end")
    f.text(c(1199, -36), 382, "本地落盘 + 副本确认后推进", 14, MUTED, 400, "end")
    f.arrow([(c(1199, 22), 410), (c(1199, 22), y0 + h + 4)], "orange", 2.6)
    f.text(c(1199, 36), 416, "可对外发布 = min(1200, 1199) = #1199", 16, ORANGE, 700, "start")
    f.text(c(1199, 36), 440, "#1200 的确认留在结果缓冲里，等提交追上再发", 14, MUTED, 400, "start")

    # legend
    lg = [(ORANGE_BG, ORANGE_BD, "已执行且已提交：可发布"), (BLUE_BG, BLUE, "已执行、未提交：结果暂存"),
          ("#ffffff", LINE, "已定序、尚未执行")]
    for i, (fill, stroke, s) in enumerate(lg):
        x = 200 + i * 400
        f.rect(x, 474, 34, 22, fill, stroke, 4, 1.6)
        f.text(x + 46, 492, s, 16, TEXT)

    # pipeline
    f.rect(48, 530, 1504, 290, PANEL, LINE, 14)
    f.box(72, 625, 200, 96, "接入网关", ["解码 · 准入"], tsize=19, lsize=15)
    f.box(330, 625, 220, 96, "定序器", ["分配序号 · 写入 ring"], tsize=19, lsize=15)
    f.box(650, 560, 340, 96, "撮合线程（单写者）", ["按序执行，推进 executed"], fill=BLUE_BG, stroke=BLUE_BD,
          tsize=19, lsize=15)
    f.box(650, 696, 340, 96, "日志与复制", ["落盘、发往副本，推进 committed"], fill=TEAL_BG, stroke=TEAL_BD,
          tsize=19, lsize=15)
    f.box(1100, 625, 230, 96, "发布闸门", ["放行 ≤ min(两者)"], fill=ORANGE_BG, stroke=ORANGE_BD, tsize=19, lsize=15)
    f.box(1390, 625, 140, 96, "回报 / 行情", ["按序发出"], tsize=18, lsize=15)
    f.arrow([(272, 673), (326, 673)], "ink", 2)
    f.arrow([(550, 673), (600, 673), (600, 608), (646, 608)], "blue", 2.2)
    f.arrow([(600, 673), (600, 744), (646, 744)], "teal", 2.2)
    f.arrow([(990, 608), (1045, 608), (1045, 655), (1096, 655)], "blue", 2.2)
    f.arrow([(990, 744), (1045, 744), (1045, 692), (1096, 692)], "teal", 2.2)
    f.arrow([(1330, 673), (1386, 673)], "orange", 2.4)
    f.tag(1045, 586, "结果", 14, BLUE, PANEL)
    f.tag(1045, 772, "提交序号", 14, TEAL, PANEL)
    f.text(72, 580, "同一条有序输入，两路并行消费", 16, MUTED)

    f.text(48, 860, "LMAX 与 Coinbase INTX 公开的做法是输入闸门：输入先落盘、复制（Raft 多数派确认）再执行。本图是把复制与执行重叠的输出闸门方案。",
           15, MUTED)
    f.save("matching-pipeline")


# ---------------------------------------------------------------------------
# 5. 整体架构
# ---------------------------------------------------------------------------
def fig_overview():
    f = Fig(1760, 1000, "撮合引擎整体架构（示意）",
            "按品种分片，每个分片由一个写线程修改其全部订单簿；日志复制与撮合并行；发布端按序号放行")

    # gateway / sequencer
    f.box(50, 340, 240, 190, "接入网关 ×N", ["协议解码 · 会话校验", "限流 · 准入检查", "资金额度预留"],
          tsize=20, lsize=15)
    f.box(350, 365, 220, 140, "定序器", ["分配输入序号", "按品种路由到分片"], tsize=20, lsize=15)
    f.arrow([(290, 435), (346, 435)], "blue")

    def shard(y, name, core, syms):
        f.rect(650, y, 560, 300, BLUE_BG, BLUE_BD, 14, 1.6)
        f.text(672, y + 34, f"{name} · 绑定核 {core} · 单写线程", 19, BLUE, 700)
        f.text(672, y + 60, f"品种 {syms} …", 15, MUTED)
        f.box(670, y + 100, 92, 150, "SPSC", ["ring"], tsize=16, lsize=15, lcolor=TEXT)
        f.rect(792, y + 80, 270, 196, "#ffffff", BLUE_BD, 10, 1.4)
        f.text(927, y + 106, "订单簿 × 本分片品种数", 15, INK, 700, "middle")
        for k, s in enumerate(["价位数组 + 非空位图", "订单 ID 索引", "订单池（同价 FIFO 链）"]):
            f.rect(808, y + 120 + k * 50, 238, 40, TEAL_BG if k == 2 else PANEL, TEAL_BD if k == 2 else LINE, 8, 1.2)
            f.text(927, y + 146 + k * 50, s, 15, TEXT, 400, "middle")
        f.box(1092, y + 100, 100, 150, "结果", ["缓冲"], tsize=16, lsize=15, lcolor=TEXT)
        f.arrow([(762, y + 175), (788, y + 175)], "blue", 2)
        f.arrow([(1062, y + 175), (1088, y + 175)], "blue", 2)
        return y + 175

    r0 = shard(140, "分片 0", 2, "600000、600519")
    r1 = shard(470, "分片 1", 3, "000001、300750")

    # sequencer -> rings
    f.arrow([(570, 410), (610, 410), (610, r0), (666, r0)], "blue")
    f.arrow([(570, 460), (610, 460), (610, r1), (666, r1)], "blue")

    # journal
    f.box(650, 830, 560, 110, "日志与复制（独立线程 / 进程）", ["保存完整输入序列与规则版本，推进 committed 序号"],
          fill=TEAL_BG, stroke=TEAL_BD, tsize=19, lsize=15, lcolor=TEXT)
    f.arrow([(460, 505), (460, 885), (646, 885)], "teal")
    f.tag(460, 700, "同一份有序输入", 14, TEAL, "#ffffff")

    # gate
    f.box(1290, 420, 190, 200, "按序发布", ["执行完成", "且已提交", "才对外可见"], fill=ORANGE_BG, stroke=ORANGE_BD,
          tsize=20, lsize=15, lcolor=TEXT)
    f.arrow([(1192, r0), (1250, r0), (1250, 490), (1286, 490)], "blue")
    f.arrow([(1192, r1), (1250, r1), (1250, 550), (1286, 550)], "blue")
    f.arrow([(1210, 885), (1385, 885), (1385, 624)], "teal")
    f.tag(1385, 760, "已提交序号", 14, TEAL, "#ffffff")

    # outputs
    outs = [(300, "成交回报", "订单状态"), (430, "公开行情", "逐笔 · 快照"), (560, "查询副本", "下游风控 / 清算")]
    for y, a, b in outs:
        f.box(1540, y, 172, 100, a, [b], tsize=17, lsize=14)
        f.arrow([(1480, 520), (1510, 520), (1510, y + 50), (1536, y + 50)], "orange", 2)

    # backpressure
    f.arrow([(400, 365), (400, 300), (170, 300), (170, 336)], "gray", 2, "7 5")
    f.tag(285, 296, "ring 满：背压到入口", 14, MUTED, "#ffffff")

    # legend
    lg = [("blue", "命令与结果"), ("teal", "日志与提交进度"), ("orange", "对外发布"), ("gray", "背压")]
    f.text(50, 640, "图例", 16, INK, 700)
    for i, (col, s) in enumerate(lg):
        y = 672 + i * 34
        f.arrow([(50, y), (110, y)], col, 2.4, "7 5" if col == "gray" else None)
        f.text(124, y + 6, s, 15, TEXT)
    f.text(50, 830, "并行发生在分片之间；", 15, MUTED)
    f.text(50, 854, "跨分片的资金占用在网关", 15, MUTED)
    f.text(50, 878, "或账户服务中预留。", 15, MUTED)
    f.save("engine-orderbook-overview")


if __name__ == "__main__":
    fig_running_example()
    fig_reversed_vector()
    fig_dense_ladder()
    fig_cursors()
    fig_overview()
