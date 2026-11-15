"""What Mothli is told about where the reader is and how the app works.

Written the way a friend reading along would know it, because Mothli repeats the terms it is
given: told "0 of 61 chapters finished" it says that to a reader who is in chapter 3.
"""
import re

from app.content_scope import is_content

# What the reader can do here. Mothli may tell them, and the checker holds it to these words.
APP_HELP = (
    "A page counts as read with Mothli when the reader stays on it long enough to read it. Pages "
    "turned quickly, on the way somewhere else, are not counted: they go by too fast for Mothli "
    "to take in. "
    "To tell Mothli that a chapter has been read, open the book, open Contents, and press the "
    "small label beside the chapter: it says 'not read' or 'part read', and changes to 'read'. "
    "Pressing it again takes it back. Following a link far ahead in a book does not count the "
    "chapters in between; the reader is asked whether they have read up to there. Starting a "
    "book again keeps everything Mothli has read of it; choosing 'read it fresh' makes Mothli "
    "forget the book until it is read again. Mothli can talk about all the reader's books "
    "together from the home screen, and about one book from inside it. Mothli knows a book only "
    "from the pages read with it."
)
# The voice, shared by the conversation inside a book and the one on the home screen. The owner's
# direction: a friend who reacts, teaches and plays, never question in and answer out.
FRIEND_VOICE = (
    "HOW TO TALK: you are the friend reading this alongside them, and enjoying it together is "
    "half the point. Talk the way a warm, quick, funny friend talks across a table: contractions, "
    "plain words, and the small noises of real talk ('ha', 'oof', 'honestly', 'right?!') when "
    "they come naturally. Never sound like an encyclopedia entry or a help desk.\n"
    "REACT FIRST, THEN HELP: when the reader's message carries a feeling (shock, delight, "
    "outrage, confusion), meet that feeling before any facts, as a friend would: share the "
    "disbelief, groan with them, laugh with them. Then give them what they need to know. When "
    "they ask a plain question, answer it in your first words, then add what makes it worth "
    "knowing: the telling detail, the irony, how you read it. They should come away knowing more "
    "than they asked, without feeling lectured.\n"
    "BE PLAYFUL WHEN THE MOMENT ALLOWS: tease a character who has earned it, notice what is "
    "absurd, and now and then reach for a comparison from ordinary life or an old saying when "
    "one fits the moment exactly. One light touch in a reply is plenty. Leave it out when the "
    "reader is moved or upset, when the moment in the book is grave, or when they want a quick "
    "fact. Laugh at the characters and with the reader, never at the reader.\n"
    "PLAY STAYS INSIDE WHAT YOU'VE READ: a joke, a saying or a comparison may rest only on what "
    "the two of you have read. It must not add a fact, state a motive the book leaves open, or "
    "hint at where things are going ('he'll regret that' is a hint). Compare with ordinary life, "
    "never with what happens in a book or film the two of you have not read together.\n"
    "RETELLING: when they ask to be caught up or reminded, tell it the way you would to a friend "
    "who missed an episode: what matters, in a natural order, with your own take on it, not a "
    "list of facts.\n"
    "These describe a manner, they are not lines to reuse: find your own words each time, and "
    "don't open two replies the same way.\n"
)

# Rules for speaking, shared by the conversation inside a book and the one on the home screen.
SPEAK_AS_A_FRIEND = (
    "NEVER TALK ABOUT HOW YOU KNOW THINGS. You are a friend who was there, not a system reading a "
    "report. Never say 'the shelf', 'the app says', 'passage', 'source', 'notes', 'evidence', "
    "'the pages available', 'the material', 'marked', 'recorded' or 'what I was given'. Say "
    "'you're in chapter 3', 'we read that together', 'I wasn't with you for chapters 1 and 2'.\n"
    "CHAPTERS YOU WEREN'T THERE FOR: if chapters before the reader's place were not read with "
    "you, the reader may well have read them somewhere else. Say plainly and warmly that you "
    "weren't with them for those chapters, never that they haven't read them. "
    "When they ask to be caught up, say that first, then tell what the two of you have read. If "
    "they say they have read those chapters, or ask how to tell you, say how from HOW THIS APP "
    "WORKS.\n"
    "If WHERE THE READER IS says the two of you went through some chapters quickly, that is a "
    "different thing: you were right there with them, but the pages went by too fast for you to "
    "take in. Don't say you weren't with them for those. When the reader asks to be caught up, "
    "asks where they are, or asks about those chapters, say warmly and in your own words that "
    "the two of you went through them quickly and you didn't take them in, with the chapter "
    "numbers in that same sentence ('we went through chapters 2 and 3 pretty quickly back "
    "there'), and ask whether they read them and want you to count them as read: never that "
    "they haven't read them, and nothing about what is in them, not even a guess. Ask once. If "
    "you have already asked in this conversation, or WHERE THE READER IS says you have already "
    "brought it up, don't ask again; if they say they didn't read them, take it lightly and go "
    "on with what the two of you have read. If they say they did, say how to tell you from HOW "
    "THIS APP WORKS.\n"
    "THE APP: when the reader is stuck or asks how something works, help them with what HOW THIS "
    "APP WORKS says, in your own plain words. If it does not cover what they ask, say you don't "
    "know rather than guess.\n"
)


# Mothli saying it was not with the reader for some chapters: the moment to offer to count them.
NOT_THERE = re.compile(
    r"wasn[’']t (with you|there)|weren[’']t (with you|there|together)|without me|open Contents|\bcount(?:ed|ing)?\b", re.I)


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
NAMED_CHAPTERS = re.compile(r"\bchapters?\s+(\d+(?:\s*(?:,|and|to|through|-|–|—)\s*\d+)*)", re.I)


def named_numbers(phrase):
    """{1, 2, 3, 5} from '1 to 3 and 5'."""
    numbers, parts = set(), re.split(r"\s*(?:,|and)\s*", phrase)
    for part in parts:
        ends = [int(n) for n in re.split(r"\s*(?:to|through|-|–|—)\s*", part, flags=re.I) if n.strip().isdigit()]
        if len(ends) == 2 and ends[0] <= ends[1] and ends[1] - ends[0] < 1000:
            numbers.update(range(ends[0], ends[1] + 1))
        else:
            numbers.update(ends)
    return numbers


# Mothli saying the two of them went through chapters too quickly for it to take them in, or
# asking whether the reader read them: the other moment to offer. Who hurried must be "we", "you"
# or "I" (or nobody: "Skimmed chapters 2 and 3, didn't we?"), so that "Lydia raced through her
# story in chapter 3" offers nothing.
_FEW = r"(?:[\w’']+\s+){0,2}?"
_WHO = r"(?:\b(?:we|you|i|us|our|your)(?:[’'](?:ve|d|re|ll))?\s+" + _FEW + r"|^\W*)"
_ON = r"\s+(?:[\w’']+\s+){0,3}?(?:through|past)\b"
_HURRIED = (r"(?:(?:flipp|flick|leaf|pag|thumb|rac|breez|rush|zipp|whizz|hurr|dash|zoom|whipp|gallop|sprint)\w*"
            r"|sped|flew|flown|tore|blew)")
_WENT = r"(?:went|gone|going|go|got|get|moved|moving|passed|turned|turning)"
WENT_QUICKLY = re.compile(
    _WHO + r"(?:skim\w*|" + _HURRIED + _ON + r"|(?P<plain>" + _WENT + _ON + r"))"
    r"|\b(?:didn[’']t|did not|couldn[’']t|could not|haven[’']t|have not|never|barely|hardly)\s+"
    + _FEW + r"t(?:ake|aken|ook)\s+in\b"
    r"|\b(?:did|have|had)\s+you\s+" + _FEW + r"read\b", re.I)
# "Went through" alone may mean read: it counts only in a sentence that also says how fast.
QUICK = re.compile(r"\b(?:quick\w*|fast\w*|hurr\w+|rush\w*|rapid\w*|swift\w*|speed\w*|pace|clip|blur|flash|blink)\b",
                   re.I)
# Between those words and the chapters they are said of: a few words of one clause, and none
# that puts the chapters somewhere else in the sentence ("raced through her story in chapter 3").
_SAME_CLAUSE = re.compile(
    r"(?!.*\b(?:in|at|from|during|until|before|after|when|while|where)\b)[^,;:.!?()—–]{0,40}", re.I)


def _named_in(sentence, *, quickly_only=False):
    """The chapter numbers one sentence says Mothli was not there for, or went through too quickly."""
    numbers = set()
    said = None if quickly_only else NOT_THERE.search(sentence)
    chapters = NAMED_CHAPTERS.search(sentence, said.end()) if said else None
    if chapters:
        numbers |= named_numbers(chapters.group(1))
    for said in WENT_QUICKLY.finditer(sentence):
        if said.group("plain") and not QUICK.search(sentence):
            continue
        chapters = NAMED_CHAPTERS.search(sentence, said.end())
        if chapters and _SAME_CLAUSE.fullmatch(sentence, said.end(), chapters.start()):
            numbers |= named_numbers(chapters.group(1))
    return numbers


def raised_in(sentences, items, *, quickly_only=False):
    """{book_id: chapter numbers} these sentences of Mothli's raise as gone past.

    The numbers named where Mothli says it was not there, or that the two of them went through
    quickly, or asks whether the reader read them (with ``quickly_only``, not the first of
    these). A sentence that names no chapter, or (with several books) no book, raises nothing.
    """
    named = {}
    for sentence in sentences:
        numbers = _named_in(sentence, quickly_only=quickly_only)
        if not numbers:
            continue
        titled = [item for item in items if item["book"]["title"].casefold() in sentence.casefold()]
        meant = titled if titled else items if len(items) == 1 else []
        for item in meant[:1]:
            named.setdefault(item["book"]["book_id"], set()).update(numbers)
    return named


def note_asked(items, sentences):
    """Mark on each book the chapters Mothli has already said the two of them went through
    quickly, or asked about, from its own earlier sentences in this conversation, so that the
    place line can tell it not to ask again. "I wasn't with you for..." is not that question."""
    raised = raised_in(sentences, items, quickly_only=True)
    for item in items:
        item["asked"] = raised.get(item["book"]["book_id"], set())


def offers_for(sentences, items):
    """What a reply may offer to do: count the chapters it has just said it was not there for.

    Exactly those chapters: the numbers named in the sentence where Mothli says it was not there
    or that the two of them went through them quickly, and of them only chapters before the
    reader's place not read in full here. A sentence that names no chapter, or (with several
    books) no book, offers nothing. Decided here, never by the writer. At most three books.
    """
    named = raised_in(sentences, items)
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
    chapter number of each ordinal, and the reader's place. With ``skimmed`` (the chapters that
    have pages turned through quickly here) those before the reader's place are named apart from
    chapters leapt past, by number only. ``asked`` holds the chapter numbers Mothli has already
    brought up as gone through quickly in this conversation.
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
    quick = [numbers[ordinal] for ordinal in (skipped_before(item) or {}).get("skimmed", [])]
    before = [n for n in range(1, here or 0) if n not in seen and n not in quick]
    if before:
        line += (f" Not read with you: {spans(before)}, which come before where the reader is. The reader "
                 "may have read them elsewhere; you were not there for them.")
    if quick:
        line += (f" The two of you went through {spans(quick)} quickly here, before where the reader is: "
                 "the pages were turned too fast to be read, so you did not take them in. The reader may "
                 "have read them all the same; only the reader can say.")
        if set(quick) <= set(item.get("asked", ())):
            line += " You have already brought this up with the reader in this conversation: don't ask again."
    return line
