"""Small conversational acknowledgments carry no claims about a book.

Only unambiguous short reactions get an immediate clarification. Substantive answers still
use the normal source retrieval and verification paths; conversation is never source evidence.
"""
from __future__ import annotations

import re


def _plain(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.casefold().replace("’", "'")).split())


# Copy reacts to what the reader expressed, without agreeing with a book-specific judgment.
_REACTIONS = (
    ({"omg", "oh my god", "no way", "wait what", "what just happened", "i can t believe that",
      "i can t believe it", "i cant believe that", "i cant believe it"},
     "Quite a moment.", "Which bit caught you off guard?"),
    ({"that was so unfair", "that s so unfair", "that s unfair", "thats so unfair", "thats unfair",
      "this is so unfair"},
     "Oof.", "Which part felt unfair to you?"),
    ({"i hate him", "i hate her", "i hate them"},
     "Oof.", "What did they do that got to you?"),
    ({"i m crying", "im crying", "that hurt", "that s so sad", "thats so sad", "this is so sad"},
     "We can take a moment.", "What part stayed with you?"),
    ({"i love this", "i love him", "i love her", "i love them", "that s adorable", "thats adorable",
      "aww", "awww"},
     "Ooh.", "What won you over?"),
    ({"why did he do that", "why did she do that", "why did they do that",
      "omg why did he do that", "omg why did she do that", "omg why did they do that",
      "how could he do that", "how could she do that", "how could they do that"},
     "Let’s untangle it.", "Who do you mean, and which moment?"),
)


def reaction_copy(question: str) -> tuple[str, str] | None:
    value = _plain(question)
    return next(((ack, clarify) for phrases, ack, clarify in _REACTIONS if value in phrases), None)


def reaction_clarification(question: str, history, frontier: int) -> str | None:
    reaction = reaction_copy(question)
    if reaction is None:
        return None
    # Ignore future context and repeated bare reactions. An earlier substantive reader turn can
    # identify the scene; its facts will still have to be checked against permitted passages.
    has_context = any(message.role == "user" and message.as_of_chapter <= frontier
                      and reaction_copy(message.content) is None for message in history)
    return None if has_context else f"{reaction[0]} {reaction[1]}"
