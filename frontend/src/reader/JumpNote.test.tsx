/** The jump-confirmation strip is an alert dialog: it interrupts to ask a spoiler-relevant question
 * ("this marks everything before it as read"), so keyboard users must land in it and be able to
 * dismiss it without a mouse. */
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { JumpNote } from "./JumpNote";

describe("JumpNote", () => {
  test("is an alertdialog naming the target chapter", () => {
    render(<JumpNote chapter={91} onFollow={() => {}} onStay={() => {}} />);
    const dlg = screen.getByRole("alertdialog");
    expect(dlg.textContent).toMatch(/chapter 91/);
  });

  test("moves focus onto a control when it appears (keyboard users are not stranded)", () => {
    render(<JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />);
    const active = document.activeElement as HTMLElement;
    expect(active.tagName).toBe("BUTTON");
    expect(active.closest('[role="alertdialog"]')).not.toBeNull();
  });

  test("Escape dismisses via onStay (the safe default)", () => {
    const onStay = vi.fn();
    render(<JumpNote chapter={5} onFollow={() => {}} onStay={onStay} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onStay).toHaveBeenCalledTimes(1);
  });

  test("follow and stay buttons fire their handlers", () => {
    const onFollow = vi.fn();
    const onStay = vi.fn();
    render(<JumpNote chapter={5} onFollow={onFollow} onStay={onStay} />);
    fireEvent.click(screen.getByRole("button", { name: /follow/i }));
    fireEvent.click(screen.getByRole("button", { name: /stay/i }));
    expect(onFollow).toHaveBeenCalledTimes(1);
    expect(onStay).toHaveBeenCalledTimes(1);
  });

  test("traps Tab within its own controls (cannot reach the reader behind it)", async () => {
    const user = userEvent.setup();
    render(<JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />);
    const follow = screen.getByRole("button", { name: /follow/i });
    const stay = screen.getByRole("button", { name: /stay/i });
    expect(document.activeElement).toBe(stay); // opens on the safe choice
    await user.tab();
    expect(document.activeElement).toBe(follow); // wraps to the other control, never escapes
    await user.tab();
    expect(document.activeElement).toBe(stay);
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(follow);
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(stay); // shift-tab wraps back
  });

  test("blocks background controls without making the containing book inert", () => {
    const turnPage = vi.fn();
    const openCompanion = vi.fn();
    const { container, unmount } = render(
      <div className="reader-grid">
        <main>
          <header>
            <button onClick={turnPage}>Next page</button>
          </header>
          <JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />
        </main>
        <aside>
          <button onClick={openCompanion}>Open companion</button>
        </aside>
      </div>,
    );
    const main = container.querySelector("main")!;
    const header = container.querySelector("header")!;
    const companion = container.querySelector("aside")!;
    expect(main.inert).not.toBe(true);
    expect(header.inert).toBe(true);
    expect(companion.inert).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    fireEvent.click(screen.getByRole("button", { name: "Open companion" }));
    expect(turnPage).not.toHaveBeenCalled();
    expect(openCompanion).not.toHaveBeenCalled();
    unmount();
    expect(header.inert).not.toBe(true);
    expect(companion.inert).not.toBe(true);
  });

  test("uses the newest dismissal callback without recapturing focus on rerender", () => {
    const oldStay = vi.fn();
    const newStay = vi.fn();
    const { rerender } = render(
      <JumpNote chapter={5} onFollow={() => {}} onStay={oldStay} />,
    );
    const follow = screen.getByRole("button", { name: "follow" });
    follow.focus();
    rerender(<JumpNote chapter={6} onFollow={() => {}} onStay={newStay} />);
    expect(document.activeElement).toBe(follow);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(oldStay).not.toHaveBeenCalled();
    expect(newStay).toHaveBeenCalledOnce();
  });

  test("restores focus to the trigger when it closes (WCAG 2.4.3)", () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    const { unmount } = render(
      <JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />,
    );
    expect(document.activeElement).not.toBe(trigger); // focus moved into the dialog
    unmount();
    expect(document.activeElement).toBe(trigger); // and returned on close
    trigger.remove();
  });

  test("has no axe violations", async () => {
    const { container } = render(
      <JumpNote chapter={5} onFollow={() => {}} onStay={() => {}} />,
    );
    expect(await axeAA(container)).toHaveNoViolations();
  });
});

describe("JumpNote with the read map", () => {
  test("says a jump is not reading, and lets the reader say they have read up to there", () => {
    const onFollow = vi.fn();
    const onFollowRead = vi.fn();
    render(<JumpNote chapter={15} onFollow={onFollow} onFollowRead={onFollowRead} onStay={() => {}} />);
    const dialog = screen.getByRole("alertdialog");
    expect(dialog.textContent).toMatch(/only knows the pages you.ve read/i);
    expect(dialog.textContent).not.toMatch(/marks earlier chapters as read/i);
    fireEvent.click(screen.getByRole("button", { name: "go there" }));
    expect(onFollow).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "I’ve read chapters 1 to 14" }));
    expect(onFollowRead).toHaveBeenCalledTimes(1);
  });

  // 2026-11-20: a reader who had finished chapter 30 tapped chapter 30 and said "I've read up to
  // here", which counted chapters 1 to 29. The button now names the chapters it counts, and the
  // chapter being opened is said to stay unread.
  test("names exactly the chapters it will count as read", () => {
    render(<JumpNote chapter={30} readThrough={29} onFollow={() => {}} onFollowRead={() => {}} onStay={() => {}} />);
    const dialog = screen.getByRole("alertdialog");
    expect(screen.getByRole("button", { name: "I’ve read chapters 1 to 29" })).toBeTruthy();
    expect(dialog.textContent).toMatch(/Chapter 30 itself stays unread until you read it/);
    expect(screen.queryByRole("button", { name: /up to here/ })).toBeNull();
  });

  test("counts the book's own chapters, not the pieces before them", () => {
    // Chapter 3 is the fifth piece of a book with a cover and a contents page.
    render(<JumpNote chapter={3} readThrough={2} onFollow={() => {}} onFollowRead={() => {}} onStay={() => {}} />);
    expect(screen.getByRole("button", { name: "I’ve read chapters 1 and 2" })).toBeTruthy();
  });

  test("one chapter before is named alone, and none offers nothing to count", () => {
    const { unmount } = render(<JumpNote chapter={2} readThrough={1} onFollow={() => {}} onFollowRead={() => {}} onStay={() => {}} />);
    expect(screen.getByRole("button", { name: "I’ve read chapter 1" })).toBeTruthy();
    unmount();
    render(<JumpNote chapter={1} readThrough={0} onFollow={() => {}} onFollowRead={() => {}} onStay={() => {}} />);
    expect(screen.queryByRole("button", { name: /I’ve read/ })).toBeNull();
    expect(screen.getByRole("button", { name: "go there" })).toBeTruthy();
  });

  test("has no axe violations", async () => {
    const { container } = render(
      <JumpNote chapter={31} readThrough={30} onFollow={() => {}} onFollowRead={() => {}} onStay={() => {}} />,
    );
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
