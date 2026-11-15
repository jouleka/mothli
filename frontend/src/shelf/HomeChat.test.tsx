import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ homeConversation: vi.fn(), askHome: vi.fn(), clearHome: vi.fn() }));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: mocks,
}));
import { ApiError } from "../api";
import { HomeChat } from "./HomeChat";

beforeEach(() => {
  mocks.homeConversation.mockReset().mockResolvedValue({ turns: [] });
  mocks.askHome.mockReset();
  mocks.clearHome.mockReset().mockResolvedValue(undefined);
});

describe("HomeChat", () => {
  test("is not shown where the server has no home conversation", async () => {
    mocks.homeConversation.mockRejectedValue(new ApiError("not enabled", 404));
    const { container } = render(<HomeChat />);
    await waitFor(() => expect(mocks.homeConversation).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  test("shows the earlier conversation, then each checked sentence of a new reply as it arrives", async () => {
    mocks.homeConversation.mockResolvedValue({ turns: [
      { role: "user", content: "Where am I?", created_at: "2026-11-15T10:00:00Z" },
      { role: "assistant", content: "Four chapters into Karamazov.", created_at: "2026-11-15T10:00:02Z" }] });
    let finish: (value: unknown) => void = () => {};
    mocks.askHome.mockImplementation((_question: string, onSentence: (text: string) => void) => {
      onSentence("I’d pick up Karamazov tonight.");
      return new Promise((resolve) => { finish = resolve; });
    });
    const { container } = render(<HomeChat />);
    expect(await screen.findByText("Four chapters into Karamazov.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Ask Mothli about your books"), { target: { value: "What next?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("I’d pick up Karamazov tonight.")).toBeTruthy();
    expect(within(screen.getByRole("list")).getByText("What next?")).toBeTruthy();
    expect(mocks.askHome.mock.calls[0][0]).toBe("What next?");
    finish({ insufficient_evidence: false, claims: [
      { text: "I’d pick up Karamazov tonight.", citation_ids: [] },
      { text: "You’re one chapter from the end.", citation_ids: [] }] });
    expect(await screen.findByText(/You’re one chapter from the end\./)).toBeTruthy();
    expect((screen.getByLabelText("Ask Mothli about your books") as HTMLTextAreaElement).value).toBe("");
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("a reply with nothing shown says so, a failure keeps the question, and clear empties both sides", async () => {
    mocks.askHome.mockResolvedValueOnce({ insufficient_evidence: true, claims: [] });
    render(<HomeChat />);
    const box = (await screen.findByLabelText("Ask Mothli about your books")) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "Who dies?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText(/haven’t got there yet/i)).toBeTruthy();
    mocks.askHome.mockRejectedValueOnce(new ApiError("Jev review unavailable", 503));
    fireEvent.change(box, { target: { value: "And then?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(box.value).toBe("And then?");
    fireEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
    await waitFor(() => expect(mocks.clearHome).toHaveBeenCalled());
    expect(screen.queryByText("Who dies?")).toBeNull();
  });
});
