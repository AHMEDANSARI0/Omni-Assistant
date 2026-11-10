import { useMemo, useState, type ReactNode } from "react";
import { WIDGET_LABELS, useUi, type WidgetId } from "@/store/ui";
import type { RouteId } from "@/nav";

/**
 * Configurable widget grid.
 *
 * Everything here is real and persisted in `omni.ui.v1`:
 *   - reordering by native HTML5 drag-and-drop *and* by the arrow buttons, which
 *     is what keyboard-only users get;
 *   - width: half or full (the span toggle);
 *   - visibility: a widget can be hidden and restored without losing its place.
 *
 * The toolbar is a hover/focus affordance, so it never covers content at rest -
 * which is fine with a mouse and useless on a touch screen, where there is no
 * hover. **Arrange mode** fixes that: one tap on "Arrange" makes every control
 * permanently visible and touch-sized (>= 32 px targets), the same tap turns it
 * off again, and the choice is remembered. It is not persisted as part of the
 * layout: arranging is a mode you are in, not a setting.
 */
export function WidgetGrid({
  render,
  onNavigate,
}: {
  render: (id: WidgetId, onNavigate: (id: RouteId) => void) => ReactNode;
  onNavigate: (id: RouteId) => void;
}) {
  const { ui, moveWidget, resetLayout, setOrder, setSpan, toggleWidget } = useUi();
  const [dragging, setDragging] = useState<WidgetId | null>(null);
  const [over, setOver] = useState<WidgetId | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  // Arrange mode: stick the controls open for touch/keyboard, and remember it.
  const [arranging, setArranging] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem("omni.widgets.arranging") === "1";
    } catch {
      return false;
    }
  });

  const setArrange = (value: boolean) => {
    setArranging(value);
    try {
      window.localStorage.setItem("omni.widgets.arranging", value ? "1" : "0");
    } catch {
      /* private mode: the mode still works for this session */
    }
  };

  const ordered = useMemo(() => [...ui.widgets].sort((a, b) => a.order - b.order), [ui.widgets]);
  const visible = ordered.filter((widget) => !widget.hidden);
  const hiddenCount = ordered.length - visible.length;

  const moveTo = (from: WidgetId, to: WidgetId) => {
    if (from === to) return;
    const list = ordered.map((item) => item.id);
    const fromIndex = list.indexOf(from);
    const toIndex = list.indexOf(to);
    if (fromIndex < 0 || toIndex < 0) return;
    list.splice(fromIndex, 1);
    list.splice(toIndex, 0, from);
    setOrder(list);
  };

  return (
    <div className="min-w-0">
      <div className="relative mb-1 flex items-center justify-end gap-1.5">
        <span className="hud-label">
          {arranging ? "Arrange mode · move, resize or hide" : "Widgets · drag to arrange · ⤢ to resize"}
        </span>
        <button
          type="button"
          onClick={() => {
            setMenuOpen(false);
            setArrange(!arranging);
          }}
          aria-pressed={arranging}
          aria-label={arranging ? "Leave arrange mode" : "Arrange widgets"}
          className={`rounded border px-1.5 py-[1px] text-[10px] transition ${
            arranging
              ? "border-accent/70 bg-accent/15 text-accent"
              : "border-line/55 text-faint hover:border-accent/50 hover:text-accent"
          }`}
        >
          {arranging ? "Done" : "Arrange"}
        </button>
        <button
          type="button"
          onClick={() => setMenuOpen((value) => !value)}
          aria-expanded={menuOpen}
          aria-label="Show or hide widgets"
          className="rounded border border-line/55 px-1.5 py-[1px] text-[10px] text-faint transition hover:border-accent/50 hover:text-accent"
        >
          Widgets{hiddenCount > 0 ? ` (${visible.length}/${ordered.length})` : ""}
        </button>
        <button
          type="button"
          onClick={resetLayout}
          className="rounded border border-line/55 px-1.5 py-[1px] text-[10px] text-faint transition hover:border-accent/50 hover:text-accent"
        >
          Reset layout
        </button>
        {menuOpen ? (
          <div
            role="dialog"
            aria-label="Widget visibility"
            className="glass absolute end-0 top-6 z-40 w-[232px] rounded-xl p-2 text-start shadow-xl"
          >
            <p className="hud-label mb-1">Show widgets</p>
            <ul className="space-y-0.5">
              {ordered.map((widget) => (
                <li key={widget.id}>
                  <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-[3px] text-[11px] text-muted hover:bg-inset/50">
                    <input
                      type="checkbox"
                      checked={!widget.hidden}
                      onChange={() => toggleWidget(widget.id)}
                      className="accent-[rgb(var(--accent))]"
                    />
                    <span className="min-w-0 flex-1 truncate">{WIDGET_LABELS[widget.id]}</span>
                  </label>
                </li>
              ))}
            </ul>
            <button
              type="button"
              onClick={() => setMenuOpen(false)}
              className="mt-1.5 w-full rounded border border-line/60 py-[2px] text-[10px] text-faint hover:border-accent/50 hover:text-accent"
            >
              Done
            </button>
          </div>
        ) : null}
      </div>
      {visible.length === 0 ? (
        <div className="glass rounded-2xl px-3 py-6 text-center">
          <p className="text-[12px] text-muted">Every widget is hidden.</p>
          <button
            type="button"
            onClick={resetLayout}
            className="mt-2 rounded border border-accent/50 px-2 py-[3px] text-[11px] text-accent transition hover:bg-accent/15"
          >
            Restore the default layout
          </button>
        </div>
      ) : null}
      <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2 xl:grid-cols-4">
        {visible.map((widget) => (
          <div
            key={widget.id}
            draggable
            onDragStart={(event) => {
              setDragging(widget.id);
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("text/plain", widget.id);
            }}
            onDragEnd={() => {
              setDragging(null);
              setOver(null);
            }}
            onDragOver={(event) => {
              event.preventDefault();
              if (dragging && dragging !== widget.id) setOver(widget.id);
            }}
            onDrop={(event) => {
              event.preventDefault();
              const from = (event.dataTransfer.getData("text/plain") || dragging) as WidgetId | null;
              if (from) moveTo(from, widget.id);
              setDragging(null);
              setOver(null);
            }}
            className={`group relative min-w-0 ${widget.span === 2 ? "md:col-span-2" : ""} ${
              dragging === widget.id ? "opacity-45" : ""
            } ${over === widget.id ? "ring-1 ring-accent/60" : ""}`}
          >
            <div
              className={`absolute -top-2 end-1.5 z-10 flex items-center gap-1 rounded-md border border-line/70 bg-elevated/95 shadow transition ${
                arranging
                  ? "px-1.5 py-1 opacity-100"
                  : "px-1 py-[1px] opacity-0 group-hover:opacity-100 focus-within:opacity-100"
              }`}
            >
              <span
                className={`cursor-grab select-none text-faint ${
                  arranging ? "flex min-h-[32px] min-w-[32px] items-center justify-center px-1 text-[13px]" : "px-0.5 text-[10px]"
                }`}
                aria-hidden
              >
                ⠿
              </span>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  moveWidget(widget.id, -1);
                }}
                className={`text-faint hover:text-accent ${
                  arranging ? "flex min-h-[32px] min-w-[32px] items-center justify-center px-1 text-[13px]" : "px-0.5 text-[10px]"
                }`}
                aria-label={`Move ${WIDGET_LABELS[widget.id]} earlier`}
              >
                ←
              </button>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  moveWidget(widget.id, 1);
                }}
                className={`text-faint hover:text-accent ${
                  arranging ? "flex min-h-[32px] min-w-[32px] items-center justify-center px-1 text-[13px]" : "px-0.5 text-[10px]"
                }`}
                aria-label={`Move ${WIDGET_LABELS[widget.id]} later`}
              >
                →
              </button>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  setSpan(widget.id, widget.span === 2 ? 1 : 2);
                }}
                className={`text-faint hover:text-accent ${
                  arranging ? "flex min-h-[32px] min-w-[32px] items-center justify-center px-1 text-[13px]" : "px-0.5 text-[10px]"
                }`}
                aria-label={
                  widget.span === 2
                    ? `Make ${WIDGET_LABELS[widget.id]} half width`
                    : `Make ${WIDGET_LABELS[widget.id]} full width`
                }
                title={widget.span === 2 ? "Half width" : "Full width"}
              >
                ⤢
              </button>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  toggleWidget(widget.id);
                }}
                className={`text-faint hover:text-danger ${
                  arranging ? "flex min-h-[32px] min-w-[32px] items-center justify-center px-1 text-[13px]" : "px-0.5 text-[10px]"
                }`}
                aria-label={`Hide ${WIDGET_LABELS[widget.id]}`}
                title="Hide widget"
              >
                ✕
              </button>
            </div>
            {render(widget.id, onNavigate)}
          </div>
        ))}
      </div>
    </div>
  );
}
