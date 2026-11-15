"""What Mothli is told about where the reader is and how the app works.

Written the way a friend reading along would know it, because Mothli repeats the terms it is
given: told "0 of 61 chapters finished" it says that to a reader who is in chapter 3.
"""
import re

from app.content_scope import is_content

# What the reader can do here. Mothli may tell them, and the checker holds it to these words.
APP_HELP = (
    "A page counts as read with Mothli when the reader stays on it long enough to read it. Pages "
    "turned quickly, on the way somewhere else, are not counted, so Mothli was not there for them. "
    "To tell Mothli that a chapter has been read, open the book, open Contents, and press the "
    "small label beside the chapter: it says 'not read' or 'part read', and changes to 'read'. "
    "Pressing it again takes it back. Following a link far ahead in a book does not count the "
    "chapters in between; the reader is asked whether they have read up to there. Starting a "
    "book again keeps everything Mothli has read of it; choosing 'read it fresh' makes Mothli "
    "forget the book until it is read again. Mothli can talk about all the reader's books "
    "together from the home screen, and about one book from inside it. Mothli knows a book only "
    "from the pages read with it."
)
# Rules for speaking, shared by the conversation inside a book and the one on the home screen.
SPEAK_AS_A_FRIEND = (
    "NEVER TALK ABOUT HOW YOU KNOW THINGS. You are a friend who was there, not a system reading a "
    "report. Never say 'the shelf', 'the app says', 'passage', 'source', 'notes', 'evidence', "
    "'the pages available', 'the material', 'marked', 'recorded' or 'what I was given'. Say "
    "'you're in chapter 3', 'we read that together', 'I wasn't with you for chapters 1 and 2'.\n"
    "CHAPTERS YOU WEREN'T THERE FOR: if chapters before the reader's place were not read with "
    "you, the reader may well have read them: perhaps quickly, or somewhere else. Say plainly and "
    "warmly that you weren't with them for those chapters, never that they haven't read them. "
    "When they ask to be caught up, say that first, then tell what the two of you have read. If "
    "they say they have read those chapters, or ask how to tell you, say how from HOW THIS APP "
    "WORKS.\n"
    "THE APP: when the reader is stuck or asks how something works, help them with what HOW THIS "
    "APP WORKS says, in your own plain words. If it does not cover what they ask, say you don't "
    "know rather than guess.\n"
)


# Mothli saying it was not with the reader for some chapters: the moment to offer to count them.
NOT_THERE = re.compile(
    r"wasn[’']t (with you|there)|weren[’']t (with you|there|together)|without me|open Contents", re.I)


def skipped_before(item):
    """Chapters before the reader's place that were not read in full here, or None.

    The reader went past them: quickly, by a leap, or having read them somewhere else. Only the
    reader can say which, so this is something to ask, never something to assume.

    ``skimmed`` names those the reader turned through quickly here, so the question can say so.
    A leap past a chapter leaves nothing skimmed.
    """
    numbers, here = item["numbers"], item["point"]["atom"]
    whole = {chapter["ordinal"] for chapter in item["chapters"] if chapter["whole"]}
    atoms = [ordinal for ordinal in sorted(numbers) if ordinal < here and ordinal not in whole]
    if not atoms:
        return None
    seen = item.get("skimmed", set())
    return {"atoms": atoms, "chapters": spans([numbers[ordinal] for ordinal in atoms]),
            "skimmed": [ordinal for ordinal in atoms if ordinal in seen]}


def offers_for(sentences, items):
    """What a reply may offer to do: count the chapters it has just said it was not there for.

    Decided here from the reader's place, never by the writer. At most three books.
    """
    if not NOT_THERE.search(" ".join(sentences)):
        return []
    found = [(item, skipped_before(item)) for item in items]
    return [{"book_id": item["book"]["book_id"], "title": item["book"]["title"],
             "atoms": skipped["atoms"], "chapters": skipped["chapters"]}
            for item, skipped in found if skipped][:3]


def content_numbers(manifest):
    """The chapters of the book proper, numbered in order: {ordinal: number}. Counted plainly,
    whatever the chapters are called, so a book of "Parts" or untitled sections still has them."""
    proper = [atom["ordinal"] for atom in manifest["atoms"] if is_content(atom)]
    return {ordinal: number for number, ordinal in enumerate(proper, 1)}


def spans(numbers):
    """[1, 2, 3, 5] -> 'chapters 1 to 3 and 5'."""
    runs = []
    for number in sorted(numbers):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    parts = [str(a) if a == b else f"{a} and {b}" if b == a + 1 else f"{a} to {b}" for a, b in runs]
    said = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return ("chapter " if len(runs) == 1 and runs[0][0] == runs[0][1] else "chapters ") + said


def place_line(item):
    """Where the reader is in one book.

    ``item`` has the book, its chapters with anything read (each marked ``whole`` or not), the
    chapter number of each ordinal, and the reader's place.
    """
    book, numbers = item["book"], item["numbers"]
    name = f"“{book['title']}” by {book['author'] or 'an unknown author'}"
    total = max(numbers.values(), default=0)
    if not item["chapters"]:
        return f"{name}: not started. You know nothing of what is in it."
    whole = [numbers[ch["ordinal"]] for ch in item["chapters"] if ch["whole"] and ch["ordinal"] in numbers]
    part = [numbers[ch["ordinal"]] for ch in item["chapters"] if not ch["whole"] and ch["ordinal"] in numbers]
    here = numbers.get(item["point"]["atom"])
    if total and len(whole) == total:
        return f"{name}: read all {total} chapters with you."
    line = f"{name}: " + (f"the reader is in chapter {here} of {total} now. " if here else f"{total} chapters. ")
    read = ([spans(whole) + " in full"] if whole else []) + (["part of " + spans(part)] if part else [])
    line += "Read with you: " + ", and ".join(read) + "."
    seen = set(whole) | set(part)
    upcoming = next((n for n in range(1, total + 1) if n not in seen), None)
    if not here and upcoming:
        line += f" The next chapter not read with you is chapter {upcoming}."
    before = [n for n in range(1, here or 0) if n not in seen]
    if before:
        line += (f" Not read with you: {spans(before)}, which come before where the reader is. The reader "
                 "may have read them quickly or elsewhere; you were not there for them.")
    return line
