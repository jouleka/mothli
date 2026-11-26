import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, test } from "vitest";
import { PANEL_MIN, PanelResizeHandle, clampPanel, panelMax, storedPanelWidth } from "./PanelResize";

function Harness({ start = null }: { start?: number | null }) {
  const [width, setWidth] = useState<number | null>(start);
  return (
    <div data-testid="panel" style={{ width: width ?? 440 }}>
      <PanelResizeHandle width={width} onWidth={setWidth} />
      <output>{width ?? "usual"}</output>
    </div>
  );
}
const shown = () => screen.getByRole("status").textContent;

beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1440 });
});

describe("resizing Mothli's side panel", () => {
  test("the arrow keys make it wider or narrower, within limits, and the width is remembered", () => {
    render(<Harness start={440} />);
    const edge = screen.getByRole("separator", { name: "Resize Mothli’s panel" });
    fireEvent.keyDown(edge, { key: "ArrowLeft" });
    expect(shown()).toBe("464");
    fireEvent.keyDown(edge, { key: "ArrowRight", shiftKey: true });
    expect(shown()).toBe("368");
    expect(storedPanelWidth()).toBe(368);
    fireEvent.keyDown(edge, { key: "Home" });
    expect(shown()).toBe(String(PANEL_MIN));
    fireEvent.keyDown(edge, { key: "End" });
    expect(shown()).toBe("760");
    expect(edge.getAttribute("aria-valuenow")).toBe("760");
  });

  test("a double-click (or Enter) goes back to the usual size and forgets the width", () => {
    render(<Harness start={600} />);
    const edge = screen.getByRole("separator");
    fireEvent.keyDown(edge, { key: "ArrowLeft" });
    expect(storedPanelWidth()).toBe(624);
    fireEvent.doubleClick(edge);
    expect(shown()).toBe("usual");
    expect(storedPanelWidth()).toBeNull();
  });

  test("the book always keeps room, and a stored width outside the limits is ignored", () => {
    expect(panelMax(900)).toBe(520);
    expect(clampPanel(9999, 900)).toBe(520);
    expect(clampPanel(10, 1440)).toBe(PANEL_MIN);
    localStorage.setItem("mothli.panelWidth", "5000");
    expect(storedPanelWidth()).toBeNull();
    localStorage.setItem("mothli.panelWidth", "nonsense");
    expect(storedPanelWidth()).toBeNull();
  });
});
