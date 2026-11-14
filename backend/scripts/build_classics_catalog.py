"""Rebuild ``app/classics_catalog.tsv.gz`` from Project Gutenberg's offline catalogue.

Project Gutenberg asks tools to use its catalogue feeds rather than crawl the site, and grants the
catalogue data to the public domain. This reads the RDF feed (the only feed that carries each book's
rights statement) and keeps every text that is marked public domain in the United States and has an
EPUB edition.

    python scripts/build_classics_catalog.py path/to/rdf-files.tar.bz2

The feed is https://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2 (about 120 MB). The archive
is only read as a stream: nothing in it is extracted to disk. Output rows are sorted by popularity
(downloads in the feed's last 30 days) so that the unfiltered list opens on well-loved books.

Each row also carries what a reader browses by: genres (from Project Gutenberg's own "Category"
bookshelves and subject headings), the size of the plain text (for a reading-time estimate) and the
opening of Project Gutenberg's summary.
"""
import gzip
import io
import re
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "classics_catalog.tsv.gz"
PUBLIC_DOMAIN = "Public domain in the USA."

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
}
RDF_ABOUT = f"{{{NS['rdf']}}}about"
MAX_RDF_BYTES = 8 * 1024 * 1024
BLURB_CHARS = 300
SUMMARY_NOTE = "(This is an automatically generated summary.)"

# genre id -> (bookshelf names, bookshelf prefixes, subject heading parts). Labels live in the app.
GENRES = (
    ("classics", {"classics of literature", "harvard classics", "best books ever listings"}, (), set()),
    ("adventure", {"adventure"}, (), {"adventure stories", "sea stories"}),
    ("romance", {"romance"}, (), {"love stories"}),
    ("mystery", {"crime, thrillers and mystery", "detective fiction", "crime fiction"}, (),
     {"detective and mystery stories"}),
    ("scifi", {"science-fiction & fantasy", "science fiction", "fantasy"}, (), {"science fiction", "fantasy fiction"}),
    ("horror", {"horror", "gothic fiction"}, (), {"horror tales", "ghost stories", "gothic fiction", "vampires"}),
    ("historical", {"historical novels", "historical fiction"}, (), {"historical fiction"}),
    ("humour", {"humour", "humor"}, (), {"humorous stories"}),
    ("children", {"children & young adult reading", "children's literature", "children's fiction",
                  "children's picture books"}, (), {"fairy tales"}),
    ("stories", {"short stories"}, (), {"short stories"}),
    ("poetry", {"poetry"}, (), set()),
    ("plays", {"plays/films/dramas"}, (), set()),
    ("myths", {"mythology, legends & folklore"}, (), set()),
    ("philosophy", {"philosophy & ethics"}, (), set()),
    ("history", set(), ("history - ",), set()),
    ("biography", {"biographies"}, (), set()),
    ("travel", {"travel writing"}, (), set()),
    ("science", {"nature/gardening/animals"}, ("science - ",), set()),
    ("essays", {"essays, letters & speeches"}, (), set()),
    ("western", {"western"}, (), {"western stories"}),
)


def genres_of(shelves, subjects):
    """Genre ids for a book. Magazines and journals get none: they are found by search, not browsed."""
    if "periodicals" in subjects:
        return []
    found = []
    for genre, names, prefixes, headings in GENRES:
        if names & shelves or headings & subjects or any(shelf.startswith(prefixes) for shelf in shelves if prefixes):
            found.append(genre)
    return found


def blurb_of(raw):
    text = squash((raw or "").replace(SUMMARY_NOTE, ""))
    if len(text) <= BLURB_CHARS:
        return text
    cut = text[:BLURB_CHARS]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop >= 120:
        return cut[:stop + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "…"


def squash(text):
    return re.sub(r"\s+", " ", (text or "").replace("\t", " ")).strip()


def tidy_title(raw):
    lines = [squash(line) for line in (raw or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    title = lines[0]
    if len(lines) > 1 and not title.endswith((":", ";", ",", "?", "!", ".")):
        title += ":"
    return squash(" ".join([title, *lines[1:]]))[:240]


def tidy_name(raw):
    """``Wells, H. G. (Herbert George)`` -> ``H. G. Wells``."""
    name = squash(re.sub(r"\([^)]*\)", "", raw or ""))
    if "," in name:
        last, rest = (part.strip() for part in name.split(",", 1))
        suffix = ""
        if "," in rest:  # "Dumas, Alexandre, fils"
            rest, suffix = (part.strip() for part in rest.split(",", 1))
        name = " ".join(part for part in (rest, last) if part)
        if suffix:
            name = f"{name}, {suffix}"
    return name


def year_of(text):
    match = re.search(r"-?\d{1,4}", text or "")
    return int(match.group()) if match else None


def parse(blob):
    root = ET.fromstring(blob)
    ebook = root.find("pgterms:ebook", NS)
    if ebook is None:
        return None
    match = re.search(r"ebooks/(\d+)$", ebook.get(RDF_ABOUT, ""))
    if not match:
        return None
    if squash(ebook.findtext("dcterms:rights", "", NS)) != PUBLIC_DOMAIN:
        return None
    if squash(ebook.findtext("dcterms:type//rdf:value", "Text", NS)) != "Text":
        return None
    files = {node.get(RDF_ABOUT, ""): node for node in ebook.findall("dcterms:hasFormat/pgterms:file", NS)}
    if not any(".epub" in url for url in files):
        return None
    text_bytes = max((int(extent) for url, node in files.items() if ".txt" in url
                      and (extent := squash(node.findtext("dcterms:extent", "", NS))).isdigit()), default=0)
    shelves = {squash(node.text).lower().removeprefix("category: ")
               for node in ebook.findall("pgterms:bookshelf//rdf:value", NS)}
    subjects = {part.strip().lower() for node in ebook.findall("dcterms:subject//rdf:value", NS)
                for part in re.split(r"\s+--\s+", node.text or "")}
    title = tidy_title(ebook.findtext("dcterms:title", "", NS))
    if not title:
        return None
    creators = [tidy_name(agent.findtext("pgterms:name", "", NS))
                for agent in ebook.findall("dcterms:creator/pgterms:agent", NS)]
    creators = [name for name in dict.fromkeys(creators) if name]
    # Every contributor counts for the copyright clock (authors, translators, illustrators): the
    # latest death year, or 0 when any of them has no recorded death year.
    deaths = [year_of(agent.findtext("pgterms:deathdate", "", NS)) for agent in root.iter(f"{{{NS['pgterms']}}}agent")]
    died = 0 if (not deaths or any(year is None for year in deaths)) else max(deaths)
    language = squash(ebook.findtext("dcterms:language//rdf:value", "", NS)).lower()[:12] or "und"
    downloads = squash(ebook.findtext("pgterms:downloads", "0", NS))
    downloads = int(downloads) if downloads.isdigit() else 0
    return {
        "pg": int(match.group(1)),
        "language": language,
        "died": died,
        "downloads": downloads,
        "kb": min(round(text_bytes / 1024), 99999),
        "genres": " ".join(genres_of(shelves, subjects)),
        "title": title,
        "author": ", ".join(creators[:3])[:160],
        "blurb": blurb_of(ebook.findtext("pgterms:marc520", "", NS)),
    }


def main(source):
    rows = []
    with tarfile.open(source, mode="r|*") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".rdf") or member.size > MAX_RDF_BYTES:
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            try:
                row = parse(handle.read())
            except ET.ParseError:
                continue
            if row:
                rows.append(row)
    rows.sort(key=lambda row: (-row["downloads"], row["pg"]))
    text = io.StringIO()
    columns = ("pg", "language", "died", "downloads", "kb", "genres", "title", "author", "blurb")
    text.write("\t".join(columns) + "\n")
    for row in rows:
        text.write("\t".join(str(row[column]) for column in columns) + "\n")
    with OUT.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as out:
        out.write(text.getvalue().encode("utf-8"))
    print(f"{len(rows)} books -> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
