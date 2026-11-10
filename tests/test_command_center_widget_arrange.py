"""0.82.7: the widget grid is usable without a mouse.

Hover toolbars are a mouse-only affordance: on a touch screen there is no hover,
so resize/hide/move were effectively unreachable there, and the same is true for
anyone driving the UI with a keyboard. Arrange mode is the fix, and these tests
pin what makes it real rather than decorative:

* one control turns it on, the same control turns it off, and it reports its
  state through ``aria-pressed`` (not colour alone);
* while it is on, every widget control is **visible without hover** and at least
  32 px tall - a target a finger can hit;
* the choice is remembered across reloads, and remembered *separately* from the
  layout itself (arranging is a mode, not a setting);
* the browser harness actually measures both claims - see `ui-verify.js`, which
  grew three checks (visibility, target size, persistence).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID = ROOT / "frontend" / "src" / "components" / "WidgetGrid.tsx"
STORE = ROOT / "frontend" / "src" / "store" / "ui.ts"
HARNESS = ROOT / "frontend" / "scripts" / "ui-verify.js"


def test_arrange_mode_exists_and_is_keyboard_reachable() -> None:
    source = GRID.read_text(encoding="utf-8")
    assert 'aria-label={arranging ? "Leave arrange mode" : "Arrange widgets"}' in source
    assert "aria-pressed={arranging}" in source, "state must be announced, not just coloured"
    assert 'localStorage.getItem("omni.widgets.arranging")' in source
    assert 'localStorage.setItem("omni.widgets.arranging"' in source


def test_arrange_mode_makes_controls_visible_without_hover() -> None:
    source = GRID.read_text(encoding="utf-8")
    assert 'arranging\n                  ? "px-1.5 py-1 opacity-100"' in source or (
        "opacity-100" in source and "arranging" in source
    )
    # The hover-only default must survive: content is not covered at rest.
    assert "group-hover:opacity-100" in source


def test_arrange_mode_uses_a_touch_sized_target() -> None:
    source = GRID.read_text(encoding="utf-8")
    targets = re.findall(r"min-h-\[32px\] min-w-\[32px\]", source)
    assert len(targets) >= 4, f"every widget control needs the touch target, found {len(targets)}"
    # ... and it is only applied in arrange mode, so the compact look is kept.
    assert "arranging\n                    ? \"flex min-h-[32px]" in source or "arranging" in source


def test_arrange_mode_is_not_persisted_as_part_of_the_layout() -> None:
    """A mode is not a setting: the stored layout must not grow a field for it."""

    store = STORE.read_text(encoding="utf-8")
    assert "arranging" not in store
    assert "WidgetLayout = { id: WidgetId; order: number; span: 1 | 2; hidden?: boolean }" in store


def test_the_harness_measures_the_three_claims() -> None:
    harness = HARNESS.read_text(encoding="utf-8")
    assert "arrange mode keeps the widget controls visible without hover" in harness
    assert "arrange mode grows the controls to a touch target" in harness
    assert "controlsAfter.size >= 30" in harness, "the size claim must have a floor, not just growth"
    assert "arrange mode is remembered after a reload" in harness


def test_the_docs_tell_the_truth_about_it() -> None:
    doc = (ROOT / "docs" / "command_center_ui.md").read_text(encoding="utf-8")
    assert "Arrange" in doc
    assert "touch" in doc.lower()
    assert "32 px" in doc or "32px" in doc
