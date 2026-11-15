"""Voice samples: a fixed list of reader messages, each answered by the real pipeline.

Writes one Markdown file for the owner to read and judge by ear. Nothing is scored. Run it
again after changing how Mothli talks to collect the same messages for a before and after.

  python scripts/voice_samples.py --env-file ../.env --pride PATH/pride.epub

Like companion_eval.py it runs REAL providers on a temporary library and touches no reader data.
The writer goes through the ChatGPT subscription (codex-local) and the checks through Jev, so
only Jev costs money: expected about $0.05 a run. The run stops once Jev spend passes --max-usd.
Chapter notes and the read map are left at their defaults (on), as a reader has them, and the
notes are written by the app's own background writer, not by this script.
"""
import argparse
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from companion_eval import capture_sentences, enter_by_map, karamazov_epub  # noqa: E402

from app.config import Settings  # noqa: E402
from app.llm.jev import JevUnavailable  # noqa: E402
from app.main import create_app  # noqa: E402
from app.read_map import read_snapshot  # noqa: E402
from app.retrieval_passages import normalize_source  # noqa: E402

# The messages, typed the way a reader types them. "follows" continues the conversation of the
# sample before it; every other sample starts a new conversation. Karamazov positions are the
# named ones in companion_eval_cases.json. Keep this list unchanged between a before and an after.
SCENES = [
    {"book": "karamazov", "position": "inside-5",
     "where": "Inside The Brothers Karamazov, read in order: chapters 1 to 4 read, and part of chapter 5.",
     "samples": [
         {"say": "catch me up"},
         {"say": "who is Alyosha again?"},
         {"say": "that chapter wrecked me"},
         {"say": "wait, Adelaida just ran off and left the kid with Fyodor??"},
         {"say": "why did she do that?", "follows": True},
         {"say": "so who looked after the kid after that", "follows": True},
         {"say": "thanks, that helps", "follows": True},
         {"say": "ok but who ends up killing fyodor"},
         {"say": "how do i mark chapters read"},
     ]},
    {"book": "karamazov", "position": "skipped-to-5",
     "where": "Inside The Brothers Karamazov, opened in the middle: only a stretch of chapter 5 "
              "read, chapters 1 to 4 skipped.",
     "samples": [
         {"say": "catch me up"},
         {"say": "what happend in chapter 1 again?"},
         {"say": "but i did read the earlier chapters", "follows": True},
     ]},
    {"book": "pride", "through": 6,
     "where": "Inside Pride and Prejudice, read in order through chapter 6.",
     "samples": [
         {"say": "who's Lizzy again"},
         {"say": "Darcy is unbearable"},
         {"say": "what does charlotte think about marriage?"},
     ]},
    {"book": None,
     "where": "Home screen. On the shelf: The Brothers Karamazov (chapters 1 to 4 read, part of "
              "chapter 5) and Pride and Prejudice (read through chapter 6).",
     "samples": [
         {"say": "where did I leave off?"},
         {"say": "what should i pick up tonight"},
         {"say": "which father is worse so far, Mr. Bennet or Fyodor Pavlovitch?"},
         {"say": "does either book end with a wedding?"},
     ]},
]

# What the reader sees when no sentence passed the checks. Copied from the reader app:
# frontend/src/reader/AskTheBook.tsx (readableBookAnswer) and frontend/src/shelf/HomeChat.tsx.
BOOK_AHEAD = "We haven’t got there yet. I only know the pages you’ve read, so I can’t say what’s coming."
BOOK_NOTHING = ("I can’t find that in what you’ve read so far. If it’s on these pages, point me to "
                "the bit you mean.")
HOME_NOTHING = "We haven’t got there yet, or I don’t have enough from what we’ve read to say."

INTRO = """# Mothli voice samples

**What to do:** read each reply as the reader would. Tick any reply that sounds wrong and say why
in a few words ("stiff", "sounds like a report", "didn't answer me", "too long"). There are no
scores here; your ear is the measure.

Each sample gives the situation, what the reader typed, Mothli's reply exactly as the reader saw
it, and any sentences Mothli wrote that the checks removed before the reader saw them.
"""


class OverBudget(Exception):
    pass


def jev_spend(catalog, book_ids):
    """Jev spend as the books' cost ledgers hold it. A refused or failed call stays in the
    ledger at the price reserved for it, so this can only be too high."""
    return sum(cost["usd"] for bid in book_ids for cost in catalog.get_costs(bid) if "jev" in cost["model"])


def jev_answers(app):
    """One small question to Jev before any writing is paid for. Not in a book's cost ledger."""
    try:
        app.state.reader_client.jev.evaluate(
            {"sentence": "The sky is blue."},
            {"about_sky": {"type": "noul", "instructions": "Is `state.sentence` about the sky?",
                           "criteria": {"true": "It is about the sky.", "false": "It is not."}}})
    except JevUnavailable:
        return False
    return True


def wait_for_notes(app, settings, bid, check_budget):
    """Wait until the app has written its notes for every chapter read in full.

    Marking chapters read starts the app's background writer, as it does for a reader. It
    gives up on a failure and is started again here, but only twice without a new chapter of
    notes: each attempt pays for a writer call and its checks.
    """
    catalog = app.state.catalog
    _point, chapters, *_rest = read_snapshot(bid, catalog, settings, app.state.worker)
    whole = {chapter["ordinal"] for chapter in chapters if chapter["whole"]}
    have, restarts = -1, 0
    while True:
        noted = {note["atom"] for note in catalog.get_chapter_notes(bid)}
        if whole <= noted:
            return
        check_budget()
        if len(noted) > have:
            have, restarts = len(noted), 0
        if app.state.notes_builder.ensure(bid):     # true when the writer had stopped
            restarts += 1
            if restarts > 2:
                raise SystemExit("the app could not write its chapter notes; stopped here")
        time.sleep(2)


def ask(api, bid, message):
    """One reader message. Returns (answer, error): the checked reply, or why there was none."""
    if bid is None:
        response = api.post("/api/reader/ask/stream", json={"question": message})
    else:
        frontier = api.get(f"/api/books/{bid}/read-frontier").json()
        response = api.post(f"/api/books/{bid}/ask/stream",
                            json={"question": message, "history": [], "read_frontier": frontier})
    if response.status_code != 200:
        return {}, f"HTTP {response.status_code}"
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    last = events[-1] if events else {"type": "error", "status": 0, "message": "no reply"}
    if last["type"] == "error":
        return {}, f"error {last['status']}: {last['message']}"
    return last.get("answer", {}), None


def as_shown(answer, home):
    """The reply as the reader app shows it, and whether it is the app's own fixed line."""
    sentences = [claim["text"] for claim in answer.get("claims", [])]
    if sentences:
        return " ".join(sentences), False
    if home:
        return HOME_NOTHING, True
    return (BOOK_AHEAD if answer.get("status") == "ahead" else BOOK_NOTHING), True


def buttons(answer, home):
    """The "I've read…" buttons under a reply, worded as the reader app words them."""
    return [f"I’ve read {offer['chapters']}" + (f" of {offer['title']}" if home else "")
            for offer in answer.get("offers", [])]


def render(results, settings, args):
    lines = [INTRO, f"Collected {date.today().isoformat()}. Writer: {settings.llm_large_model} through the "
             f"ChatGPT subscription. Checks: {args.jev_model}. To collect again: "
             "`backend/scripts/voice_samples.py`.", ""]
    number = 0
    for scene, samples in results:
        lines += ["## " + scene["where"], ""]
        for sample in samples:
            number += 1
            lines += [f"### {number}. “{sample['say']}”", "",
                      "**Situation:** " + scene["where"] + (
                          " Continues the conversation in the sample above." if sample.get("follows")
                          else " A new conversation."), "",
                      "**Reader:** " + sample["say"], ""]
            if sample.get("error"):
                lines += [f"**Mothli:** no reply was collected ({sample['error']}).", ""]
            else:
                lines += ["**Mothli:**" + (" (the app’s own line: nothing Mothli wrote passed the checks)"
                                           if sample["fixed"] else ""), "", "> " + sample["reply"], ""]
                for label in sample["buttons"]:
                    lines += [f"**Button under the reply:** {label}", ""]
                if sample["dropped"]:
                    lines += ["**Written by Mothli, removed by the checks:**", "",
                              *("- " + sentence for sentence in sample["dropped"]), ""]
                else:
                    lines += ["**Removed by the checks:** nothing.", ""]
            lines += ["- [ ] Sounds wrong, because:", ""]
    args.out.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--pride", type=Path, required=True, help="the Pride and Prejudice EPUB")
    parser.add_argument("--out", type=Path, default=BACKEND.parent / "docs" / "product" / "voice-samples.md")
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("companion_eval_cases.json"))
    parser.add_argument("--jev-model", default="jev-1.14.0")
    parser.add_argument("--max-usd", type=float, default=0.15, help="stop once Jev spend passes this")
    args = parser.parse_args()
    named = {position["name"]: position for position in json.loads(
        args.cases.read_text(encoding="utf-8"))["books"]["karamazov"]["positions"]}
    base = Settings(_env_file=args.env_file)
    gates = []
    capture_sentences(gates)
    results = []
    with tempfile.TemporaryDirectory(prefix="mothli-voice-", ignore_cleanup_errors=True) as directory:
        # The eval's settings, with the writer fixed to the ChatGPT subscription. Streamed
        # replies, chapter notes and the read map are not set: they follow their defaults.
        settings = Settings(
            _env_file=None, data_dir=directory, llm_transport="codex-local", allow_stub=False,
            openai_api_key=None, openai_base_url=None,
            llm_cheap_model="gpt-6.1-terra", llm_large_model="gpt-6.1-terra", embed_provider="stub",
            typesafe_api_key=base.typesafe_api_key, jev_windows_credential=base.typesafe_api_key is None,
            jev_enabled=True, jev_model=args.jev_model, reader_page_context=True,
            reader_conversation_pipeline=True, reader_conversation_jev=True,
            reader_conversation_routing=True, reader_conversation_draft_effort="low",
            reader_conversation_review_effort="low", reader_memory_ingest=False,
            reader_answer_cache=False, reader_prepare_enabled=False, recap_prewarm=False,
            classics_enabled=False)
        if not (settings.reader_conversation_stream and settings.reader_story_notes and settings.reader_read_map):
            raise SystemExit("streamed replies, chapter notes and the read map must all be on")
        app = create_app(settings)
        books = {}

        def check_budget():
            spent = jev_spend(catalog, books.values())
            if spent > args.max_usd:
                raise OverBudget(f"Jev spend ${spent:.4f} passed ${args.max_usd}")

        def open_book(api, name):
            if name not in books:
                data = karamazov_epub() if name == "karamazov" else args.pride.read_bytes()
                posted = api.post("/api/books", files={"file": (f"{name}.epub", data, "application/epub+zip")})
                posted.raise_for_status()
                books[name] = posted.json()["book_id"]
            return books[name]

        def read_to(api, name, position):
            """Put the reader where the scene says, then wait for the notes a reader would have."""
            bid = open_book(api, name)
            chapters = app.state.worker.source_chapters(bid)
            enter_by_map(api, app, bid, position, chapters)
            if not (position.get("quote") or position.get("pages")):
                # Read in order to the end of a chapter: the reader's place is its last page.
                atom = position["atom"]
                text = normalize_source(next(c["text"] for c in chapters if c["ordinal"] == atom))
                catalog.set_read_position(bid, incarnation=catalog.get_book(bid)["incarnation"],
                                          atom=atom, offset=len(text))
            wait_for_notes(app, settings, bid, check_budget)
            return bid

        with TestClient(app, raise_server_exceptions=False) as api:
            catalog = app.state.catalog      # made when the app starts
            if not jev_answers(app):
                raise SystemExit("Jev did not answer a one-line question (credits? key?). Every reply "
                                 "sentence needs its check, so nothing was collected.")
            try:
                for scene in SCENES:
                    if scene["book"] == "karamazov":
                        bid = read_to(api, "karamazov", named[scene["position"]])
                    elif scene["book"] == "pride":
                        bid = read_to(api, "pride", {"name": "pride", "atom": scene["through"]})
                    else:
                        # The shelf as the scenes above describe it: Karamazov back to its in-order place.
                        read_to(api, "karamazov", named["inside-5"])
                        bid = None
                    collected = []
                    results.append((scene, collected))
                    for sample in scene["samples"]:
                        if not sample.get("follows"):
                            catalog.clear_turns(bid)
                        gates.clear()
                        started = time.perf_counter()
                        answer, error = ask(api, bid, sample["say"])
                        reply, fixed = as_shown(answer, bid is None)
                        shown = [claim["text"] for claim in answer.get("claims", [])]
                        written = gates[-1]["written"] if gates else []
                        collected.append({**sample, "error": error, "reply": reply, "fixed": fixed,
                                          "buttons": buttons(answer, bid is None),
                                          "dropped": [s for s in written if s not in shown]})
                        render(results, settings, args)
                        print(f"{sum(len(c) for _, c in results)}: {time.perf_counter() - started:.1f}s"
                              + (f" ({error})" if error else ""), flush=True)
                        check_budget()
            except OverBudget as stop:
                print(f"stopped: {stop}", flush=True)
            finally:
                costs = [cost for bid in books.values() for cost in catalog.get_costs(bid)]
                for model in sorted({cost["model"] for cost in costs}):
                    mine = [cost for cost in costs if cost["model"] == model]
                    print(f"{model}: {len(mine)} calls, ${sum(cost['usd'] for cost in mine):.4f}", flush=True)
                print(f"Jev spend: ${jev_spend(catalog, books.values()):.4f}", flush=True)
                # The home conversation opens the reading history; close it so the temporary library can go.
                from app.api import history as history_api
                with history_api._stores_lock:
                    for store in history_api._stores.values():
                        store.close()
                    history_api._stores.clear()
    collected = sum(1 for _, samples in results for sample in samples if not sample["error"])
    print(f"{collected} of {sum(len(scene['samples']) for scene in SCENES)} samples written to {args.out}")


if __name__ == "__main__":
    main()
