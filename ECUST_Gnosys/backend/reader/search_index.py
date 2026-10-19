"""In-process bigram index for substring search over locator paragraphs.

The index is intentionally not persisted into book.db: book databases are
application data, and a search cache must not grow their binary size.
"""

import threading

from agent.tools.book import normalize_text

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple[int, str | None], list[tuple[int, int, str]], dict[str, list[int]]]] = {}
_MAX_CANDIDATES = 5000


def _book_pk(db) -> int | None:
    row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    return row[0] if row else None


def _grams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _source_signature(db, pk: int) -> tuple[int, str | None]:
    row = db.execute(
        "SELECT COUNT(*), MAX(built_at) FROM book_paragraphs WHERE book_id=?",
        (pk,),
    ).fetchone()
    return int(row[0] or 0), row[1]


def _build(db, pk: int) -> tuple[list[tuple[int, int, str]], dict[str, list[int]]]:
    rows = db.execute(
        "SELECT chapter_idx, para_idx, text FROM book_paragraphs "
        "WHERE book_id=? ORDER BY chapter_idx, para_idx",
        (pk,),
    ).fetchall()
    entries, postings = [], {}
    for row in rows:
        entry_id = len(entries)
        entries.append((row["chapter_idx"], row["para_idx"], row["text"]))
        for gram in _grams(normalize_text(row["text"])):
            postings.setdefault(gram, []).append(entry_id)
    return entries, postings


def search_paragraphs(book_id: str, db, query: str, limit: int = 50):
    """Return matching (chapter_idx, para_idx, text) rows, or None if index is inapplicable."""
    normalized = normalize_text(query)
    if len(normalized) < 2:
        return None
    pk = _book_pk(db)
    if pk is None:
        return None
    source = _source_signature(db, pk)
    if source[0] == 0:
        return None

    with _LOCK:
        cached = _CACHE.get(book_id)
        if not cached or cached[0] != source:
            entries, postings = _build(db, pk)
            _CACHE.clear()
            _CACHE[book_id] = (source, entries, postings)
            cached = _CACHE[book_id]
        entries, postings = cached[1], cached[2]
        grams = list(dict.fromkeys(
            normalized[i:i + 2] for i in range(len(normalized) - 1)
        ))
        lists = [postings.get(gram) for gram in grams]
        if any(not posting for posting in lists):
            return []
        candidate_ids = set(lists[0])
        for posting in lists[1:]:
            candidate_ids.intersection_update(posting)
            if not candidate_ids:
                return []
        results = []
        for entry_id in sorted(candidate_ids)[:_MAX_CANDIDATES]:
            chapter_idx, para_idx, text = entries[entry_id]
            if normalized in normalize_text(text):
                results.append((chapter_idx, para_idx, text))
        return results
