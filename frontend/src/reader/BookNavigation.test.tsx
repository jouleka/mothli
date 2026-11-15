import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ReaderNavigation } from "./BookNavigation";
import { mapReaderToc } from "./readerNavigation";

const atoms = [
  { ordinal: 1, href: "ch1.xhtml", title: "Chapter I", part_label: "Part I", char_len: 100 },
  { ordinal: 2, href: "ch2.xhtml", title: "", part_label: "", char_len: 100 },
];
const toc = mapReaderToc([
  { label: "raw part", subitems: [
    { label: "Chapter I", href: "ch1.xhtml" },
    { label: "Future spoiler", href: "ch2.xhtml" },
  ] },
], ["ch1.xhtml", "ch2.xhtml"], [0, 1], atoms);

const props = {
  toc,
  atoms,
  currentAtom: 0,
  canGoBack: true,
  canGoForward: false,
  searchableChapters: 1,
  onBack: vi.fn(),
  onForward: vi.fn(),
  onNavigate: vi.fn(),
  onSearch: vi.fn(async () => [{
    cfi: "epubcfi(/6/2)", atom: 0,
    excerpt: { pre: "A ", match: "lantern", post: " glowed." },
  }]),
  onSearchNavigate: vi.fn(),
};

describe("ReaderNavigation", () => {
  test("renders hierarchical spoiler-safe TOC and marks the current location", async () => {
    const { container } = render(<ReaderNavigation {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    const nav = screen.getByRole("navigation", { name: /table of contents/i });
    expect(within(nav).getByRole("button", { name: "Chapter I" }).getAttribute("aria-current"))
      .toBe("location");
    expect(nav.textContent).toContain("Part I");
    expect(nav.textContent).toContain("Chapter 2");
    expect(nav.textContent).not.toContain("Future spoiler");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("searches read pages, renders escaped excerpts, and opens the exact CFI", async () => {
    const onSearchNavigate = vi.fn();
    render(<ReaderNavigation {...props} onSearchNavigate={onSearchNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    fireEvent.click(screen.getByRole("tab", { name: /search pages/i }));
    fireEvent.change(screen.getByRole("searchbox", { name: /find in pages/i }), {
      target: { value: "lantern" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^find$/i }));
    await waitFor(() => expect(props.onSearch).toHaveBeenCalledWith("lantern"));
    const result = screen.getByRole("button", { name: /lantern/i });
    expect(result.querySelector("mark")?.textContent).toBe("lantern");
    fireEvent.click(result);
    expect(onSearchNavigate).toHaveBeenCalledWith(expect.objectContaining({
      cfi: "epubcfi(/6/2)", atom: 0,
    }));
  });

  test("history controls and Escape focus restoration are keyboard accessible", () => {
    const onBack = vi.fn();
    render(<ReaderNavigation {...props} onBack={onBack} />);
    const opener = screen.getByRole("button", { name: /^contents$/i });
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));
    expect(onBack).toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("region", { name: /book navigation/i }), { key: "Escape" });
    expect(screen.queryByRole("region", { name: /book navigation/i })).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  test("Contents and Search use keyboard tabs without competing for attention", () => {
    render(<ReaderNavigation {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    const contents = screen.getByRole("tab", { name: /^contents$/i });
    expect(screen.queryByRole("searchbox")).toBeNull();
    fireEvent.keyDown(contents, { key: "ArrowRight" });
    const search = screen.getByRole("tab", { name: /search pages/i });
    expect(search.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(search);
    expect(screen.queryByRole("navigation", { name: /table of contents/i })).toBeNull();
    expect(screen.getByRole("searchbox")).toBeTruthy();
    fireEvent.keyDown(search, { key: "Home" });
    expect(document.activeElement).toBe(contents);
    expect(contents.getAttribute("aria-selected")).toBe("true");
  });
});

describe("ReaderNavigation with the read map", () => {
  test("each chapter says whether Mothli counts it as read, and the reader can change that", () => {
    const onMarkRead = vi.fn();
    render(<ReaderNavigation {...props} readShare={{ 0: 1, 1: 0.4 }} onMarkRead={onMarkRead} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    const first = screen.getByRole("button", { name: "I’ve read Chapter I" });
    const second = screen.getByRole("button", { name: "I’ve read Chapter 2" });
    expect(first.getAttribute("aria-pressed")).toBe("true");
    expect(second.getAttribute("aria-pressed")).toBe("false");
    expect(second.textContent).toMatch(/part read/i);
    fireEvent.click(second);
    expect(onMarkRead).toHaveBeenLastCalledWith(1, true);
    fireEvent.click(first);
    expect(onMarkRead).toHaveBeenLastCalledWith(0, false);
  });

  test("without a read map the contents list has no such controls", () => {
    render(<ReaderNavigation {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /^contents$/i }));
    expect(screen.queryByRole("button", { name: /I’ve read/ })).toBeNull();
  });
});
