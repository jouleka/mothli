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


# "chapters 1 to 3 and 5", "chapter 2", "chapters 6, 7 and 9": the numbers after the word.
NAMED_CHAPTERS = re.compile(r"\bchapters?\s+(\d+(?:\s*(?:,|and|to|-|–|—)\s*\d+)*)", re.I)


def named_numbers(phrase):
    """{1, 2, 3, 5} from '1 to 3 and 5'."""
    numbers, parts = set(), re.split(r"\s*(?:,|and)\s*", phrase)
    for part in parts:
        ends = [int(n) for n in re.split(r"\s*(?:to|-|–|—)\s*", part) if n.strip().isdigit()]
        if len(ends) == 2 and ends[0] <= ends[1] and ends[1] - ends[0] < 1000:
            numbers.update(range(ends[0], ends[1] + 1))
        else:
            numbers.update(ends)
    return numbers


def offers_for(sentences, items):
    """What a reply may offer to do: count the chapters it has just said it was not there for.

    Exactly those chapters: the numbers named in the sentence where Mothli says it was not there,
    and of them only chapters before the reader's place not read in full here. A sentence that
    names no chapter, or (with several books) no book, offers nothing. Decided here, never by
    the writer. At most three books.
    """
    named = {}
    for sentence in sentences:
        said = NOT_THERE.search(sentence)
        if not said:
            continue
        chapters = NAMED_CHAPTERS.search(sentence, said.end())
        if not chapters:
            continue
        titled = [item for item in items if item["book"]["title"].casefold() in sentence.casefold()]
        meant = titled if titled else items if len(items) == 1 else []
        for item in meant[:1]:
            named.setdefault(item["book"]["book_id"], set()).update(named_numbers(chapters.group(1)))
    offers = []
    for item in items:
        numbers = named.get(item["book"]["book_id"])
        skipped = skipped_before(item) if numbers else None
        if not skipped:
            continue
        atoms = [atom for atom in skipped["atoms"] if item["numbers"][atom] in numbers]
        if atoms:
            offers.append({"book_id": item["book"]["book_id"], "title": item["book"]["title"],
                           "atoms": atoms, "chapters": spans([item["numbers"][atom] for atom in atoms])})
    return offers[:3]


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
