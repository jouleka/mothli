# Mothli language audit — 15 November 2026

Read-and-measure pass on `main` at `0b86e3c`. No product code changed. This file is the only addition.

The question: does what Mothli does work for books and readers in languages other than English?

## Where things stand

Mothli was built and measured on English books, and today it is an English product that tolerates other languages in places. A book in French, German or Spanish opens and can be read, and several low-level parts already handle accents and other alphabets correctly. But the fixed protections against spoilers are written for English: the check for future-tense hints knows only English future forms, and the check for names of people not yet met looks for whole words, which fails in Chinese, Japanese and Thai and for names with case endings (Russian, Polish, Finnish) or attached particles (Arabic, Hebrew). Beyond that, a number of things misbehave: some books lose chapters at import, chapter counts come out wrong, pages in Chinese or Japanese count as read after a second and a half, replies in some languages are checked as one block or cut in the wrong place, and the "I've read…" button never appears unless Mothli happens to answer in English. None of this was measured with real models; how the writer and the judge behave in each language is listed at the end as questions for a later run.

29 findings: **8 spoiler risks, 15 wrong, 6 missing.**

## What "ran" and "read" mean here

| Label | Meaning |
|---|---|
| **Ran** | Short invented texts were pushed through the real functions in this worktree. Numbers are from those runs. |
| **Read** | Established by reading the code at the cited line. Not run. |
| **Model** | Depends on how the writer or judge model behaves. Not measured. No model was called and nothing was paid for. |

What was run: eight throwaway Python scripts against the real backend functions and one throwaway test in the frontend's own test runner. They are not in this commit. Texts were invented, a few sentences each, in French, German, Spanish, Russian, Chinese, Japanese, Arabic, Hindi and Turkish, with spot checks in Italian, Portuguese, Dutch, Polish, Finnish, Greek, Hebrew, Korean, Thai and Urdu. Invented books were built as small EPUB files in memory and passed through the real import code. These are small hand-made samples: they show whether a part works at all in a language, not how often it fails on real books.

Not run: anything in a browser (text direction, page order in right-to-left or vertical books), and anything with a model.

## Findings at a glance

### Spoiler risk: a protection that does nothing, or less, in that language

| # | What | Languages | What the reader could see |
|---|---|---|---|
| S1 | The fixed check for future-tense hints knows only English | Every language except English | A sentence such as "he will die before the winter ends", written in French, German, Russian, Chinese and so on, passes the fixed gate. Only the model checks stand between it and the reader. |
| S2 | The check for names not yet met needs the name to stand as a separate word | Chinese, Japanese, Thai; Korean when a particle is attached | A person the reader has not met is named in a reply and the fixed gate does not notice. |
| S3 | The same check misses a name with an ending or a prefix | Russian, Polish, Finnish and similar; German possessive; Arabic and Hebrew with "and"/"to" attached; Turkish capital İ/I | Same as S2. |
| S4 | The same check misses a name spelled in a different alphabet from the book | Any reader who talks in one language about a book in another alphabet | "Raskolnikov" in a reply about a Russian book, or "Зосима" in a reply about an English one, is not recognised as the stored name. |
| S5 | A page counts as read by its number of words, and words are counted by spaces | Chinese, Japanese, Thai | A page flicked past in two seconds counts as read. Mothli may then talk about pages the reader did not read, and never says "we went through those quickly". |
| S6 | A cast list at the front of a book is not recognised and becomes a chapter | French, German, Spanish, Italian, Arabic, Hindi, Turkish and others | Everyone the cast list names counts as met once that page is opened, including people who appear much later. |
| S7 | A capital letter in mid-sentence is taken as proof the reader has met a name | German | A later character whose name is also an ordinary noun (Wolf, König, Bauer) is treated as already met in the recap and the "right now" line. |
| S8 | The check that every recap sentence can be traced to the facts is weaker outside English | Books with no language tag whose recap comes out in French, Spanish, German, Italian, Russian | An invented event in the recap passes the fixed gate where the same sentence in English is rejected. |

### Wrong: something misbehaves

| # | What | Languages | What the reader sees |
|---|---|---|---|
| W1 | Project Gutenberg books whose contents list says "Chapitre", "Kapitel", "Capítulo"… lose their opening and closing chapters, or are refused | French, German, Spanish, Italian, Portuguese, Dutch, Finnish, Arabic, Hindi, Turkish | "No body chapters detected", or a book where Mothli does not know the first and last chapters and pages there never count as read. |
| W2 | Title page, foreword, contents, acknowledgements and "Part One" pages are counted as chapters | Every language except English, when the file carries no structural markup | "You're in chapter 5 of 11" for Chapitre I of three. "I wasn't with you for chapters 1 to 4" about the title page and foreword. |
| W3 | Accented letters and non-breaking spaces written as named codes in the book file are lost or garbled | Any, but mostly French, German, Spanish and other accented languages | Mothli quotes "caf" for "café"; pages around such text may never count as read. |
| W4 | Finding the right passage for a question relies on shared whole words | Chinese, Japanese, Thai; Russian and other inflected languages in part; any question asked in a different alphabet from the book | "I can't find that in what you've read" about something the reader did read. |
| W5 | The size limit on a request counts bytes, not letters | Russian, Greek, Arabic, Hebrew; Hindi, Thai, Chinese, Japanese, Korean | "Request too large" on a normal question, or sentences silently dropped. |
| W6 | A reply is not split into sentences | Chinese, Japanese, Thai, Urdu; partly Hindi, Arabic, Greek | Nothing appears until the whole reply is written; one doubtful clause loses the whole reply; a long reply is dropped entirely. |
| W7 | A reply is split in the wrong place | German, French, Spanish, Russian | "Du bist im 3." and "Kapitel." are checked as two sentences; fragments are cut or shown alone. |
| W8 | German "will" (wants) is read as English "will" | German | True present-tense sentences are cut. |
| W9 | Chapters in scripts without spaces count as one word | Chinese, Japanese, Thai | In some books a stretch of a chapter is left out as "near-empty". |
| W10 | A recap written in a script without capital letters is always rejected | Arabic, Hebrew, Chinese, Korean (and Japanese, Thai) | No recap appears. Only when the recap is not in English: see S8. |
| W11 | Words in the reader's question are mistaken for names not yet met | German, Russian, Chinese, Japanese, Arabic; any question in another language than the book | "We haven't got there yet" when the right line was "I can't find that". |
| W12 | Matching titles and authors, and search in the free library | Japanese, Russian, Hindi, Tamil, Thai, Turkish; articles in French, German, Spanish | A search misses, or an imported reading record is not matched to the book on the shelf. |
| W13 | The chat has no text direction | Arabic, Hebrew, Persian, Urdu | Replies are laid out left to right, with punctuation at the wrong end. Read only. |
| W14 | The "where you stopped" snippet is cut by spaces and Western full stops | Chinese, Japanese, Thai | The snippet stops mid-sentence, or shrinks to a few characters and is not shown. |
| W15 | Two rare invisible characters are treated differently by the browser and the server | Any | The page containing one never counts as read. Rare. |

### Missing: a feature quietly does not appear

| # | What | Languages | What the reader sees |
|---|---|---|---|
| M1 | The "I've read…" button depends on English words in Mothli's reply | Any reply not in English | No button. Mothli may also ask about the same skimmed chapters again each time. |
| M2 | Tapping a name in the text needs a capital letter | Chinese, Japanese, Korean, Arabic, Hebrew, Hindi, Thai; Russian names in any case but the base form | Names are not underlined and cannot be tapped. |
| M3 | A novel is recognised as a novel only by English words | Every language except English | The book is shown as "sections" with "people mentioned" and "reading notes" instead of chapters and a cast. |
| M4 | The extra help for "who is X?" starts only on the English words "who" or "remind me" | Every language except English | Weaker answers to "Qui est… ?", "Wer ist… ?", "Кто такой… ?". |
| M5 | The app's own words are English only | Every language except English | Buttons, fixed lines and labels in English beside a reply in another language. Mothli may translate a label that exists only in English. |
| M6 | Recap, cast descriptions and chapter summaries are written in English by design | Every book tagged as non-English | An English recap of a French book. |

---

## Detail

### Spoiler risks

**S1. Future-tense hints: English only.** *Ran.*
`backend/app/eval/spoiler_gate/synthesis.py:70-74` (`PROLEPSIS_RE`), used for every chat sentence at `backend/app/stream_conversation.py:353`, and so also for the home screen and chapter notes.
The pattern lists English forms: will, shall, going to, about to, destined to and a few more. Fifteen invented future-tense sentences in French (2), German, Spanish (2), Russian (2), Chinese, Japanese, Arabic, Hindi, Turkish, Italian, Portuguese and Dutch gave 0 hits; both English ones hit. Put through the same decision the chat makes, all nine I tried (one each in French, German, Spanish, Russian, Chinese, Japanese, Arabic, Hindi and Turkish) would be shown; the English one would be dropped.
This matters only if Mothli writes in that language. The writer's instructions (`stream_conversation.py:57-93`) say nothing about which language to answer in. *Model.*

**S2. Names not yet met, in scripts without spaces.** *Ran.*
`backend/app/unicode_text.py:18-30` treats an unbroken run of letters as one word. `synthesis.py:510-512` then looks for the forbidden name among those words.
In Chinese, Japanese and Thai a whole clause is one run, so the name is never a word of its own. On the real fixture store, with a future character added:

| Sentence | Result |
|---|---|
| English sentence with the Chinese name between spaces | caught |
| Chinese running text ("后来林黛玉嫁给了他。") | **missed** |
| Chinese, name between commas | caught |
| Japanese running text, and with a comma before the name | **missed** (the particle は is attached) |
| Thai running text | **missed** |
| Korean, name with particle (김철수가) | **missed** |
| Korean, name alone | caught |

An existing test (`backend/tests/eval/test_spoiler_gate.py:104`) covers non-Latin names only in the first form, inside an English sentence.

**S3. Names with endings or prefixes.** *Ran.* Same code as S2.

| Sentence | Result |
|---|---|
| Russian, base form (Раскольников) | caught |
| Russian, accusative (Раскольникова) and dative (Раскольникову) | **missed** |
| Polish genitive (Kowalskiego), Finnish genitive (Vronskin) | **missed** |
| German plain | caught |
| German possessive (Wronskis Verrat) | **missed** |
| Arabic plain | caught |
| Arabic with "and" or "to" attached (وفرونسكي, لفرونسكي); Hebrew with "and" attached | **missed** |
| Hindi with postposition, Turkish with apostrophe suffix, French, French after "de", Spanish | caught |
| Turkish name Işıl written in capitals (IŞIL) | **missed** |
| "Müller" spelled "Mueller" | **missed** |

Of the 27 sentences in S2 and S3 together: 12 caught, 15 missed.

**S4. A name in a different alphabet from the book.** *Ran; how often it arises is Model.*
Names are stored in the book's own spelling (`backend/app/language.py:22-31`). If the reply spells the name differently, nothing matches. 8 of 9 cases missed: "Raskolnikov", "Lin Daiyu" and "Vronsky" in English replies about Russian, Chinese, Arabic and Japanese books; "Зосима", "ゾシマ", "佐西马" and the French spelling "Zosime" in replies about the English fixture book. Only the identical spelling ("Zossima" in a French sentence) was caught.

**S5. Read or skimmed, by word count.** *Ran, in the frontend test runner.*
`frontend/src/reader/ReaderPage.tsx:916` counts a page's words by splitting on spaces. `frontend/src/reader/pagesRead.ts:15-17` requires a quarter of ordinary reading time for that many words, with a floor of 1.5 seconds.

| Page | Characters | Words counted | Counts as read after |
|---|---|---|---|
| English | 1,554 | 308 | 15.4 s |
| French / German / Spanish | 1,568–1,680 | 294–336 | 14.7–16.8 s |
| Russian / Arabic / Hindi / Turkish / Korean | 658–1,386 | 196–280 | 9.8–14.0 s |
| Chinese | 504 | 1 | **1.5 s** |
| Chinese in 8 paragraphs | 583 | 8 | **1.5 s** |
| Japanese | 602 | 1 | **1.5 s** |
| Thai | 1,134 | 28 | **1.5 s** |

A page left after 1.5 seconds is reported as read, not skimmed, so its text enters the read map and can reach the writer.

**S6. Cast lists.** *Ran.*
`backend/app/ingest/segmentation/signals.py:37-46` recognises a cast list by its heading. Of 11 headings tried, 4 were recognised: "Dramatis Personae", "Действующие лица", "登場人物", "人物表". Not recognised: "Personnages", "Personen", "Personajes", "Personaggi", "الشخصيات", "पात्र", "Kişiler".
In the invented worst-case books (see W2) the cast page became chapter 2 in all ten non-English languages. That includes Russian, Chinese and Japanese, where the heading *was* recognised: a page is only dropped as front matter if nothing before it was kept (`epub_segmenter.py:499-501`), and the title page before it was not recognised.
The file's own header calls a misclassified cast list "a chapter-1 leak". The reader has seen that page, so it is a weaker protection, not text from beyond their place. With structural markup in the file the cast page was dropped correctly in all four languages tried.

**S7. German nouns.** *Ran.*
`synthesis.py:369-390`: a name the reader saw capitalised in mid-sentence is treated as met. German capitalises every noun. With a future character called "Wolf" and read text containing "den Wolf", the recap gate reported no leak; with the English equivalent ("the wolf") it reported one. This is the gate behind the recap and the "right now" line. In the chat both cases pass, in any language, because of a looser rule at `stream_conversation.py:349-350`; that is not a language matter and is noted only so the two are not confused.

**S8. Tracing recap sentences to the facts.** *Ran; when it arises is Model.*
`backend/app/eval/spoiler_gate/grounding.py:41-50` (English function words), `:67-79` (English event words, "-ed" past tense, English auxiliaries), `:126-138`, `:182-185`. The same invented sentence, "Dmitri was murdered in the night", against facts that do not contain it:

| Language of facts and sentence | Share of the sentence's words found in the facts | Result |
|---|---|---|
| English | 0 of 2 | rejected |
| French | 2 of 5 ("dans", "la") | **passes** |
| Spanish | 2 of 5 ("en", "la") | **passes** |
| German | 2 of 4 ("der", "Nacht") | **passes** |
| Italian | 1 of 4 | weak, passes the fixed gate |
| Russian | 1 of 4 | weak, passes the fixed gate |

The related check that binds a named person to a death or arrest (`binding.py:24-88`) found the two English bindings and none in French, German, Spanish or Russian.
By design these checks see English: for a book tagged with a non-English language the model is told to write summaries and recaps in English (`language.py:22-43`). They see another language when the book file has no language tag (the instruction is then not added, `language.py:25`, `:37`) or if the model ignores the instruction.

### Wrong

**W1. Gutenberg-shaped books.** *Ran on invented files; not on real ones.*
`signals.py:29-32` (`CHLABEL`) accepts as a chapter heading: chapter, letter, act, scene, book, part, prologue, epilogue, canto, stave, volume; the Russian and Chinese/Japanese equivalents; or a leading number with a full stop. Of 17 headings tried, 4 were accepted ("Chapter III", "Глава 5", "第三章", "III."). Not accepted: Chapitre, Kapitel, Capítulo, Capitolo, Hoofdstuk, Rozdział, Κεφάλαιο, الفصل, अध्याय, Bölüm, 第3話, 제3장.
This decides whether a Gutenberg file that holds both boilerplate and chapters is kept (`signals.py:104-107`, `:146-147`; `epub_segmenter.py:475-492`).

| Invented book | Contents-list labels | Result |
|---|---|---|
| Header + ch. 1–2 in file 1, ch. 3–4 + licence in file 2 | "Chapter I", "I", "1. Kapitel", "Глава 1", "第一章" | all 4 chapters kept |
| same | "Chapitre I", "Chapitre premier", "Erstes Kapitel", "Capítulo I", "Capitolo I", "Hoofdstuk I", "Ensimmäinen luku" | **import refused**: "no body chapters detected" (`api/books.py:69`) |
| Header + ch. 1–2, then ch. 3–4, then ch. 5–6 + licence | "Chapter N", "Глава N" | all 6 kept |
| same | "Chapitre N", "Kapitel N", "Capítulo N", "Capitolo N", "الفصل N", "अध्याय N", "Bölüm N" | **only chapters 3 and 4 kept**; 1, 2, 5, 6 silently dropped |

English labels that are bare titles ("The Departure") fail the same way, so the shape itself is fragile; English books are protected because they usually say "Chapter".
Why this is not a corner case: the free library bundled with the app lists 77,713 Gutenberg titles, of which 16,200 are not English (French 4,152, Finnish 3,685, German 2,393, Dutch 1,107, Italian 1,106, Spanish 881, Hungarian 654, Portuguese 651, Chinese 435). How many of those files have this shape was not measured.

**W2. Front matter, back matter and part pages.** *Ran.*
`signals.py:23-27`, `:49-73`, `:90-93`; `backend/app/content_scope.py:9-13`. All the word lists are English, with Russian for part pages.
One invented book per language: title page, cast, foreword, part page, two chapters, part page, one chapter, notes, contents, acknowledgements. Neutral file names and no structural markup, which is the worst case.

| Language | "Chapters" found | Expected |
|---|---|---|
| English | 3 | 3 |
| French, German, Spanish, Italian, Russian, Chinese, Japanese, Arabic, Hindi, Turkish | **11 each** | 3 |

Label by label: front-matter headings recognised 2 of 16 ("Preface", "Introduction"); contents, copyright, notes and similar recognised 5 of 34 (the English ones); part-page headings recognised 3 of 16 ("Part I", "Book the First", "Часть 1"; not "Première partie", "Erster Teil", "Часть первая", "第一部").
With structural markup (`epub:type`) on every file, front and back matter were dropped correctly in French, German, Chinese and Arabic, but the two part pages were still counted: 5 chapters, not 3.
Consequences, *read*: Mothli numbers chapters by counting these (`reader_place.py:221-225`), so its "chapter 5 of 11" does not match the book. A foreword the reader skipped stays "not read", and the cast, recap and chapter titles wait until every chapter before the reader's place is finished (`read_map.py:56-72`, `:103-104`).

**W3. Named character codes.** *Ran.*
The import parser is set not to resolve named codes (`epub_segmenter.py:29-30`), for safety against hostile files. `chapter_text.py:64-80` then takes the text as it comes.

| Book file contains | Text Mothli stores |
|---|---|
| literal "café. « Vraiment ? » dit-elle à Noël." | the same |
| `caf&eacute;`, `&laquo;&nbsp;`, `No&euml;l`, with an XHTML doctype | "caf &eacute; . &laquo; &nbsp; Vraiment &nbsp; ? &nbsp; &raquo; dit-elle &agrave; No &euml; l." |
| the same, without a doctype | "caf. Vraiment? dit-elle Nol." |

The reader's screen shows the real letters (the reading engine re-parses such files leniently, `frontend/src/vendor/foliate-js/epub.js:817`, *read*). So the passages Mothli works from are garbled, and the quote the browser sends to prove a page was read cannot be found in the server's text (`read_frontier.py:40-46`), so that page does not count. This also affects English books made the same way (`&mdash;`, `&nbsp;`), but accented languages and French spacing are hit hardest. How common such files are was not measured. Numeric codes (`&#233;`) are fine.

**W4. Finding passages.** *Ran.*
`backend/app/ask.py:374`, `:422-426`; `grounding.py:136-138`. A passage is a candidate when it shares whole words with the question. Three chapters of invented text with one passage that answers the question:

| Book and question | Passages returned | Right passage |
|---|---|---|
| English, French, German, Spanish, Arabic, Hindi (2 questions each) | 3–17 | first |
| Russian, question words in the same form as the text (2) | 3 | first |
| Russian, name in another case ("про Анну") | **0** | not found |
| Chinese (3), Japanese (3), Thai (1) | **0** | not found |
| English question, French book | 3 | first (the shared name "Anna") |
| English question, Russian book; English question, Chinese book | **0** | not found |

Inside a book the chat still receives the last few pages read and the chapter notes, so recent questions can be answered. Questions about earlier chapters cannot. On the home screen only the last thing read in each book is passed (`home_conversation.py:137-141`).
The same word-overlap is used to pick chapter notes when they do not all fit (`chapter_notes.py:67-101`; ran: the relevant note was kept in English and lost in Chinese) and to pick which passages a note sentence is checked against in a long chapter (`chapter_notes.py:114-120`).
Passages in scripts without spaces are also cut at an arbitrary character, not at a sentence end (`retrieval_passages.py:24-39`; ran: 0 of 2 Chinese cuts fell on a full stop). Every character is still covered.

**W5. The size limit counts bytes.** *Ran.*
`backend/app/cost/limits.py:108-116` estimates a request's size as its length in bytes; `:210-216` refuses anything over the limit (`config.py:106`, default 60,000); `:103-105` adds 24,000 when the model is reached through the local Codex route. Latin letters take one byte, Cyrillic, Greek, Arabic and Hebrew two, Hindi, Thai, Chinese, Japanese and Korean three.
One in-book chat request with the maximum of ten full passages:

| Script | Direct | Through the local Codex route | The second look at one sentence, local route |
|---|---|---|---|
| English, French, German, Spanish | 24,579–25,309 | 48,579–49,309 | ok |
| Russian, Greek, Arabic | 36,269–36,519 | **60,269–60,519, refused** | ok |
| Hebrew | 35,819 | 59,819 | ok |
| Korean | 45,839 | **69,839, refused** | **refused** |
| Hindi | 47,719 | **71,719, refused** | **refused** |
| Thai, Chinese, Japanese | 53,899–54,579 | **77,899–78,579, refused** | **refused** |

With a full allowance of chapter notes written in the book's language, Hindi, Thai, Chinese and Japanese also exceed the limit on the direct route (63,085–71,949), and Hebrew on the local route.
A refused reply shows "request too large" (`stream_conversation.py:660-664`). A refused second look drops that sentence without a message (`:381-383`).
Ten full passages is the most a request carries; typical ones are smaller. In Chinese and Japanese the limit is seldom reached today only because W4 returns so few passages. I do not know which route and limit the running app uses.

**W6. Replies not split into sentences.** *Ran.*
`stream_conversation.py:128`: a sentence ends at `. ! ? …` followed by a space. Three-sentence replies:

| Language | Pieces | Shown before the reply is complete |
|---|---|---|
| English, French, Spanish, Russian, Turkish, German | 3 | 2 |
| Chinese, Japanese, Thai, Urdu | **1** | **0** |
| Hindi | 2 (the danda "।" is not an end) | 1 |
| Arabic, Greek (4 sentences) | 3 (the question marks "؟" and ";" are not ends) | 2 |

A piece longer than 400 characters is dropped (`:50`, `:252`). A Chinese reply of 13 short sentences (429 characters) and a Hindi one of 7 (468) were each one piece over the limit, so the whole reply would be lost. Under the limit, the whole reply is judged as one unit: the design of "one fact per sentence, cut only the sentence that goes too far" does not apply.

**W7. Replies split in the wrong place.** *Ran.* `stream_conversation.py:126-129`, `:144-149`.

| Reply | Pieces |
|---|---|
| "Du bist jetzt im 3. Kapitel. Anna kam am 4. Mai nach Berlin." | "Du bist jetzt im 3." / "Kapitel." / "Anna kam am 4." / "Mai nach Berlin." |
| "Das steht in Nr. 5 der Zeitung." | "Das steht in Nr." / "5 der Zeitung." |
| "La Sra. Pérez llegó tarde. Ud. Marcos lo sabía." | "La Sra." / "Pérez llegó tarde." / "Ud." / "Marcos lo sabía." |
| "Это случилось в гл. 3 романа." | "Это случилось в гл." / "3 романа." |
| "Il dit : « Viens. » Elle vint." | "Il dit : « Viens." / "» Elle vint." |
| "Er sagte: „Komm.“ Sie kam. Dann gingen sie." | "Er sagte: „Komm.“ Sie kam." / "Dann gingen sie." (two sentences joined) |

The German case is the one Mothli will hit most: it is how German writes "in chapter 3". Finnish, Czech, Hungarian, Norwegian and Turkish forms with a number and full stop split correctly, because the next word is not capitalised.

**W8. German "will".** *Ran.* `synthesis.py:72-74`.
"Anna will nach Hause gehen" (Anna wants to go home) hits the English future pattern and is dropped. All six invented present-tense sentences containing "will" were dropped; the four without it were kept. The name "Will" also hits, in any language.

**W9. Word counts at import in scripts without spaces.** *Ran.*
`epub_segmenter.py:467` counts words by spaces: a 6,800-character Chinese chapter is 1 word. Two rules use that count. `:608`, `:648`, `:522-526`: a file with no contents-list entry and under 20 words is ignored as near-empty. In an invented book where a chapter runs on into such a file, the English continuation (11,000 characters) was joined to its chapter; the Chinese (3,400) and Japanese (4,000) ones were **dropped**. `signals.py:161-167`: a chapter under 200 words whose label is a bare "Part II" is folded away as a divider; true for a full Chinese or Japanese chapter with that label.

**W10. Recap in a script without capitals.** *Ran; when it arises is Model.*
`grounding.py:117-123`, `:176-181` with `unicode_text.py:39-47`: in a script without capitals every word of two letters or more counts as a name, so every sentence looks like "names only" and is rejected. A sentence copied word for word from the facts was accepted in English, French, German, Russian and Hindi and **rejected** in Arabic, Hebrew, Chinese and Korean. An English sentence with Chinese names in it was accepted, which is what the "write in English" instruction produces. So this bites in the same situations as S8.

**W11. "We haven't got there yet".** *Ran.*
`stream_conversation.py:538-540` takes capitalised words in the reader's question as names and checks them against the read text. When no sentence can be shown, a "name not met" makes the fixed line say "We haven't got there yet" (`AskTheBook.tsx:195`) instead of "I can't find that" (`:197`). The signal fired for: a German question with a noun not in the book ("Mantel"); a Russian name in another case ("Анной"); every Chinese and Japanese question (the whole question is one "name"); an Arabic question; an English question about a Russian book. It did not fire for English-on-English or French-on-French.

**W12. Titles, authors and search.** *Ran.* `backend/app/classics.py:129-132`, `:304-322`; `unicode_text.py:13-14`.
What works: accents, "ß", Cyrillic capitals, Arabic vowel marks, full-width letters. What does not:

- Japanese "ガラス" (glass) and "カラス" (crow) become the same; Russian "мой" and "мои" too.
- Hindi, Tamil and Thai titles are broken at every vowel sign: "गोदान" becomes "ग द न".
- Turkish "Kırmızı" does not match "kirmizi"; "İstanbul" does not match "istanbul" and "IŞIL" does not match "Işıl" where names are compared.
- "æ", "œ", "þ", "ł" are not matched to "ae", "oe", "th", "l".
- Only English articles are dropped: "The Idiot" becomes "idiot", "Der Idiot" stays "der idiot".
- The author is keyed by the last word: "村上 春樹" gives the given name; "Marx und Engels" gives "engels".

**W13. Text direction in the chat.** *Read.*
The page is `lang="en"` (`frontend/index.html:2`); nothing in the app's own code sets a text direction, and the chat styles fix `text-align: left` (`frontend/src/reader/mothli-conversation.css:168`, `:338`). The book itself is laid out by the reading engine, which does handle right-to-left and vertical books. Not checked in a browser.

**W14. "Where you stopped".** *Ran.* `frontend/src/reader/lastPassage.ts:11-22`.
Chinese and Japanese snippets are cut at 420 characters mid-sentence. If the page begins with a short heading and a space ("第一章 …"), the snippet shrinks to that heading (4 characters), which is under the 40-character minimum, so nothing is saved.

**W15. Two invisible characters.** *Ran.* `frontend/src/reader/readFrontier.ts:6`, `backend/app/retrieval_passages.py:20-21`.
The browser treats U+FEFF as white space and the server does not; the server treats U+0085 (and four control characters) as white space and the browser does not. A page whose edge falls near one cannot be matched. All the invisible characters that matter in real text (zero-width space in Thai, joiners in Hindi and Persian, soft hyphen in German) are treated the same by both.

### Missing

**M1. The "I've read…" button.** *Ran.* `backend/app/reader_place.py:92-93`, `:116`, `:141-152`, `:199-218`.
The server offers the button by reading Mothli's own sentence for "wasn't with you", "went through … quickly" and "chapters 2 and 3". Two replies (one of each kind) in each of French, German, Spanish, Russian, Chinese, Japanese, Arabic, Hindi and Turkish: 0 of 18 produced a button. English: 2 of 2.
The same patterns record that Mothli has already asked about skimmed chapters (`:190-196`); in the nine languages nothing was recorded, so the instruction "don't ask again" is never given. Also inside English: "chapters II and III" and "chapters two and three" give no button. Arabic-Indic digits do work.
The button's own words ("I've read chapters 2 and 3", `frontend/src/reader/CountAsRead.tsx`) are English.

**M2. Tapping a name.** *Ran.* `frontend/src/reader/nameTaps.ts:25-27`, `:48`.
A name must start with a capital letter. English, French, German, Greek, Turkish and Russian (base form) names were found. Russian "Анну" was not. For Chinese, Japanese, Korean, Arabic, Hebrew, Hindi and Thai the list of tappable names was empty.

**M3. Novel or not.** *Ran.* `backend/app/ingest/book_type.py:16-30`, `:48`, `:55`, `:74-79`.
A novel is recognised by English verbs ("said", "asked", "went") and long chapters by word count. Twelve long invented chapters of plain narrative: English "novel" (0.94); French, German, Spanish, Russian, Arabic, Hindi, Chinese, Japanese all "unknown" (0.35–0.40; the Chinese and Japanese chapters also counted as "short sections"). An unknown book gets the neutral presentation (`frontend/src/reader/bookProfile.ts`) and the neutral recap and extraction instructions.

**M4. "Who is X?"** *Ran.* `stream_conversation.py:130`, `:555-595`.
The cue is the English word "who" or "remind me". 0 of 9 questions in other languages matched. Without it, the passages where the reader first met the person are not fetched and nicknames are not linked.
If the cue is fixed, the next step has its own problem (`backend/app/known_names.py:69-122`, ran): candidate names are runs of capitalised words. In a German passage that gave 8 candidates of which 7 were ordinary nouns (Abend, Bahnhof, Brief…); in Chinese and Arabic the candidates were whole clauses.

**M5. The app's own words.** *Read.*
Fixed lines (`frontend/src/reader/AskTheBook.tsx:183-198`), the button text, default chapter names ("Chapter 3"), and the text Mothli is given about the app (`reader_place.py:11-23`), which names labels as 'not read', 'part read', 'read', 'Contents', 'read it fresh'. Where Mothli is told the reader's place it is told in English ("chapters 1 to 3 and 5", `reader_place.py:228-278`). Whether Mothli translates the labels when it answers in another language is *Model*.

**M6. English recap by design.** *Read.* `backend/app/language.py:22-43`.
For a book tagged non-English, the extraction and recap instructions add "write in English, keep names in their source spelling". The cast descriptions, chapter summaries and recap are therefore English for a French book. This is what keeps S8 and W10 away, so it should be changed only together with them.

---

## What is not a problem

Confirmed by running:

- **Splitting text into words** handles accents, Cyrillic, Arabic, Hindi vowel signs and Turkish letters correctly (`unicode_text.py`): French, German, Spanish, Russian, Arabic, Hindi and Turkish sentences came out as the right words.
- **Names in other alphabets are caught when they stand as separate words**: Russian base form, Arabic, Hindi with a postposition, Turkish with an apostrophe suffix, French (also after "de"), Spanish, and a Chinese name between spaces or punctuation.
- **Ordinary replies in French, Spanish, Russian, Turkish and German** split into the right three sentences, including French "… !" with its space and Spanish "¿…?".
- **The quote that proves a page was read** matched in all ten cases tried: Chinese with full-width punctuation, Japanese, French with non-breaking and narrow non-breaking spaces, accents stored as combining marks, Arabic with vowel marks, Hindi with joiners, Thai with zero-width spaces, the ideographic space, and characters outside the basic plane. Neither side re-normalises text, so they agree.
- **Passage windows** cover every character in every script.
- **Latin-script languages stay well inside the size limit** (W5 table).
- **Books with structural markup** have front and back matter recognised whatever the language.
- **Russian and Chinese chapter headings** ("Глава", "第…章") and Russian, Chinese and Japanese cast headings are recognised.
- **Arabic-Indic digits** in "chapters ٢ and ٣" are read as 2 and 3.
- **Folding for search** handles accents, "ß", Cyrillic case, Arabic vowel marks, ligatures and full-width forms.

Confirmed by reading only:

- The book's language tag is read at import, normalised and passed to the extraction and recap instructions (`epub_segmenter.py:275-308`, `language.py`).
- The reading engine lays out right-to-left and vertical books (`frontend/src/vendor/foliate-js/`).
- Prompts carry the text as it is; nothing is forced to plain ASCII on the way to the writer or the second look (`ask.py:42`, `stream_conversation.py:232-234`).

Assumed, not confirmed: that the writer and judge models handle these languages well. They generally do, but nothing here measured it, and the instructions they are given are English. See the questions below.

## Questions that need a measured run

Each is listed with the smallest run that would answer it. Q1 decides how much S1, S4 and M1 matter, so it comes first.

1. **Which language does Mothli answer in: the reader's or the book's? And how does it spell names?** One French and one Russian book, six questions each, three asked in the book's language and three in English: 12 replies. Record the language of each reply and the spelling of each name.
2. **Does the judge hold up outside English, and across languages?** Take the repo's existing control sentences (the 28 bad ones and a matching number of true ones), translate them and their passages into French, and run only the sentence check: about 60 judge calls. Repeat for one non-Latin language. Add ten where the sentence is in one language and the passage in another, which is the case when a reader chats in English about a French book, and when a French sentence is checked against the English "where the reader is" text.
3. **Do the model checks catch the future-tense hints the fixed pattern misses?** Five planted "what happens later" sentences in each of French, German, Russian and Chinese through the sentence check: 20 calls.
4. **Are chapter notes written in the book's language, and do they survive the checks at the English rate?** One finished chapter each in French, Russian and Chinese; count sentences kept and dropped against an English chapter of similar length.
5. **Does extraction keep to "write in English, names in source spelling"? What does it do for a book with no language tag?** One chapter each in French, Russian and Chinese, tagged and untagged: 6 calls. Read the output.
6. **Does Mothli translate the app's labels?** "How do I tell you I've read chapter 2?" asked in French, German and Chinese: 3 replies.
7. **No model needed: how common are W1, W2 and W3 in real files?** Import ten Gutenberg books each in French, German, Spanish, Italian, Finnish and Dutch through the import code alone and compare the chapters found with each book's contents list. Costs nothing but needs the files downloaded.
8. **No model needed: a browser check.** Open one Arabic book and one vertical Japanese book; look at the chat with an Arabic reply (W13), and check that turning pages forward still counts as "the next page" for the skim rule (`pagesRead.ts:25-28`).

## Proposed order of fixes

Spoiler risks first, then by how many readers each one touches. Sizes are rough: small is an afternoon, medium a few days, large more than a week or a product decision.

| Order | Fix | Covers | Size |
|---|---|---|---|
| 1 | Count characters, not spaces, for pages in scripts without spaces | S5 | small |
| 2 | Match not-yet-met names inside running text for scripts without spaces; match by stem for inflected names; strip the Arabic and Hebrew one-letter prefixes; fold Turkish İ/I | S2, S3 | medium |
| 3 | Decide how names are spelled in replies (always the book's spelling, or store other spellings), then enforce it | S4 | medium; needs Q1 |
| 4 | Future-tense hints: apply the English pattern to English sentences only, and for other languages either add patterns per language (French, German, Spanish, Italian, Russian first) or send every such sentence to the careful second look with an explicit "does this say what happens later" question | S1, W8 | medium; needs Q1 and Q3 |
| 5 | Cast-list headings in more languages; drop a recognised front-matter page even when an unrecognised one stands before it | S6 | small + medium |
| 6 | Skip the mid-sentence-capital rule for German books | S7 | small |
| 7 | When a book has no language tag, detect the language from its text; refuse to run the English-only recap checks on non-English text rather than pass or fail it by accident | S8, W10 | medium |
| 8 | Chapter words in more languages (chapitre, kapitel, capítulo, capitolo, hoofdstuk, luku, الفصل, अध्याय, bölüm…), then run Q7 | W1 | small |
| 9 | Resolve named character codes from a fixed safe table at import | W3 | small |
| 10 | Sentence ends for Chinese, Japanese, Arabic, Hindi, Urdu, Greek; closing » “ 」; German ordinals; more abbreviations | W6, W7 | small to medium |
| 11 | Estimate request size by script instead of bytes, or shorten passages for two- and three-byte scripts | W5 | small to medium |
| 12 | Passage search that works without spaces (overlapping character pairs) and tolerates endings | W4 (same script) | medium |
| 13 | Front matter, back matter and part-page words in more languages; count characters where there are no spaces | W2, W9 | small per language |
| 14 | Offer the "I've read…" button from what the server already knows about skipped chapters, not from Mothli's wording | M1 | medium |
| 15 | Use the judge's existing "is this an identity question" answer in place of the English word "who" | M4 | small |
| 16 | Recognise a novel by language-neutral signs | M3 | small to medium |
| 17 | Name tapping for scripts without capitals | M2 | small to medium |
| 18 | Small repairs: the "not there yet" signal, title and author matching, chat text direction, the "where you stopped" snippet, the two invisible characters | W11–W15 | small each |
| 19 | Questions in one language about a book in another alphabet | W4 (cross-language) | large |
| 20 | The app's own words, and recaps in the book's or reader's language | M5, M6 | large; product decision |

Items 1, 6, 8 and 9 are small and independent and could go first without waiting for any measured run.

## Not covered

- The older answer paths behind `/ask` (`ask_grounding.py`, `answer_reuse.py`, `conversation_pipeline.py`, `recap_grounding.py`, `reader_reactions.py`, the recap shortcut at `api/views.py:135-141`). They contain more English-only patterns. I read enough to see that, and did not audit them, because the streamed chat does not go through them.
- Reading aloud, highlights and notes, import of FB2 and MOBI files, and reading-history import beyond title matching.
- Fonts: which scripts the app's bundled fonts cover, and what the device falls back to for the rest, was not checked.
- Real books. Every text here was invented and short.
