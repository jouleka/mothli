/** The edge between the book and Mothli's side panel: drag it, or use the arrow keys, to make the
 * panel wider or narrower. The width is this browser's own (localStorage, optional), and a
 * double-click puts the panel back to its usual size. Phones never show it: there the panel is
 * the whole screen. */
import { type KeyboardEvent, type PointerEvent, useRef } from "react";

export const PANEL_MIN = 320;
export const PANEL_MAX = 760;
/** The book keeps at least this much room beside the panel. */
const BOOK_MIN = 380;
const STEP = 24;
const BIG_STEP = 96;
const STORAGE_KEY = "mothli.panelWidth";

export function storedPanelWidth(): number | null {
  try {
    const value = Number(localStorage.getItem(STORAGE_KEY));
    return Number.isFinite(value) && value >= PANEL_MIN && value <= PANEL_MAX ? Math.round(value) : null;
  } catch {
    return null;
  }
}

export function storePanelWidth(width: number | null) {
  try {
    if (width == null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, String(Math.round(width)));
  } catch {
    /* The width is a convenience: without storage it lasts for this visit. */
  }
}

/** The widest the panel may be in a window this wide. */
export function panelMax(viewport: number) {
  return Math.max(PANEL_MIN, Math.min(PANEL_MAX, viewport - BOOK_MIN));
}

export function clampPanel(width: number, viewport = window.innerWidth) {
  return Math.round(Math.min(panelMax(viewport), Math.max(PANEL_MIN, width)));
}

export function PanelResizeHandle({
  width,
  onWidth,
}: {
  /** The width the reader chose, or null for the panel's usual size. */
  width: number | null;
  onWidth: (width: number | null) => void;
}) {
  const handleRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ startX: number; startWidth: number; last: number } | null>(null);
  // The panel's own width as drawn, so a first drag starts from wherever the layout put it.
  const shown = () => {
    const panel = handleRef.current?.parentElement;
    if (width != null) return width;
    if (!panel) return PANEL_MIN;
    // The grid column the panel sits in: its own width plus the margins around it.
    const style = getComputedStyle(panel);
    const margins = (parseFloat(style.marginLeft) || 0) + (parseFloat(style.marginRight) || 0);
    return Math.round(panel.getBoundingClientRect().width + margins);
  };
  const choose = (next: number) => {
    const clamped = clampPanel(next);
    onWidth(clamped);
    storePanelWidth(clamped);
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startWidth = shown();
    drag.current = { startX: event.clientX, startWidth, last: startWidth };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    document.documentElement.dataset.panelResizing = "";
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current) return;
    // The panel is on the right: moving the edge left makes it wider.
    const next = clampPanel(current.startWidth + (current.startX - event.clientX));
    if (next !== current.last) {
      current.last = next;
      onWidth(next);
    }
  };
  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    delete document.documentElement.dataset.panelResizing;
    if (current.last !== current.startWidth) storePanelWidth(current.last);
  };
  const reset = () => {
    onWidth(null);
    storePanelWidth(null);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? BIG_STEP : STEP;
    const now = shown();
    if (event.key === "ArrowLeft") choose(now + step);
    else if (event.key === "ArrowRight") choose(now - step);
    else if (event.key === "Home") choose(PANEL_MIN);
    else if (event.key === "End") choose(PANEL_MAX);
    else if (event.key === "Enter") reset();
    else return;
    event.preventDefault();
    event.stopPropagation();
  };

  const value = width ?? undefined;
  return (
    <div
      ref={handleRef}
      className="panel-resize"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize Mothli’s panel"
      aria-valuemin={PANEL_MIN}
      aria-valuemax={typeof window === "undefined" ? PANEL_MAX : panelMax(window.innerWidth)}
      aria-valuenow={value}
      aria-valuetext={value ? `${value} pixels wide` : "Usual width"}
      title="Drag to resize. Double-click for the usual size."
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={reset}
      onKeyDown={onKeyDown}
    >
      <span aria-hidden="true" />
    </div>
  );
}
