#!/usr/bin/env python3
"""Render all SVG files under assets/hft-0X to PNG next to them.

Usage: python3 assets/render_all.py [--width 1360]
Uses rsvg-convert (already installed). Chinese fonts resolve via fontconfig
(PingFang SC / Heiti TC / Noto Sans CJK SC).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def render(svg: Path, width: int) -> None:
    png = svg.with_suffix(".png")
    r = subprocess.run(
        ["rsvg-convert", "-w", str(width), "-o", str(png), str(svg)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"rsvg-convert failed for {svg}:\n{r.stderr}")
    print(f"{svg.relative_to(ROOT)} -> {png.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=1360)
    ap.add_argument("--only", default=None, help="render only files containing this substring")
    args = ap.parse_args()
    svgs = sorted(ROOT.glob("assets/hft-*/**/*.svg"))
    if args.only:
        svgs = [s for s in svgs if args.only in s.name]
    if not svgs:
        raise SystemExit("no SVG files found")
    for svg in svgs:
        render(svg, args.width)


if __name__ == "__main__":
    main()
