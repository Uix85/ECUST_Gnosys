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
    """NFKC 归一化并去掉所有空白、转小写，作为比较/指纹的统一口径。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def fingerprint(text: str) -> str:
    """段落内容指纹（sha256），用于把注释绑定到“这一版文字”而不是段号。"""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    """历史 semantic_layer 表使用的 md5 文本哈希（与 fingerprint 算法不同，勿混用）。"""
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
    """打开指定书籍的 SQLite；book_id 只允许 `*_data` 形式，防止路径穿越。"""
    safe = os.path.basename(book_id)
    if safe != book_id or not safe.endswith("_data"):
        raise HTTPException(status_code=404, detail="Book not found")
    path = os.path.join(BOOKS_DIR, safe, "book.db")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Book not found")
    return get_db(path)


def chapter_html(book_id: str, chapter_idx: int):
    """回退路径：通过渲染管线取某章 HTML 与章节元数据（含总章数）。"""
    # Import lazily to avoid a cycle with the application router.
    from api.server import _get_chapter_slice
    from reader.book_service import load_book_cached

    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    chapters, chapter, html = _get_chapter_slice(book_id, book, chapter_idx)
    return chapter, html, len(chapters)


def paragraph_blocks(html: str) -> list[dict]:
    """HTML → 段落块 `{text, html, tag}`。

    定位层的构建与“现解回退”**必须**共用这一个实现，否则两边会分叉。
    规则：最内层块级标签为一块；块内 <br> 再切（epub 与 txt 转换器都用 <br> 断行）。
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks = [tag for tag in soup.find_all(BLOCK_TAGS) if not tag.find(BLOCK_TAGS)]
    out: list[dict] = []
    for block in blocks:
        nodes = []

        def flush():
            """把当前累积的行内节点输出为一个段落；空/纯空白段落丢弃。"""
            if not nodes:
                return
            text = "".join(
                str(node) if isinstance(node, NavigableString) else node.get_text()
                for node in nodes
            )
            if normalize_text(text):
                out.append({"text": text, "html": "".join(str(node) for node in nodes),
                            "tag": block.name})
            nodes.clear()

        for node in block.children:
            if isinstance(node, Tag) and node.name.lower() == "br":
                flush()
            else:
                nodes.append(node)
        flush()
    return out


def split_paragraphs(html: str) -> list[str]:
    """HTML → 段落文本列表（定位层用；与 paragraph_blocks 同一实现）。"""
    return [block["text"] for block in paragraph_blocks(html)]


def logical_paragraphs(book_id: str, chapter_idx: int) -> tuple[dict, list[dict], int]:
    """某章的段落（逻辑坐标）。**优先读定位表**，没建过定位的书回退现解 HTML。

    定位表是入库产物（见 backend/reader/locator.py）：读表拿到的是冻结过的段落与段序，
    不再依赖渲染管线（旧路径：chapter_html → _get_chapter_slice → 统一 HTML）。
    """
    from reader import locator
    if locator.has_locator(book_id):
        nodes = locator.load_nodes(book_id)
        chapter_node = next((n for n in nodes if n["kind"] == "chapter"
                             and n["chapter_idx"] == chapter_idx), None)
        if chapter_node is None:
            raise HTTPException(status_code=404, detail="Chapter not found")
        rows = locator.load_paragraphs(book_id, chapter_idx) or []
        paragraphs = [{"paragraph_index": r["para_idx"], "text": r["text"],
                       "fingerprint": r["fingerprint"], "node_idx": r["node_idx"],
                       "seg_in_node": r["seg_in_node"]} for r in rows]
        chapter = {"chapter_idx": chapter_idx, "title": chapter_node["title"],
                   "node_idx": chapter_node["node_idx"],
                   "n_paragraphs": len(paragraphs)}
        return chapter, paragraphs, locator.chapter_count(book_id)

    # ── 回退：没建定位的书（或刚上传还没建）按渲染管线现解 ──
    chapter, html, total = chapter_html(book_id, chapter_idx)
    paragraphs = [{"paragraph_index": i, **block}
                  for i, block in enumerate(paragraph_blocks(html))]
    for para in paragraphs:
        para["fingerprint"] = fingerprint(para["text"])
    return chapter, paragraphs, total


def get_paragraph(book_id: str, chapter_idx: int, paragraph_index: int) -> dict:
    """按逻辑段号取段落，附带章标题与前后段文本（用于拼上下文）。"""
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


def locate(book_id: str, chapter_idx: int, para_index: int) -> dict:
    """段地址 → 完整位置（含所属节点与祖先路径）。定位表缺失时退回现解。"""
    from reader import locator
    if locator.has_locator(book_id):
        return locator.locate(book_id, chapter_idx, para_index)
    item = get_paragraph(book_id, chapter_idx, para_index)
    return {"chapter_idx": chapter_idx, "para_index": para_index, "text": item["text"],
            "fingerprint": item["fingerprint"], "char_count": len(item["text"]),
            "node": None, "node_path": [], "seg_in_node": para_index}


def find_by_fingerprint(book_id: str, fp: str) -> list[dict]:
    """指纹 → 位置列表（内容反查位置；同一段文字可能多处出现，故为列表）。"""
    from reader import locator
    return locator.find_by_fingerprint(book_id, fp) if locator.has_locator(book_id) else []


def semantic_needed(text: str) -> bool:
    """判断该段是否需要语义级解释（规则由 reader.auxiliary_reading 统一提供）。"""
    from reader.auxiliary_reading import needs_semantic

    return needs_semantic(text)


def current_baseline(db, chapter_idx: int, para: dict) -> dict:
    """取某段落当前“已有解释”的基线，供采纳时做并发修改校验。

    返回三个来源（互不覆盖，可能同时存在）：
    * workspace：读者手写注释（workspace_notes，按 L3:{章}:{段} 键）；
    * agent：最新一条 published/accepted 的 agent 笔记（按内容指纹匹配）；
    * legacy：旧 semantic_layer 中的解释（按 md5 text_hash 匹配）。
    """
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
    manual = db.execute("SELECT content,revision,updated_at FROM workspace_notes WHERE key=?", (f"L3:{chapter_idx}:{para['paragraph_index']}",)).fetchone()
    return {
        "workspace": dict(manual) if manual else None,
        "agent": dict(row) if row else None,
        "legacy": dict(old) if old else None,
    }


def list_paragraph_notes(book_id: str, chapter_idx: int) -> list[dict]:
    """按段落顺序列出某章已有解释。

    每段优先取最新一条 published/accepted 的 agent 笔记；没有时回退到旧的
    semantic_layer（kind='para'），并以 origin 标记来源（agent / legacy）。
    """
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
    """正文变更后作废该内容指纹下仍有效的生成注释，返回受影响行数。

    只作废 published/accepted/pending：superseded 已是终态，无需重复处理。
    """
    h = fingerprint(text)
    cur = db.execute(
        "UPDATE agent_notes SET state='superseded' WHERE fingerprint=? AND state IN ('published','accepted','pending')",
        (h,),
    )
    return cur.rowcount
