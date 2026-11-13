import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { NameCard } from "./NameCard";

const mocks = vi.hoisted(() => ({ character: vi.fn() }));
vi.mock("../api", () => ({ api: { character: mocks.character } }));

const dmitri = { entity_id: 2, canonical_name: "Dmitri Karamazov", aliases: ["Mitya"], type: "character", revealed_at: 2 };
const anchor = { left: 100, top: 100, right: 160, bottom: 120 };

beforeEach(() => {
  mocks.character.mockReset();
});

describe("the card for a tapped name", () => {
  test("says who it is from what is already known, then adds connections", async () => {
    mocks.character.mockResolvedValue({
      as_of_chapter: 20,
      entity_id: 2,
      name: "Dmitri Karamazov",
      type: "character",
      aliases: ["Mitya"],
      first_seen: 2,
      status: null,
      ties: [{ entity_id: 1, name: "Fyodor Pavlovich", rel_type: "family", label: "his father", direction: "out" }],
    });
    render(<NameCard bookId="kar" bookmark={36} person={dmitri} anchor={anchor} onAsk={() => {}} onMore={() => {}} onClose={() => {}} />);
    const card = screen.getByRole("dialog", { name: "Dmitri Karamazov" });
    expect(card.textContent).toContain("Also called Mitya.");
    expect(card.textContent).toContain("You met them in chapter 2.");
    expect(mocks.character).toHaveBeenCalledWith("kar", 2, 36);
    await waitFor(() => expect(card.textContent).toContain("his father"));
  });

  test("never shows details stamped later than the reader's place", async () => {
    mocks.character.mockResolvedValue({
      as_of_chapter: 90,
      entity_id: 2,
      name: "Dmitri Karamazov",
      type: "character",
      aliases: [],
      first_seen: 2,
      status: null,
      ties: [{ entity_id: 7, name: "A later secret", rel_type: "other", label: "spoiler", direction: "out" }],
    });
    render(<NameCard bookId="kar" bookmark={36} person={dmitri} anchor={anchor} onAsk={() => {}} onMore={() => {}} onClose={() => {}} />);
    await waitFor(() => expect(mocks.character).toHaveBeenCalled());
    expect(screen.getByRole("dialog").textContent).not.toContain("spoiler");
  });

  test("hands off to Mothli or the cast page, and closes on Escape", async () => {
    const user = userEvent.setup();
    mocks.character.mockRejectedValue(new Error("offline"));
    const onAsk = vi.fn();
    const onMore = vi.fn();
    const onClose = vi.fn();
    render(<NameCard bookId="kar" bookmark={36} person={dmitri} anchor={anchor} onAsk={onAsk} onMore={onMore} onClose={onClose} />);
    await user.click(screen.getByRole("button", { name: "Ask Mothli about Dmitri" }));
    expect(onAsk).toHaveBeenCalledWith("Dmitri Karamazov");
    await user.click(screen.getByRole("button", { name: "More" }));
    expect(onMore).toHaveBeenCalledWith(2);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});
