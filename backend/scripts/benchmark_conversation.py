"""Paired live API benchmark. Use a COPY of the library: calls record real provider spend.

python scripts/benchmark_conversation.py --data-dir scratch --book-id ID --cases cases.json
Cases: [{"id": "followup", "path": "ask", "body": {"question": "Why?", "bookmark": 8}}]
Prints compact JSON lines; no keys, full source dumps, or unchecked draft text.
"""
import argparse
import json
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--fast", action="store_true", help="Include Terra Fast (premium pricing)")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if isinstance(cases, dict):
        cases = cases.get("cases")  # Accept the compact retained validation record too.
    if not isinstance(cases, list) or not 1 <= len(cases) <= 30:
        parser.error("provide one to thirty explicit request cases")
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not isinstance(case.get("body"), dict)
                or case.get("path", "ask") not in {"ask", "selection-action"}):
            parser.error("each case requires an id, request body and a supported read-only path")
    settings = Settings(_env_file=args.env_file, data_dir=args.data_dir,
        llm_cheap_model="gpt-6.1-terra", llm_large_model="gpt-6.1-terra",
        reader_fast_mode=args.fast, reader_answer_cache=False, reader_reuse_enabled=False,
        reader_prepare_enabled=False, recap_prewarm=False,
        reader_conversation_review_effort="low")
    app = create_app(settings)
    modes = ["classic", "conversation"] + (["fast"] if args.fast else [])
    with TestClient(app) as api:
        fast_reader = app.state.reader_client
        for iteration in range(args.repetitions):
            for index, case in enumerate(cases):
                # Alternate order to reduce a systematic first-request/network advantage.
                ordered = modes if (index + iteration) % 2 == 0 else list(reversed(modes))
                for mode in ordered:
                    app.state.settings.reader_conversation_pipeline = mode != "classic"
                    app.state.reader_client = (fast_reader if mode == "fast" else app.state.client)
                    started = perf_counter()
                    response = api.post(f"/api/books/{args.book_id}/{case.get('path', 'ask')}",
                                        json=case["body"])
                    value = response.json()
                    result = {"case": case["id"], "mode": mode, "iteration": iteration + 1,
                        "seconds": round(perf_counter() - started, 4), "status": response.status_code,
                        "insufficient_evidence": value.get("insufficient_evidence"),
                        "as_of_chapter": value.get("as_of_chapter"), "cached": value.get("cached", False),
                        "message": value.get("message"), "claims": value.get("claims", []),
                        "text": value.get("text"), "cost": value.get("cost")}
                    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
