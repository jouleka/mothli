/** The first-open question: where is the reader in a book they may have started elsewhere? It
 * must say, before anything is pressed, exactly which chapters will count as read, be easy to
 * skip, and work by keyboard and screen reader. */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";
import { StartingPlace, startingLine, type StartingChapter } from "./StartingPlace";

// A book with a cover (piece 0), then chapters 1 to 61 as pieces 1 to 61.
const chapters: StartingChapter[] = Array.from({ length: 61 }, (_, index) => ({
  index: index + 1, number: index + 1, before: index }));

describe("StartingPlace", () => {
  test("asks in Mothli's words, names the chapters it will count, and counts them only when asked", async () => {
    const onChoose = vi.fn(async () => {});
    const onStart = vi.fn();
    render(<StartingPlace chapters={chapters} onChoose={onChoose} onStart={onStart} />);
    const dialog = screen.getByRole("dialog", { name: "Already partway through?" });
    expect(dialog.getAttribute("aria-modal")).toBe("false");
    expect(dialog.textContent).toMatch(/tell me which chapter you’re on/);
    const go = screen.getByRole("button", { name: "take me there" }) as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("I’m on"), { target: { value: "31" } });
    expect(dialog.textContent).toMatch(/Chapters 1 to 30 will count as read\. We’ll pick up at the start of chapter 31\./);
    expect(onChoose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "take me to chapter 31" }));
    await waitFor(() => expect(onChoose).toHaveBeenCalledWith(chapters[30]));
    expect(onStart).not.toHaveBeenCalled();
  });

  test("chapter 1 counts nothing, and a reader who finished chapter 30 is told it counts", () => {
    expect(startingLine(chapters[0], "chapter")).toBe("Nothing counts as read yet. We’ll start at chapter 1.");
    expect(startingLine(chapters[1], "chapter")).toBe("Chapter 1 will count as read. We’ll pick up at the start of chapter 2.");
    expect(startingLine(chapters[30], "chapter")).toMatch(/^Chapters 1 to 30 will count as read/);
  });

  test("“I’m just starting” and Escape both skip it", async () => {
    const onStart = vi.fn();
    const { unmount } = render(<StartingPlace chapters={chapters} onChoose={async () => {}} onStart={onStart} />);
    fireEvent.click(screen.getByRole("button", { name: "I’m just starting" }));
    expect(onStart).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onStart).toHaveBeenCalledTimes(2);
    unmount();
  });

  test("keyboard: focus lands on the picker, and the reader can tab back out to the book", async () => {
    const user = userEvent.setup();
    render(
      <main id="main" tabIndex={-1}>
        <StartingPlace chapters={chapters} onChoose={async () => {}} onStart={() => {}} />
        <button>Next page</button>
      </main>,
    );
    expect(document.activeElement).toBe(screen.getByLabelText("I’m on"));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "I’m just starting" }));
    await user.tab();
    // "take me there" is disabled until a chapter is picked, so the book comes next: not trapped.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Next page" }));
  });

  test("gives focus back to the book when it closes", () => {
    const main = document.createElement("main");
    main.id = "main";
    main.tabIndex = -1;
    document.body.appendChild(main);
    const { unmount } = render(<StartingPlace chapters={chapters} onChoose={async () => {}} onStart={() => {}} />);
    unmount();
    expect(document.activeElement).toBe(main);
    main.remove();
  });

  test("a failed answer says so and can be tried again", async () => {
    const onChoose = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(undefined);
    render(<StartingPlace chapters={chapters} onChoose={onChoose} onStart={() => {}} />);
    fireEvent.change(screen.getByLabelText("I’m on"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "take me to chapter 5" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toBe("That didn’t go through. Try once more.");
    fireEvent.click(screen.getByRole("button", { name: "take me to chapter 5" }));
    await waitFor(() => expect(onChoose).toHaveBeenCalledTimes(2));
  });

  test("has no axe violations", async () => {
    const { container } = render(<StartingPlace chapters={chapters} onChoose={async () => {}} onStart={() => {}} />);
    fireEvent.change(screen.getByLabelText("I’m on"), { target: { value: "31" } });
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
