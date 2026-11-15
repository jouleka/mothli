import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { ImportOffer } from "./ImportOffer";

describe("ImportOffer", () => {
  test("a book finished elsewhere is offered as read, and nothing happens until the reader answers", async () => {
    const onAccept = vi.fn();
    const onDecline = vi.fn();
    const { container } = render(<ImportOffer onAccept={onAccept} onDecline={onDecline} offer={{
      kind: "finished", sources: ["goodreads"], finished_on: "2021-05-03", chapter: null, atoms: [1, 2, 3] }} />);
    const note = screen.getByRole("region", { name: /from your reading history/i });
    expect(note.textContent).toMatch(/Goodreads says you finished this book/);
    expect(note.textContent).toMatch(/2021/);
    expect(onAccept).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "yes, I’ve read it" }));
    expect(onAccept).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "no, I’m starting it now" }));
    expect(onDecline).toHaveBeenCalledTimes(1);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("highlights from another app offer the chapters before the furthest one", () => {
    render(<ImportOffer onAccept={() => {}} onDecline={() => {}} offer={{
      kind: "progress", sources: ["kindle"], finished_on: null, chapter: 3, atoms: [1, 2] }} />);
    const note = screen.getByRole("region", { name: /from your reading history/i });
    expect(note.textContent).toMatch(/Kindle highlights reach 2 chapters into this book/);
    expect(screen.getByRole("button", { name: "yes, I’ve read those" })).toBeTruthy();
  });
});
