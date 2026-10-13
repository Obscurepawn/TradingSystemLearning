"""Render the article's two memory diagrams as SVG and PNG.

Run with Python 3. Requires rsvg-convert (librsvg) and a CJK font.
Only the four generated files named below are overwritten.
"""

from html import escape
from pathlib import Path
import subprocess


OUT = Path(__file__).resolve().parent
INK = "#15243b"
MUTED = "#596a80"
LINE = "#cdd6e2"
BLUE = "#2166c8"
BLUE_PALE = "#e8f1ff"
PURPLE = "#7046b2"
PURPLE_PALE = "#f1ebfb"
PAPER = "#f5f7fa"
WHITE = "#ffffff"


class Diagram:
    def __init__(self, width, height, title, description):
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" '
            'role="img" aria-labelledby="title description">',
            f'<title id="title">{escape(title)}</title>',
            f'<desc id="description">{escape(description)}</desc>',
            '<style>text {font-family: "Noto Sans CJK SC", "Microsoft YaHei", '
            '"PingFang SC", Arial, sans-serif;}</style>',
            '<defs>',
            f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
            f'<path d="M 1 1 L 9 5 L 1 9 Z" fill="{BLUE}"/></marker>',
            '</defs>',
        ]
        self.rect(0, 0, width, height, WHITE, "none", 0)

    def rect(self, x, y, w, h, fill=WHITE, stroke=LINE, radius=10):
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
        )

    def text(self, x, y, value, size=22, color=INK, weight=400, anchor="start"):
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{escape(str(value))}</text>'
        )

    def path(self, commands, arrow=True, dashed=False):
        marker = ' marker-end="url(#arrow)"' if arrow else ""
        dash = ' stroke-dasharray="5 5"' if dashed else ""
        self.parts.append(
            f'<path d="{commands}" fill="none" stroke="{BLUE}" stroke-width="2.4" '
            f'stroke-linejoin="round"{marker}{dash}/>'
        )

    def cell(self, x, y, w, h, label, selected=False, cold=False):
        color = BLUE if selected else MUTED
        fill = BLUE_PALE if selected else (PAPER if cold else WHITE)
        self.rect(x, y, w, h, fill, BLUE if selected else LINE, 3)
        self.text(x + w / 2, y + h / 2 + 8, label, 22, color,
                  600 if selected else 400, "middle")

    def save(self, name):
        svg = OUT / f"{name}.svg"
        png = OUT / f"{name}.png"
        svg.write_text("\n".join(self.parts + ["</svg>"]), encoding="utf-8")
        subprocess.run(
            ["rsvg-convert", "-w", "1920", str(svg), "-o", str(png)], check=True
        )
        print(f"{svg.name} -> {png.name}")


def data_layout():
    d = Diagram(
        1440, 1060, "AoS、冷热分离与 SoA 的数据布局",
        "三种布局都突出数量字段。AoS 中数量与价格、备注交错；冷热分离后备注单独存放；"
        "SoA 的 quantities 数组让数量字段连续。格子只表示布局关系，不表示字节比例。",
    )
    d.text(60, 64, "围绕访问方式组织数据", 34, weight=700)
    d.text(60, 105, "示例：扫描数量字段；蓝色格子表示本次需要读取的数据。", 22, MUTED)

    d.rect(60, 145, 1320, 190, PAPER, "none", 16)
    d.text(86, 208, "AoS", 29, weight=700)
    d.text(86, 247, "按记录排列", 22, MUTED)
    d.text(86, 280, "字段相互穿插", 20, MUTED)
    starts = (350, 686, 1022)
    for i, x in enumerate(starts):
        d.text(x + 144, 195, f"记录 {i}", 20, MUTED, anchor="middle")
        d.cell(x, 218, 96, 66, f"price {i}")
        d.cell(x + 96, 218, 96, 66, f"qty {i}", selected=True)
        d.cell(x + 192, 218, 96, 66, f"note {i}", cold=True)

    d.rect(60, 357, 1320, 282, PAPER, "none", 16)
    d.text(86, 416, "冷热分离", 29, weight=700)
    d.text(86, 455, "热字段保留 AoS", 22, MUTED)
    d.text(86, 494, "cold_index", 21, BLUE)
    d.text(86, 524, "定位冷数据", 20, MUTED)
    for i, x in enumerate(starts):
        d.text(x + 144, 397, f"热记录 {i}", 20, MUTED, anchor="middle")
        d.cell(x, 419, 96, 60, f"price {i}")
        d.cell(x + 96, 419, 96, 60, f"qty {i}", selected=True)
        d.cell(x + 192, 419, 96, 60, f"idx = {i}")
        d.path(f"M {x + 240} 484 V 506 H {x + 144} V 534", dashed=True)
        d.cell(x, 541, 288, 59, f"冷记录 {i}：note {i}", cold=True)

    d.rect(60, 661, 1320, 291, PAPER, "none", 16)
    d.text(86, 726, "SoA", 29, weight=700)
    d.text(86, 765, "按字段排列", 22, MUTED)
    d.text(86, 804, "数量连续存放", 20, BLUE, 600)
    for row, (name, field, y) in enumerate([
        ("prices", "price", 698),
        ("quantities", "qty", 776),
        ("notes", "note", 854),
    ]):
        d.text(350, y + 37, name, 22, BLUE if row == 1 else MUTED,
               600 if row == 1 else 400)
        for i in range(3):
            d.cell(512 + i * 266, y, 266, 58, f"{field} {i}",
                   selected=row == 1, cold=row == 2)

    d.text(60, 997, "示意图省略 id、history 等字段；note 代表冷字段，不展开其内部存储。", 21, MUTED)
    d.text(60, 1031, "格子只表达布局关系，不代表实际字节比例；布局选择取决于经常一起访问哪些字段。", 21, MUTED)
    d.save("01-data-layout")


def pool_panel(d, x, title, subtitle, used, free, operations):
    d.rect(x, 177, 420, 611, WHITE, LINE, 16)
    d.text(x + 24, 222, title, 27, weight=700)
    d.text(x + 24, 259, subtitle, 21, MUTED)
    d.text(x + 24, 309, "对象数组 objects_[0..3]", 21, MUTED)

    for i, occupied in enumerate(used):
        cx = x + 24 + i * 94
        d.rect(cx, 330, 86, 86, PURPLE_PALE if occupied else BLUE_PALE,
               PURPLE if occupied else BLUE, 8)
        color = PURPLE if occupied else BLUE
        d.text(cx + 43, 367, i, 27, color, 700, "middle")
        d.text(cx + 43, 397, "占用" if occupied else "空闲", 20, color, anchor="middle")

    d.text(x + 24, 475, f"空闲链表 · head = {free[0]}", 22, BLUE, 600)
    chain = free + [4]
    for i, index in enumerate(chain):
        cx = x + 24 + i * 142
        end = index == 4
        d.rect(cx, 502, 90, 77, PAPER if end else BLUE_PALE,
               LINE if end else BLUE, 8)
        d.text(cx + 45, 534, "N = 4" if end else f"槽 {index}", 22,
               MUTED if end else BLUE, 600, "middle")
        d.text(cx + 45, 561, "结束" if end else "空闲", 19,
               MUTED if end else BLUE, anchor="middle")
        if i < len(chain) - 1:
            d.path(f"M {cx + 95} 541 H {cx + 135}")

    for i, operation in enumerate(operations):
        d.text(x + 24, 647 + i * 38, operation, 21, INK)


def slot_pool():
    d = Diagram(
        1440, 958, "固定槽位池的申请与归还",
        "容量 N 等于 4。初始占用槽 0 和 2，空闲链表为 head 1 到 3 到结束标记 4。"
        "申请取走槽 1 后 head 为 3。随后归还槽 2，将它放在 head 前面，链表变成 2 到 3 到 4。",
    )
    d.text(60, 64, "固定槽位池：用索引管理空闲位置", 34, weight=700)
    d.text(60, 106, "N = 4 · 单线程 · 下方的箭头只连接空闲槽位，表示 next_[i]。", 22, MUTED)
    pool_panel(d, 60, "① 初始状态", "槽 0、2 占用；槽 1、3 空闲",
               [True, False, True, False], [1, 3],
               ["head = 1", "next[1] = 3", "next[3] = N"])
    pool_panel(d, 510, "② acquire() 取走槽 1", "返回索引 1；链表头前移到 3",
               [True, True, True, False], [3],
               ["i = head = 1", "head = next[1] = 3", "used[1] = true"])
    pool_panel(d, 960, "③ release(2) 归还槽 2", "将槽 2 放到当前链表头前面",
               [True, True, False, False], [2, 3],
               ["used[2] = false", "next[2] = head = 3", "head = 2"])

    d.rect(60, 822, 1320, 99, PAPER, "none", 12)
    d.text(84, 861, "N = 4 是结束标记，没有“槽 4”；占用槽位不属于空闲链表。", 22, INK, 600)
    d.text(84, 897, "对象始终位于固定数组中；申请、归还只更新少量索引和状态，不移动其他对象。", 22, MUTED)
    d.save("02-slot-pool")


if __name__ == "__main__":
    data_layout()
    slot_pool()
