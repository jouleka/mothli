"""Cover art for the free library, kept on disk and fetched ahead of the reader.

One cover takes the better part of a second to arrive from the mirror, and a browser asks for only
a handful at a time, so a page of forty would fill in slowly. This keeps every cover it has fetched,
fetches several at once, lets the page say which covers it is about to need (``warm``) so they are
on their way before the pictures are asked for, and never fetches the same cover twice at once: a
request for a cover that is already being fetched waits for that one download."""
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from app.ingest.cover import image_type

WORKERS = 16
WAIT_SECONDS = 20
MAX_WAITING = 400  # covers queued or in flight; beyond this, warming is skipped rather than piled up
MAX_MISSING = 20_000


class CoverCache:
    def __init__(self, folder, *, workers=WORKERS):
        self._folder = Path(folder)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="classics-cover")
        self._lock = threading.Lock()
        self._flights = {}
        self._missing = set()  # books with no usable cover; not asked for again until restart

    def _path(self, pg):
        return self._folder / f"{int(pg)}.img"

    def _on_disk(self, pg):
        try:
            blob = self._path(pg).read_bytes()
        except OSError:
            return None
        return blob if image_type(blob) else None

    def _load(self, entry, fetch):
        pg = entry["pg"]
        try:
            blob = fetch(entry)
            if blob is None or not image_type(blob):
                with self._lock:
                    if len(self._missing) < MAX_MISSING:
                        self._missing.add(pg)
                return None
            self._folder.mkdir(parents=True, exist_ok=True)
            partial = self._folder / f"{int(pg)}.{threading.get_ident()}.part"
            partial.write_bytes(blob)
            partial.replace(self._path(pg))
            return blob
        finally:
            with self._lock:
                self._flights.pop(pg, None)

    def _start(self, entry, fetch, *, optional=False):
        pg = entry["pg"]
        with self._lock:
            flight = self._flights.get(pg)
            if flight is None:
                if pg in self._missing or (optional and len(self._flights) >= MAX_WAITING):
                    return None
                flight = self._flights[pg] = self._pool.submit(self._load, entry, fetch)
            return flight

    def get(self, entry, fetch):
        """The cover's bytes, fetching it if need be; None when the book has no usable cover."""
        blob = self._on_disk(entry["pg"])
        if blob is not None:
            return blob
        flight = self._start(entry, fetch)
        if flight is None:
            return None
        try:
            return flight.result(timeout=WAIT_SECONDS)
        except FutureTimeout:
            return None

    def warm(self, entries, fetch):
        """Start fetching these covers in the background. Returns how many were started."""
        started = 0
        for entry in entries:
            pg = entry["pg"]
            with self._lock:
                under_way = pg in self._flights
            if under_way or self._path(pg).exists():
                continue
            if self._start(entry, fetch, optional=True) is not None:
                started += 1
        return started
