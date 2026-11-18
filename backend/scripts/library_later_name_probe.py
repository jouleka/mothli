"""The later-name probe of ADR 0042 section 3, with real models. MAKES MODEL CALLS ONLY WITH --run.

The question it answers: when a reader of a famous novel asks Mothli something, and the writer
names a person it remembers from its own reading of the novel who is first met after the
reader's place, does that name reach the reader? Through the library door the fixed name check
cannot know such a name (nothing stamped after the place is handed out), so Jev and the second
look are all that stand in its way. If they let such names through, "version 2" of section 3
(a bare list of later names, only for blocking) goes back to the owner with these results.

    python scripts/library_later_name_probe.py --epub pg1342-images-3.epub --place 3 \\
        --later Wickham --later Lydia --question "Who will Elizabeth end up with?" --run

Without --run it prints what it would do and calls nothing. With --run it uses the writer and
checker the settings name (the environment, or --env-file, exactly as the app reads them; nothing
here chooses a model) and makes, per question, one streamed reply's calls: the writer, Jev on
each sentence, and a second look where Jev doubts. Nothing else is called: story memory is not
built (no extraction).

How, and why it measures the door's case: local mode in a temporary data folder that is deleted
afterwards; the EPUB is imported, chapters 1 to --place are marked read ("I've read this"), and
each question goes through the unchanged streamed chat. Story memory is left empty. The door
hands a reader nothing stamped after their place, so the fixed check's list of names "not met
yet" is empty, exactly as with an empty memory: the probe measures what stops a later name in
the door's case. (The names the reader has met would only add the writer's names note.)

It prints, per question, every sentence shown, and marks each that holds one of the --later
names. The book's text and the replies stay in the terminal; nothing is written anywhere else.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

# Run as a file (python scripts/...), the app package is one folder up.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _NoIngest:
    """No extraction: nothing is submitted, so no model is called to build story memory."""

    def submit(self, *args, **kwargs):
        return None

    def shutdown(self, wait=True, cancel_futures=False):
        return None


def _events(text: str) -> list[dict]:
    return [json.loads(row[6:]) for row in text.splitlines() if row.startswith("data: ")]


def holds(sentence: str, names: list[str]) -> list[str]:
    return [name for name in names
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", sentence)]


def probe(epub: Path, place: int, questions: list[str], later: list[str], env_file) -> int:
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    with tempfile.TemporaryDirectory(prefix="mothli-probe-") as data:
        settings = Settings(_env_file=env_file, data_dir=data, reader_page_context=True,
                            reader_conversation_jev=True, reader_memory_ingest=False,
                            reader_story_notes=False, reader_recollections=False,
                            reader_answer_cache=False)
        app = create_app(settings, ingest_executor=_NoIngest())
        with TestClient(app) as api:
            posted = api.post("/api/books", files={
                "file": (epub.name, epub.read_bytes(), "application/epub+zip")})
            if posted.status_code != 201:
                print(f"the book was not imported ({posted.status_code})")
                return 2
            book = posted.json()["book_id"]
            marked = api.put(f"/api/books/{book}/read-chapters",
                             json={"atoms": list(range(1, place + 1)), "read": True})
            if marked.status_code != 200:
                print(f"chapters 1 to {place} could not be marked read ({marked.status_code})")
                return 2
            leaked = 0
            for question in questions:
                point = api.get(f"/api/books/{book}/read-frontier").json()
                reply = api.post(f"/api/books/{book}/ask/stream",
                                 json={"question": question, "read_frontier": point})
                events = _events(reply.text)
                print(f"\nQ: {question}")
                for event in events:
                    if event["type"] == "sentence":
                        found = holds(event["text"], later)
                        leaked += bool(found)
                        print(("  LATER NAME " + ", ".join(found) + ": " if found else "  ")
                              + event["text"])
                    elif event["type"] == "error":
                        print(f"  (the reply stopped: {event.get('code') or event.get('status')})")
            print(f"\n{leaked} sentence(s) shown holding a later name, over "
                  f"{len(questions)} question(s), at chapter {place}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ADR 0042 section 3: do later names the writer remembers reach a reader?")
    parser.add_argument("--epub", type=Path, required=True)
    parser.add_argument("--place", type=int, required=True,
                        help="the reader's place: chapters 1 to this are marked read")
    parser.add_argument("--question", action="append", required=True)
    parser.add_argument("--later", action="append", required=True,
                        help="a name first met after the place (repeat)")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--run", action="store_true",
                        help="really call the models the settings name (paid or subscription)")
    args = parser.parse_args(argv)
    if args.place < 1 or not args.epub.is_file():
        parser.error("give an EPUB file and a place of at least 1")
    if not args.run:
        print(f"would ask {len(args.question)} question(s) at chapter {args.place} of "
              f"{args.epub.name}, watching for {', '.join(args.later)}; nothing called. "
              "Add --run to call the models the settings name.")
        return 0
    return probe(args.epub, args.place, args.question, args.later, args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
