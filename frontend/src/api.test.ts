import { afterEach, describe, expect, test, vi } from "vitest";

import { api } from "./api";
import { flushOfflineMutations, pendingPosition, queuePosition, SIGNED_OUT_EVENT } from "./pwa/offline";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

describe("API errors", () => {
  test("rejects oversized selections locally with a useful message and no request", async () => {
    const request=vi.spyOn(globalThis,"fetch");
    await expect(api.selectionAction("book",{action:"define",text:"word ".repeat(500),atom:1,cfi:"cfi"}))
      .rejects.toMatchObject({status:413,code:"selection_too_large"});
    expect(request).not.toHaveBeenCalled();
  });

  test("shares a recap request between the sidebar and dialog at the same reading boundary", async () => {
    let finish!: (response: Response) => void;
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const sidebar = api.catchMeUp("shared", 4);
    const dialog = api.catchMeUp("shared", 4);
    expect(dialog).toBe(sidebar);
    expect(fetch).toHaveBeenCalledTimes(1);
    finish(new Response(JSON.stringify({ recap: "Nell arrived.", as_of_chapter: 4 }), { status: 200 }));
    expect((await dialog).recap).toBe("Nell arrived.");
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ recap: "Checked again.", as_of_chapter: 4 }), { status: 200 }));
    await api.catchMeUp("shared", 4);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  test("a stalled recap ends with a timeout rather than waiting forever", async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    const result = expect(api.catchMeUp("stalled", 4)).rejects.toMatchObject({ code: "recap_timeout", status: 504 });
    await vi.advanceTimersByTimeAsync(60_000);
    await result;
  });

  test("an old owner’s recap never appears in a new reading session", async () => {
    localStorage.setItem("litlet.offline.owner", "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67");
    let finish!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const pending = api.catchMeUp("private", 4);
    localStorage.setItem("litlet.offline.owner", "local");
    finish(new Response(JSON.stringify({ recap: "Private recap.", as_of_chapter: 4 }), { status: 200 }));
    await expect(pending).rejects.toMatchObject({ code: "session_changed" });
  });

  test("preserves a structured provider error code without exposing raw response details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      detail: {
        code: "provider_authentication_failed",
        message: "The AI provider rejected the configured credentials.",
      },
    }), { status: 503, headers: { "content-type": "application/json" } }));

    await expect(api.catchMeUp("b", 4)).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      code: "provider_authentication_failed",
      message: "The AI provider rejected the configured credentials.",
    });
  });
});

describe("Reader navigation reads", () => {
  test("encodes owner-scoped in-book search queries and bounds the result count", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      as_of_chapter: 1, hits: [],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    await api.searchBook("book", "light & shadow", 12);
    expect(fetch).toHaveBeenCalledWith(
      "/api/books/book/search?q=light+%26+shadow&limit=12",
    );
  });
});

describe("Hosted writes", () => {
  test("outbox replay waits for a live completion and preserves the later cursor with its acknowledged version", async () => {
    localStorage.setItem("litlet.offline.owner", "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67");
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    let release!: (response: Response) => void;
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; }))
      .mockImplementationOnce(async (_url, options) => {
        expect(JSON.parse(options?.body as string)).toMatchObject({
          cfi: "rereading", offset: 10, completed_chapter: 3, base_version: 1,
          position_epoch: 0, client_sequence: 2,
        });
        return new Response(JSON.stringify({ bookmark: 3, position_epoch: 0, position_version: 2 }), { status: 200 });
      });
    const live = api.putPosition("book", "finished", 300, 3, 0, 0, "reader-client", 1);
    queuePosition("book", "/api/books/book/position", { cfi: "rereading", offset: 10,
      completed_chapter: 3, position_epoch: 0, base_version: 0, client_id: "reader-client", client_sequence: 2 });
    const replay = flushOfflineMutations();
    await Promise.resolve();
    expect(fetch).toHaveBeenCalledTimes(1);
    release(new Response(JSON.stringify({ bookmark: 3, current_chapter: 4, chapter_progress: 1,
      position_epoch: 0, position_version: 1, applied: true }), { status: 200 }));
    await live;
    await replay;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(pendingPosition("book")).toBeNull();
  });

  test("a failing old-owner position request never queues data under the new owner", async () => {
    const owner = "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67";
    localStorage.setItem("litlet.offline.owner", owner);
    let fail!: (error: Error) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>((_resolve, reject) => { fail = reject; }));
    const delivery = api.putPosition("book", "private-page", 10, 1, 0, 0, "reader-client", 1);
    localStorage.setItem("litlet.offline.owner", "local");
    fail(new TypeError("offline"));
    await expect(delivery).rejects.toMatchObject({ status: 409 });
    expect(pendingPosition("book")).toBeNull();
    localStorage.setItem("litlet.offline.owner", owner);
    expect(pendingPosition("book")).toBeNull();
  });

  test("a reopened reader's live write follows an already replaying outbox position", async () => {
    localStorage.setItem("litlet.offline.owner", "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67");
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    queuePosition("book", "/api/books/book/position", { cfi: "queued-page", offset: 10,
      completed_chapter: 3, position_epoch: 0, base_version: 0, client_id: "reopening-client", client_sequence: 2 });
    let release!: (response: Response) => void;
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; }))
      .mockImplementationOnce(async (_url, options) => {
        expect(JSON.parse(options?.body as string)).toMatchObject({ cfi: "newest-page",
          completed_chapter: 3, base_version: 2, client_sequence: 3 });
        return new Response(JSON.stringify({ bookmark: 3, current_chapter: 1, chapter_progress: 0.2,
          position_epoch: 0, position_version: 3, applied: true }), { status: 200 });
      });
    const replay = flushOfflineMutations();
    const live = api.putPosition("book", "newest-page", 20, 3, 0, 0, "reopening-client", 3);
    expect(fetch).toHaveBeenCalledTimes(1);
    release(new Response(JSON.stringify({ bookmark: 3, position_epoch: 0,
      position_version: 2, applied: true }), { status: 200 }));
    await replay;
    await live;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(pendingPosition("book")).toBeNull();
  });

  test("a successful reset discards old-pass intent and preserves a newer queued epoch", async () => {
    queuePosition("book", "/api/books/book/position", { cfi: "old-finish", offset: 300,
      completed_chapter: 3, position_epoch: 0 });
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({
      bookmark: 0, cfi: null, position_epoch: 1,
    }), { status: 200 }));
    await api.resetPosition("book", 0);
    expect(pendingPosition("book")).toBeNull();
    queuePosition("book", "/api/books/book/position", { cfi: "new-pass", offset: 0,
      completed_chapter: 0, position_epoch: 1 });
    await api.resetPosition("book", 0);
    expect(pendingPosition("book")).toMatchObject({ cfi: "new-pass", position_epoch: 1, completed_chapter: 0 });
  });

  test("copies the non-HttpOnly CSRF cookie into provider-setting writes", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      id: "setting", capability: "extraction", validation_status: "unchecked",
    }), { status: 200, headers: { "content-type": "application/json" } }));

    await api.putProviderSetting("extraction", {
      provider: "openai-compatible",
      model: "gpt-4o-mini",
      credential_id: "credential",
      base_url: "https://api.openai.com/v1",
    });
    expect(fetch).toHaveBeenCalledWith("/api/provider-settings/extraction", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
    }));
  });

  test("accepts an empty 204 response when deleting a credential", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
    await expect(api.deleteCredential("credential")).resolves.toBeUndefined();
  });

  test("sends constrained reader preferences to the owner/book route with CSRF", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const preferences = {
      font_size: "large" as const,
      line_height: "comfortable" as const,
      measure: "balanced" as const,
      theme: "night" as const,
      margins: "balanced" as const,
      typeface: "serif" as const,
    };
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      ...preferences, preference_version: 2,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    await api.putReaderPreferences("book", preferences);
    expect(fetch).toHaveBeenCalledWith("/api/books/book/preferences", expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      body: JSON.stringify(preferences),
    }));
  });

  test("writes portable reader anchors with CSRF and deletes marks through their typed route", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const mark = {
      id: "mark", kind: "highlight" as const,
      anchor: { cfi: "epubcfi(/6/2!/4/2,/1:0,/1:4)", atom: 1 },
      color: "yellow" as const, selected_text: "text", body: null, label: null,
      version: 1, created_at: "now", updated_at: "now",
    };
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(mark), {
        status: 201, headers: { "content-type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await api.createHighlight("book", {
      anchor: mark.anchor, color: "yellow", selected_text: "text",
    });
    expect(fetch).toHaveBeenNthCalledWith(1, "/api/books/book/highlights", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
      body: JSON.stringify({ anchor: mark.anchor, color: "yellow", selected_text: "text" }),
    }));
    await api.deleteReaderMark("book", mark);
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/books/book/highlights/mark", expect.objectContaining({
      method: "DELETE",
      headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
    }));
  });

  test("queues an optimistic portable highlight when the network is offline", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));
    const mark = await api.createHighlight("book", {
      anchor: { cfi: "epubcfi(/6/2!/4/2,/1:0,/1:4)", atom: 1 },
      color: "green",
      selected_text: "survives the train tunnel",
    });
    expect(mark).toMatchObject({
      kind: "highlight",
      color: "green",
      selected_text: "survives the train tunnel",
      pending: true,
    });
    await expect(api.readerMarks("book")).rejects.toThrow("offline");
  });

  test("posts selected-text and chapter-closeout assistance with CSRF and bounded inputs", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const payload = JSON.stringify({
      insufficient_evidence: true,
      cost: { currency: "USD", usd: "0", calls: [] },
    });
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(payload, {
        status: 200, headers: { "content-type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(payload, {
        status: 200, headers: { "content-type": "application/json" },
      }));
    await api.selectionAction("book", {
      action: "translate", text: "Bonjour", atom: 2, cfi: "epubcfi(/6/4)",
    });
    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "/api/books/book/selection-action",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify({
          action: "translate", text: "Bonjour", atom: 2, cfi: "epubcfi(/6/4)",
          target_language: "English",
        }),
      }),
    );
    await api.chapterCloseout("book", 2);
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "/api/books/book/chapter-closeout",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify({ chapter: 2 }),
      }),
    );
  });

  // Catches: the browser sending a conversation of its own with a question (the server keeps the
  // record and reads no other), and a clear the server refused being taken for done.
  test("a book's conversation is read from the server, never sent to it, and a refused clear is an error", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const point = { id: "a".repeat(64), atom: 2, offset: 120, position_epoch: 0, available_chapter: 2 };
    const answer = { as_of_chapter: 2, insufficient_evidence: false, claims: [{ text: "Ouch.", citation_ids: [] }], citations: [], cost: null };
    const stored = { read_frontier: point, turns: [{ role: "user", content: "Who?", atom: 2, reaction: false, created_at: "2020-01-01T10:00:00+00:00" }] };
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(stored), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(
        `data: ${JSON.stringify({ type: "sentence", text: "Ouch." })}\n\ndata: ${JSON.stringify({ type: "done", answer })}\n\n`,
        { status: 200, headers: { "content-type": "text/event-stream" } }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "unknown book" }), { status: 404 }));
    expect(await api.bookConversation("book")).toEqual(stored);
    expect(fetch).toHaveBeenNthCalledWith(1, "/api/books/book/conversation");
    const heard: string[] = [];
    expect(await api.askBookStream("book", "What did he say?", 2, point, (sentence) => heard.push(sentence))).toEqual(answer);
    expect(heard).toEqual(["Ouch."]);
    expect(fetch).toHaveBeenNthCalledWith(2, "/api/books/book/ask/stream", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ question: "What did he say?", bookmark: 2, read_frontier: point }),
    }));
    await expect(api.clearConversation("book")).resolves.toBeUndefined();
    expect(fetch).toHaveBeenNthCalledWith(3, "/api/books/book/conversation", expect.objectContaining({
      method: "DELETE", headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
    }));
    await expect(api.clearConversation("book")).rejects.toMatchObject({ status: 404 });
  });

  // Catches: a caller's stop not reaching the request. Cancel and a dropped reply abort the fetch
  // itself, so the server sees the browser go before the reply is done and stores nothing; the
  // panel is told it was stopped, not that it took too long.
  test("a stopped question aborts its request, for a book and on the home screen", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const point = { id: "a".repeat(64), atom: 2, offset: 120, position_epoch: 0, available_chapter: 2 };
    const seen: AbortSignal[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      const signal = init!.signal!;
      seen.push(signal);
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    const asks = [
      (stop: AbortSignal) => api.askBookStream("book", "Why?", 2, point, () => {}, stop),
      (stop: AbortSignal) => api.askHome("Why?", () => {}, stop),
      (stop: AbortSignal) => api.askBook("book", "Why?", 2, [], point, stop),
    ];
    for (const [index, ask] of asks.entries()) {
      const stop = new AbortController();
      const reply = ask(stop.signal);
      await vi.waitFor(() => expect(seen).toHaveLength(index + 1));
      const request = seen[index];
      expect(request.aborted).toBe(false);
      stop.abort();
      expect(request.aborted).toBe(true);
      await expect(reply).rejects.toMatchObject({ code: "ask_stopped" });
    }
    expect(seen).toHaveLength(3);
  });

  // Catches: a streamed reply's own reason for stopping (hosted allowance, a reply still in
  // flight) being dropped, so the chats could only show a generic failure.
  test("a streamed reply that ends at the allowance keeps its code and the server's line", async () => {
    const point = { id: "a".repeat(64), atom: 2, offset: 120, position_epoch: 0, available_chapter: 2 };
    const ended = (code: string, message: string) => new Response(
      `data: ${JSON.stringify({ type: "error", status: 429, code, message })}\n\n`,
      { status: 200, headers: { "content-type": "text/event-stream" } });
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(ended("allowance_reached", "Mothli is resting until tomorrow."))
      .mockResolvedValueOnce(ended("companion_busy", "Mothli is still answering you."));
    await expect(api.askBookStream("book", "Who?", 2, point, () => {})).rejects.toMatchObject({
      status: 429, code: "allowance_reached", message: "Mothli is resting until tomorrow.",
    });
    await expect(api.askHome("Who?", () => {})).rejects.toMatchObject({ status: 429, code: "companion_busy" });
  });

  // Catches: a hosted sign-in that ended going unnoticed, leaving the reader on failing screens.
  test("a 401 answer tells the app the reader is signed out; other refusals do not", async () => {
    const heard = vi.fn();
    window.addEventListener(SIGNED_OUT_EVENT, heard);
    try {
      vi.spyOn(globalThis, "fetch")
        .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Authentication required" }), { status: 401 }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "CSRF validation failed" }), { status: 403 }));
      await expect(api.books()).rejects.toMatchObject({ status: 401 });
      expect(heard).toHaveBeenCalledTimes(1);
      await expect(api.books()).rejects.toMatchObject({ status: 403 });
      expect(heard).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(SIGNED_OUT_EVENT, heard);
    }
  });

  // Catches: the reader's last page being lost when the sign-in ends mid-read. The place is kept
  // like an offline one and sent once they are signed in again.
  test("a position refused with 401 is kept for after signing in again", async () => {
    localStorage.setItem("litlet.offline.owner", "05dbebd1-e9e8-4be8-8172-eb65ca8d2a67");
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Authentication required" }), { status: 401 }));
    const saved = await api.putPosition("book", "page-40", 40, 2, 0, 0, "reader-client", 1);
    expect(saved).toMatchObject({ queued: true });
    expect(pendingPosition("book")).toMatchObject({ cfi: "page-40", offset: 40 });
  });

  test("posts an exact-frontier memory correction with CSRF", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-litlet-csrf=csrf-token");
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      as_of_chapter: 3, correction_id: 7, target_entity_id: 8, items: [],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const correction = {
      source_entity_id: 4,
      canonical_name: "Wilhelmina Harker",
      reason: "The full name is established in the chapters already read.",
      bookmark: 3,
    };
    await api.correctMemory("book", correction);
    expect(fetch).toHaveBeenCalledWith(
      "/api/books/book/memory-corrections",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-CSRF-Token": "csrf-token" }),
        body: JSON.stringify(correction),
      }),
    );
  });
});
