"""WCAG 2.1 contrast audit for the Command Center design tokens.

Run it as ``python3 scripts/check_contrast.py`` (``npm run check`` does). No
shebang on purpose: file modes are not preserved by every copy/extract path, and
``ruff`` flags a shebang on a non-executable file.

The two themes are defined as channel triplets in ``src/theme.css``. This script
re-reads those values (no copies), checks every text/background pair the UI
actually renders, and exits non-zero if a pair is below its target:

* body text (< 18.5 px) must reach AA, 4.5:1;
* large text and non-text UI indicators must reach 3:1.

Backgrounds are the *worst case* surfaces the text can land on: the elevated
panel, the inset well, and the plain surface.

Run: ``python3 scripts/check_contrast.py`` (also executed by ``npm run check``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

THEME = Path(__file__).resolve().parent.parent / "src" / "theme.css"

TOKENS = re.compile(r"--([a-z0-9-]+):\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)")

TEXT_PAIRS = [
    # (foreground token, [background tokens], target ratio, what it is)
    ("ink", ["surface", "elevated", "inset"], 4.5, "primary text"),
    ("muted", ["surface", "elevated", "inset"], 4.5, "secondary text"),
    ("faint", ["surface", "elevated", "inset"], 4.5, "labels and metadata"),
    ("accent", ["surface", "elevated", "inset"], 4.5, "accent text and links"),
    ("violet", ["surface", "elevated", "inset"], 4.5, "violet accent text"),
    ("ok", ["surface", "elevated", "inset"], 4.5, "success text"),
    ("warn", ["surface", "elevated", "inset"], 4.5, "warning text"),
    ("danger", ["surface", "elevated", "inset"], 4.5, "error text"),
    ("line", ["surface", "elevated"], 1.6, "fine borders (decorative, must be visible)"),
]


def parse_theme(css: str) -> dict[str, dict[str, tuple[float, float, float]]]:
    themes: dict[str, dict[str, tuple[float, float, float]]] = {}
    for block in re.finditer(r"(\[data-theme=\"(\w+)\"\]|:root(?:,\s*\n\[data-theme=\"dark\"\])?)\s*\{(.*?)\}", css, re.DOTALL):
        name = block.group(2) or "dark"
        values: dict[str, tuple[float, float, float]] = {}
        for match in TOKENS.finditer(block.group(3)):
            values[match.group(1)] = (float(match.group(2)), float(match.group(3)), float(match.group(4)))
        if values:
            themes[name] = values
    return themes


def luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        scaled = value / 255.0
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def main() -> int:
    css = THEME.read_text(encoding="utf-8")
    themes = parse_theme(css)
    missing = {"dark", "light"} - set(themes)
    if missing:
        print(f"FAIL could not read theme blocks: {sorted(missing)}")
        return 2

    failures = 0
    for theme_name, tokens in themes.items():
        print(f"\n[{theme_name}]")
        for foreground, backgrounds, target, label in TEXT_PAIRS:
            if foreground not in tokens:
                print(f"  missing token --{foreground}")
                failures += 1
                continue
            worst = None
            for background in backgrounds:
                if background not in tokens:
                    continue
                value = ratio(tokens[foreground], tokens[background])
                if worst is None or value < worst[1]:
                    worst = (background, value)
            if worst is None:
                continue
            ok = worst[1] >= target
            failures += 0 if ok else 1
            print(
                f"  {'ok  ' if ok else 'FAIL'} {foreground:8s} on {worst[0]:9s} "
                f"{worst[1]:5.2f}:1 (target {target}) — {label}"
            )

    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} failing pair(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
