#!/usr/bin/env python3
"""Generates the Diwali lettering and rangoli SVG fixtures in examples/diwali/.

Why two single-word figures, not one two-line "HAPPY DIWALI": a drone needs
roughly 15+ points per letter to read as that letter, and the 2.0 m minimum
spacing fixes the point density. 100 drones is ~9 per letter across 11 letters
(measured: unreadable) but ~16-20 per letter across 5-6, so the greeting is two
consecutive scenes, HAPPY then DIWALI.

Studio's built-in text font is seven-segment (digits plus A B C D E F H L P U)
and caps a caption at 8 characters, so it cannot spell HAPPY DIWALI: Y, I and
W have no glyph and fall back to an "8". The SVG importer
(`svgToFormation`, shapeType "svg") is the existing Studio feature that can,
so the lettering is authored here as stroke paths and loaded through it. No
Studio engine code is touched.

Each letter is ONE `M ... L ...` contour. The importer puts every separate
contour on its own depth plane (2.5 m apart along y, the audience's line of
sight), so a letter per contour keeps the plane count - and with it the depth
spread inside the 30 m site radius - small. Letters that retrace a stroke (H, A,
Y) rely on the importer's own collision pass to hop the doubled-up samples onto
a neighbouring depth plane; from the front they still read as one stroke.

Usage, from the repo root:

    python tools/diwali_make_lettering.py
"""

from __future__ import annotations

import math
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "examples" / "diwali"

HEIGHT = 12.0   # glyph points are drawn for 8; scaled up so letters are tall and narrow
GAP = 1.5       # between letters
LINE_GAP = 6.0  # between text lines

# Strokes in glyph units, y up, origin at the letter's lower-left. Each glyph is a
# single pen path; (width, [points]).
GLYPHS: dict[str, tuple[float, list[tuple[float, float]]]] = {
    "H": (4.0, [(0, 8), (0, 0), (0, 4), (4, 4), (4, 8), (4, 0)]),
    "A": (5.0, [(0, 0), (2.5, 8), (5, 0), (4.0, 3.2), (1.0, 3.2)]),
    "P": (4.0, [(0, 0), (0, 8), (3, 8), (4, 6), (3, 4), (0, 4)]),
    "Y": (4.0, [(0, 8), (2, 4), (4, 8), (2, 4), (2, 0)]),
    "D": (4.0, [(0, 0), (0, 8), (3, 7), (4, 4), (3, 1), (0, 0)]),
    "I": (0.0, [(0, 8), (0, 0)]),
    "W": (6.0, [(0, 8), (1.5, 0), (3, 5), (4.5, 0), (6, 8)]),
    "L": (4.0, [(0, 8), (0, 0), (4, 0)]),
}


def line_width(text: str) -> float:
    return sum(GLYPHS[c][0] for c in text) + GAP * (len(text) - 1)


def line_contours(text: str, y_top: float, canvas_w: float) -> list[str]:
    """Contours for one text line, centred on the canvas, SVG y pointing down."""
    x = (canvas_w - line_width(text)) / 2
    out = []
    for ch in text:
        width, pts = GLYPHS[ch]
        d = " ".join(
            f"{'M' if i == 0 else 'L'} {x + px:.2f} {y_top + (HEIGHT - py * HEIGHT / 8.0):.2f}"
            for i, (px, py) in enumerate(pts)
        )
        out.append(d)
        x += width + GAP
    return out


def lettering_svg(lines: list[str], title: str) -> str:
    canvas_w = max(line_width(t) for t in lines) + 4.0
    canvas_h = len(lines) * HEIGHT + (len(lines) - 1) * LINE_GAP
    paths = []
    for i, text in enumerate(lines):
        y_top = i * (HEIGHT + LINE_GAP)
        for d in line_contours(text, y_top, canvas_w):
            paths.append(f'  <path d="{d}" fill="none" stroke="currentColor" stroke-width="1" />')
    body = "\n".join(paths)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas_w:.1f} {canvas_h:.1f}" '
        f'width="{canvas_w:.0f}" height="{canvas_h:.0f}">\n  <!-- {title}: one contour per letter -->\n{body}\n</svg>\n'
    )


def rangoli_svg(petals: int = 8, outer: float = 50.0, inner: float = 20.0) -> str:
    """A flower rangoli: one closed petal outline with `petals` lobes, plus a ring.

    Two contours (petal outline, centre ring) so the importer keeps them 2.5 m
    apart in depth rather than interleaving them.
    """
    cx = cy = 50.0
    pts = []
    steps = petals * 16
    for k in range(steps + 1):
        a = 2 * math.pi * k / steps
        r = inner + (outer - inner) * (0.5 + 0.5 * math.cos(petals * a)) ** 1.5
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    petal_d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in pts)
    ring_r = inner * 0.55
    ring = [(cx + ring_r * math.cos(2 * math.pi * k / 32), cy + ring_r * math.sin(2 * math.pi * k / 32)) for k in range(33)]
    ring_d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in ring)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">\n'
        "  <!-- Rangoli: 8-petal flower outline and a centre ring -->\n"
        f'  <path d="{petal_d}" fill="none" stroke="currentColor" stroke-width="2" />\n'
        f'  <path d="{ring_d}" fill="none" stroke="currentColor" stroke-width="2" />\n'
        "</svg>\n"
    )


def diya_festive_svg(halo_r: float = 46.0) -> str:
    """The stock diya_flame.svg lamp and flame, plus a halo ring around them.

    The stock diya's outline is too short to hold 2.0 m at 90+ drones (it is
    refused at 90, 100 and 120), so the 100-drone show uses this variant: the
    two original paths verbatim and a third contour for the halo.
    """
    cx, cy = 50.0, 46.0
    halo = [(cx + halo_r * math.cos(2 * math.pi * k / 48), cy + halo_r * math.sin(2 * math.pi * k / 48)) for k in range(49)]
    halo_d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in halo)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">\n'
        "  <!-- Festival diya with halo: lamp and flame from diya_flame.svg, plus a halo ring -->\n"
        '  <path d="M 20 60 Q 50 90 80 60 Q 50 70 20 60 Z" fill="none" stroke="currentColor" stroke-width="3" />\n'
        '  <path d="M 50 55 Q 35 40 50 15 Q 65 40 50 55 Z" fill="none" stroke="currentColor" stroke-width="3" />\n'
        f'  <path d="{halo_d}" fill="none" stroke="currentColor" stroke-width="2" />\n'
        "</svg>\n"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "happy.svg": lettering_svg(["HAPPY"], "HAPPY"),
        "diwali.svg": lettering_svg(["DIWALI"], "DIWALI"),
        "rangoli.svg": rangoli_svg(),
        "diya_festive.svg": diya_festive_svg(),
    }
    for name, text in files.items():
        (OUT_DIR / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()
