#!/usr/bin/env python3
"""Render article diagrams from JSON manifests in conventional layouts.

Usage: python3 hft/assets/render_text_diagrams.py hft/assets/hft-02/text-diagrams.json
Requires Pillow, Noto Sans CJK fonts and rsvg-convert. Outputs SVG and PNG beside
the manifest (1280px wide by default); Markdown uses PNG for compatibility.
Only files named by a manifest's text-* IDs are written.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
from html import escape
import json
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageFont


WIDTH = 1280
MARGIN = 36
INK = "#15243b"
MUTED = "#475569"
TONES = {
    "blue": ("#eff6ff", "#2563eb"),
    "purple": ("#f5f3ff", "#7c3aed"),
    "teal": ("#f0fdfa", "#0f766e"),
    "orange": ("#fff7ed", "#c2410c"),
    "gray": ("#f8fafc", "#64748b"),
}
FONT_DIR = Path("/usr/share/fonts/opentype/noto")


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False):
    name = "NotoSansCJK-Bold.ttc" if bold else "NotoSansCJK-Regular.ttc"
    return ImageFont.truetype(str(FONT_DIR / name), size, index=2)


def text_width(text: str, size: int, bold: bool = False) -> float:
    return font(size, bold).getlength(text)


def wrap(text: str, width: float, size: int, bold: bool = False) -> list[str]:
    """Wrap by measured glyph width, including long identifiers and CJK text."""
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        tokens = re.findall(r"[A-Za-z0-9_]+(?:[-./:][A-Za-z0-9_]+)*|.", paragraph)
        for token in tokens:
            pieces = [token] if text_width(token, size, bold) <= width else list(token)
            for piece in pieces:
                if line and text_width(line + piece, size, bold) > width:
                    lines.append(line.rstrip())
                    line = piece.lstrip()
                else:
                    line += piece
        lines.append(line.rstrip())
    return lines


class Canvas:
    def __init__(self):
        self.parts: list[str] = []
        self.labels: list[dict] = []

    def rect(self, x, y, width, height, fill="white", stroke="#cbd5e1", radius=14):
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>'
        )

    def text(self, x, y, text, size=28, color=INK, bold=False, center=False):
        measured = text_width(str(text), size, bold)
        left = x - measured / 2 if center else x
        if left < 8 or left + measured > WIDTH - 8:
            raise ValueError(f"Text outside canvas: {text!r} ({left}, {measured})")
        self.labels.append({"text": str(text), "size": size, "left": left, "right": left + measured, "baseline": y})
        self.parts.append(
            f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
            f'font-weight="{700 if bold else 400}" text-anchor="{"middle" if center else "start"}">'
            f'{escape(str(text))}</text>'
        )

    def paragraph(self, x, y, text, width, size=28, color=INK, bold=False, center=False):
        lines = wrap(str(text), width, size, bold)
        leading = round(size * 1.35)
        for line in lines:
            self.text(x + width / 2 if center else x, y + size, line, size, color, bold, center)
            y += leading
        return y

    def arrow(self, x1, y1, x2, y2, color="#64748b"):
        self.parts.append(
            f'<path d="M {x1} {y1} L {x2} {y2}" fill="none" stroke="{color}" '
            'stroke-width="2.6" marker-end="url(#arrow)"/>'
        )

    def path(self, points, color="#64748b", arrow=False):
        commands = "M " + " L ".join(f"{x} {y}" for x, y in points)
        marker = 'marker-end="url(#arrow)"' if arrow else ""
        self.parts.append(
            f'<path d="{commands}" fill="none" stroke="{color}" stroke-width="2.6" '
            f'stroke-linejoin="round" {marker}/>'
        )


def card_content(node, width):
    padding = 18
    inner = width - 2 * padding
    chunks = []
    if node.get("title"):
        chunks.append(("text", node["title"], 30, True))
    body = node.get("body", [])
    if isinstance(body, str):
        body = [body]
    chunks.extend(("text", text, 28, False) for text in body)
    chunks.extend(("row", row, 28, False) for row in node.get("rows", []))
    height = padding
    for kind, data, size, bold in chunks:
        if kind == "text":
            height += len(wrap(data, inner, size, bold)) * round(size * 1.35) + 6
        else:
            if data.get("label"):
                height += len(wrap(data["label"], inner, 28)) * 38 + 8
            cells = data["cells"]
            if not 1 <= len(cells) <= 6:
                raise ValueError("Use one to six cells per row")
            gap = 30 if data.get("arrows") else 8
            cell_width = (inner - gap * (len(cells) - 1)) / len(cells)
            cell_lines = max(len(wrap(str(cell), cell_width - 12, 28)) for cell in cells)
            height += cell_lines * 38 + 20 + 12
    return chunks, max(70, height + padding - 6)


def draw_card(canvas, node, x, y, width):
    chunks, height = card_content(node, width)
    fill, accent = TONES[node.get("tone", "blue")]
    canvas.rect(x, y, width, height, fill, accent)
    cy = y + 18
    inner = width - 36
    for kind, data, size, bold in chunks:
        if kind == "text":
            cy = canvas.paragraph(x + 18, cy, data, inner, size, INK, bold) + 6
        else:
            if data.get("label"):
                cy = canvas.paragraph(x + 18, cy, data["label"], inner) + 8
            cells = data["cells"]
            gap = 30 if data.get("arrows") else 8
            cell_width = (inner - gap * (len(cells) - 1)) / len(cells)
            cell_height = max(len(wrap(str(c), cell_width - 12, 28)) for c in cells) * 38 + 20
            for i, cell in enumerate(cells):
                cx = x + 18 + i * (cell_width + gap)
                canvas.rect(cx, cy, cell_width, cell_height, "white", accent, 6)
                canvas.paragraph(cx + 6, cy + 10, str(cell), cell_width - 12, center=True)
                if data.get("arrows") and i:
                    canvas.arrow(cx - gap + 4, cy + cell_height / 2, cx - 4, cy + cell_height / 2)
            cy += cell_height + 12
    return y + height


def flow_layout(steps, x, y, width, arrows=True, columns=None):
    if columns is None:
        if width < 700:
            columns = 1
        elif any(step.get("rows") for step in steps):
            columns = min(2, len(steps))
        elif len(steps) in (5, 6, 9):
            columns = 3
        else:
            columns = min(4, len(steps))
    gap = 46
    card_width = (width - gap * (columns - 1)) / columns
    positions = []
    for start in range(0, len(steps), columns):
        row = steps[start:start + columns]
        heights = [card_content(step, card_width)[1] for step in row]
        row_height = max(heights)
        reverse = arrows and (start // columns) % 2 == 1
        for j, (step, height) in enumerate(zip(row, heights)):
            column = columns - 1 - j if reverse else j
            positions.append((x + column * (card_width + gap), y + (row_height - height) / 2, card_width, height))
        y += row_height + 58
    return positions, y - 58


def flow_height(steps, width, arrows=True):
    return flow_layout(steps, 0, 0, width, arrows)[1]


def draw_flow(canvas, steps, x, y, width, arrows=True, columns=None):
    positions, bottom = flow_layout(steps, x, y, width, arrows, columns)
    for step, (cx, cy, cw, _) in zip(steps, positions):
        draw_card(canvas, step, cx, cy, cw)
    if arrows:
        for previous, following in zip(positions, positions[1:]):
            ax, ay, aw, ah = previous
            bx, by, bw, bh = following
            if abs((ay + ah / 2) - (by + bh / 2)) < 1:
                if bx > ax:
                    canvas.arrow(ax + aw + 6, ay + ah / 2, bx - 6, by + bh / 2)
                else:
                    canvas.arrow(ax - 6, ay + ah / 2, bx + bw + 6, by + bh / 2)
            else:
                canvas.arrow(ax + aw / 2, ay + ah + 6, bx + bw / 2, by - 6)
    return bottom


def panel_height(panel, width):
    title_height = len(wrap(panel.get("title", ""), width - 32, 30, True)) * 40
    return 16 + title_height + 12 + flow_height(panel["steps"], width - 32, panel.get("arrows", True)) + 16


def draw_panel(canvas, panel, x, y, width):
    height = panel_height(panel, width)
    fill, accent = TONES[panel.get("tone", "gray")]
    canvas.rect(x, y, width, height, fill, "#cbd5e1", 16)
    cy = canvas.paragraph(x + 16, y + 16, panel.get("title", ""), width - 32, 30, accent, True) + 12
    draw_flow(canvas, panel["steps"], x + 16, cy, width - 32, panel.get("arrows", True))
    return y + height


def render(spec, output):
    global WIDTH
    WIDTH = int(spec.get("width", 1280))
    ident = spec["id"]
    if not re.fullmatch(r"text-[a-z0-9-]+", ident):
        raise ValueError(f"Invalid output id: {ident}")
    canvas = Canvas()
    y = canvas.paragraph(MARGIN, 22, spec["title"], WIDTH - 2 * MARGIN, 36, bold=True) + 24
    kind = spec.get("type", "flow")
    if kind == "flow":
        positions, _ = flow_layout(spec["steps"], MARGIN, y, WIDTH - 2 * MARGIN, columns=spec.get("columns"))
        y = draw_flow(canvas, spec["steps"], MARGIN, y, WIDTH - 2 * MARGIN, columns=spec.get("columns"))
        for edge in spec.get("returns", []):
            ax, ay, aw, ah = positions[edge["from"]]
            bx, by, bw, bh = positions[edge["to"]]
            channel_y = y + 60
            canvas.path([(ax + aw/2, ay + ah + 5), (ax + aw/2, channel_y),
                         (bx + bw/2, channel_y), (bx + bw/2, by + bh + 5)], arrow=True)
            canvas.text((ax + aw/2 + bx + bw/2)/2, channel_y + 38, edge["label"], 28, center=True)
            y = channel_y + 50
    elif kind == "panels":
        panels = spec["panels"]
        columns = min(2, len(panels))
        if len(panels) == 3:
            columns = 3
        gap = 28
        panel_width = (WIDTH - 2 * MARGIN - gap * (columns - 1)) / columns
        for start in range(0, len(panels), columns):
            row = panels[start:start + columns]
            heights = []
            for j, panel in enumerate(row):
                end = draw_panel(canvas, panel, MARGIN + j * (panel_width + gap), y, panel_width)
                heights.append(end)
            y = max(heights) + 24
        y -= 24
    elif kind == "fork":
        shared_width = 620 if len(spec["steps"]) == 1 else WIDTH - 2 * MARGIN
        shared_x = (WIDTH - shared_width) / 2
        positions, _ = flow_layout(spec["steps"], shared_x, y, shared_width)
        y = draw_flow(canvas, spec["steps"], shared_x, y, shared_width)
        px, py, pw, ph = positions[-1]
        fork_y = y + 30
        y = fork_y + 36
        branches = spec["branches"]
        gap = 32
        branch_width = (WIDTH - 2 * MARGIN - gap * (len(branches) - 1)) / len(branches)
        centers = [MARGIN + i * (branch_width + gap) + branch_width / 2 for i in range(len(branches))]
        canvas.path([(px + pw / 2, py + ph), (px + pw / 2, fork_y)])
        canvas.path([(min(centers), fork_y), (max(centers), fork_y)])
        ends = []
        for i, branch in enumerate(branches):
            canvas.arrow(centers[i], fork_y, centers[i], y - 6)
            ends.append(draw_panel(canvas, branch, MARGIN + i * (branch_width + gap), y, branch_width))
        y = max(ends)
    else:
        raise ValueError(f"Unsupported diagram type: {kind}")
    if spec.get("note"):
        y += 24
        y = canvas.paragraph(MARGIN, y, spec["note"], WIDTH - 2 * MARGIN, 28, MUTED)
    height = round(y + MARGIN)
    desc = spec.get("description", "")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img" aria-labelledby="title description">\n'
        f'<title id="title">{escape(spec["title"])}</title>\n'
        f'<desc id="description">{escape(desc)}</desc>\n'
        '<style>text { font-family: "Noto Sans CJK SC", sans-serif; }</style>\n'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto"><path d="M1,1 L9,5 L1,9 Z" '
        'fill="#64748b"/></marker></defs>\n'
        f'<rect width="{WIDTH}" height="{height}" fill="white"/>\n'
        + "\n".join(canvas.parts) + "\n</svg>\n"
    )
    svg_path = output / f"{ident}.svg"
    png_path = output / f"{ident}.png"
    svg_path.write_text(svg)
    subprocess.run(["rsvg-convert", str(svg_path), "-o", str(png_path)], check=True)
    with Image.open(png_path) as image:
        assert image.size == (WIDTH, height)
    if any(label["baseline"] + label["size"] * .25 > height - 4 for label in canvas.labels):
        raise ValueError(f"Text below image bounds in {ident}")
    return {"id": ident, "width": WIDTH, "height": height, "min_font": min(label["size"] for label in canvas.labels), "labels": len(canvas.labels)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.manifests:
        manifest = json.loads(path.read_text())
        identifiers = [spec["id"] for spec in manifest["diagrams"]]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"Duplicate figure id in {path}")
        for spec in manifest["diagrams"]:
            print(json.dumps(render(spec, path.parent), ensure_ascii=False))


if __name__ == "__main__":
    main()
