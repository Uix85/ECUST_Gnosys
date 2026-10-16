"""书籍上下文读取与段落定位。"""

import hashlib
import json
import os
import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString, Tag
from fastapi import HTTPException

from db.schema import get_db
from reader.book_service import BOOKS_DIR

BLOCK_TAGS = ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.md5(normalize_text(text).encode("utf-8")).hexdigest()


# 分句规则：三处（L3 段落的 sentences、前端 splitSentences、L4 的定位工具）
# 必须完全一致，否则“逐句解释”和用户看到的一句对不上。定义只留这一份：
#   * 句末标点：。！？!?  以及“词外的” ASCII 句点
#   * 句末后紧跟的引号/括号归给前一句（”）】等）
_SENT_END_HARD = "。！？!?"
_SENT_TAIL = re.compile(r"[”’」』）)】]")


def _is_sentence_end(text: str, i: int) -> bool:
    """句末判定。

    ASCII 句点要特别当心：中文书里的 `让.瓦尔`（译名 OCR 伪影）、`3.5`、`J.W.`、
    `...` 都不是句末。实测 1424 条 L3 记录里就有 1 条因此多切了一句。
    规则：左右邻都是字母/数字（含汉字），或本身就是连续点的一部分 → 不算句末。
    """
    ch = text[i]
    if ch in _SENT_END_HARD:
        return True
    if ch != ".":
        return False
    prev = text[i - 1] if i else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    if prev == "." or nxt == ".":
        return False                      # 省略号/点点点
    return not (prev.isalnum() and nxt.isalnum())


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，吸收尾随引号/括号。与前端 splitSentences 同规则。"""
    out, cur, i, n = [], "", 0, len(text or "")
    while i < n:
        cur += text[i]
        if _is_sentence_end(text, i):
            while i + 1 < n and _SENT_TAIL.match(text[i + 1]):
                i += 1
                cur += text[i]
            out.append(cur)
            cur = ""
        i += 1
    if cur:
        out.append(cur)
    return out


def book_db(book_id: str):
    safe = os.path.basename(book_id)
    if safe != book_id or not safe.endswith("_data"):
        raise HTTPException(status_code=404, detail="Book not found")
    path = os.path.join(BOOKS_DIR, safe, "book.db")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Book not found")
    return get_db(path)


def chapter_html(book_id: str, chapter_idx: int):
    # Import lazily to avoid a cycle with the application router.
    from api.server import _get_chapter_slice
    from reader.book_service import load_book_cached

    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    chapters, chapter, html = _get_chapter_slice(book_id, book, chapter_idx)
    return chapter, html, len(chapters)


def logical_paragraphs(book_id: str, chapter_idx: int) -> tuple[dict, list[dict], int]:
    chapter, html, total = chapter_html(book_id, chapter_idx)
    soup = BeautifulSoup(html, "html.parser")
    blocks = [tag for tag in soup.find_all(BLOCK_TAGS) if not tag.find(BLOCK_TAGS)]
    paragraphs = []
    for block in blocks:
        nodes = []

        def flush():
            if not nodes:
                return
            text = "".join(
                str(node) if isinstance(node, NavigableString) else node.get_text()
                for node in nodes
            )
            if normalize_text(text):
                paragraphs.append({"paragraph_index": len(paragraphs), "text": text})
            nodes.clear()

        for node in block.children:
            if isinstance(node, Tag) and node.name.lower() == "br":
                flush()
            else:
                nodes.append(node)
        flush()
    for para in paragraphs:
        para["fingerprint"] = fingerprint(para["text"])
    return chapter, paragraphs, total


def get_paragraph(book_id: str, chapter_idx: int, paragraph_index: int) -> dict:
    chapter, paragraphs, _ = logical_paragraphs(book_id, chapter_idx)
    if paragraph_index < 0 or paragraph_index >= len(paragraphs):
        raise HTTPException(status_code=404, detail="Paragraph not found")
    result = paragraphs[paragraph_index].copy()
    result.update(
        {"chapter_idx": chapter_idx, "chapter_title": chapter.get("title", "")}
    )
    result["previous"] = (
        paragraphs[paragraph_index - 1]["text"] if paragraph_index else ""
    )
    result["next"] = (
        paragraphs[paragraph_index + 1]["text"]
        if paragraph_index + 1 < len(paragraphs)
        else ""
    )
    return result


def semantic_needed(text: str) -> bool:
    from reader.auxiliary_reading import needs_semantic

    return needs_semantic(text)


def current_baseline(db, chapter_idx: int, para: dict) -> dict:
    row = db.execute(
        "SELECT id, short, detail, sentences, state, updated_at FROM agent_notes "
        "WHERE chapter_idx=? AND paragraph_index=? AND fingerprint=? AND state IN ('published','accepted') "
        "ORDER BY updated_at DESC LIMIT 1",
        (chapter_idx, para["paragraph_index"], para["fingerprint"]),
    ).fetchone()
    old = db.execute(
        "SELECT content, detail, sentences, kind, model_version, updated_at FROM semantic_layer "
        "WHERE text_hash=?",
        (text_hash(para["text"]),),
    ).fetchone()
    return {
        "agent": dict(row) if row else None,
        "legacy": dict(old) if old else None,
    }


def list_paragraph_notes(book_id: str, chapter_idx: int) -> list[dict]:
    db = book_db(book_id)
    _, paragraphs, _ = logical_paragraphs(book_id, chapter_idx)
    notes = []
    for para in paragraphs:
        row = db.execute(
            "SELECT short, detail, sentences, source_text, state FROM agent_notes "
            "WHERE chapter_idx=? AND paragraph_index=? AND fingerprint=? AND state IN ('published','accepted') "
            "ORDER BY updated_at DESC LIMIT 1",
            (chapter_idx, para["paragraph_index"], para["fingerprint"]),
        ).fetchone()
        if row and row["state"] in ("published", "accepted"):
            notes.append(
                {
                    "chapter_idx": chapter_idx,
                    **para,
                    "short": row["short"],
                    "detail": row["detail"],
                    "sentences": json.loads(row["sentences"]),
                    "source_text": row["source_text"],
                    "origin": "agent",
                }
            )
            continue
        legacy = db.execute(
            "SELECT content, detail, sentences, source_text FROM semantic_layer "
            "WHERE text_hash=? AND kind='para' LIMIT 1",
            (text_hash(para["text"]),),
        ).fetchone()
        if legacy:
            try:
                sentences = json.loads(legacy["sentences"] or "[]")
            except (TypeError, ValueError):
                sentences = []
            notes.append(
                {
                    "chapter_idx": chapter_idx,
                    **para,
                    "short": legacy["content"] or "",
                    "detail": legacy["detail"] or "",
                    "sentences": sentences,
                    "source_text": legacy["source_text"] or para["text"],
                    "origin": "legacy",
                }
            )
    return notes


def invalidate_generated_notes(db, text: str) -> int:
    h = fingerprint(text)
    cur = db.execute(
        "UPDATE agent_notes SET state='superseded' WHERE fingerprint=? AND state IN ('published','accepted','pending')",
        (h,),
    )
    return cur.rowcount
