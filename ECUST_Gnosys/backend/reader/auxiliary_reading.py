"""
辅助阅读（auxiliary_reading）—— 四层级 AI 辅助功能专用模块。

独立于基础阅读器（book_service.py）与路由层（server.py），
承载概念层/语义层/全书层/章节层等"项目专用功能"的读取与后续生成逻辑。
目前：L1 全书 / L2 章节 / L3 语义 / L4 概念 读取。
"""

import os
import re
import json
import hashlib
import unicodedata
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException

from db.schema import get_db
from reader.book_service import BOOKS_DIR


router = APIRouter(prefix="/api/layer", tags=["layer"])


def _normalize_text(text: str) -> str:
    """【文本规范化】NFKC 统一全半角 + 去全部空白 + 小写（用于 L3 语义层键）"""
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).lower()


def _text_hash(text: str) -> str:
    """【文本哈希】L3 语义层键 = MD5(规范化文本)"""
    return hashlib.md5(_normalize_text(text).encode('utf-8')).hexdigest()


# ── 是否需要段落级语义解释（与前端 SemMargin.needsNote 同一判据）──
# 在《精神现象学》1738 段上实测：1738 → 1426 段需要、312 段不需要（均为标题/页眉/书目行，未误杀正文）
# ① 规范化后不足 25 字；② 没有句末标点（中文 。！？ / 西文 .!?）；③ 行首为目录式序号
_HEADING_RE = re.compile(
    r'^[\[［【(（]?(第[一二三四五六七八九十百零两0-9]+[章节篇部分卷]'
    r'|[一二三四五六七八九十]+[、．.]'
    r'|[0-9]+[、．.)）])'
)
_HEADING_MAX = 60   # 只在“标题那么短”时才按行首序号排除（章标题与正文粘成一段时不能误杀）


def needs_semantic(text: str) -> bool:
    """该段是否需要语义层解释（标题、页眉、书目行不需要）"""
    t = (text or '').strip()
    n = _normalize_text(t)
    if len(n) < 25:
        return False
    term = re.compile(r'[。！？]') if re.search(r'[\u4e00-\u9fff]', t) else re.compile(r'[.!?]')
    if not term.search(t):
        return False
    if len(n) <= _HEADING_MAX and _HEADING_RE.match(n):
        return False
    return True


def _book_db(book_id: str):
    from agent.book import book_db
    return book_db(book_id)


@router.get("/book/{book_id}")
async def get_book_layer(book_id: str):
    """【全书层API】L1：查询 book_layer 全书层内容；无则返回空"""
    db = _book_db(book_id)
    _ensure_columns(db, "book_layer", {"model_version": "TEXT", "updated_at": "TEXT"})
    row = db.execute(
        "SELECT content, model_version, updated_at FROM book_layer LIMIT 1"
    ).fetchone()
    if not row:
        return {"content": "", "model_version": "", "updated_at": "", "matched": False}
    return {"content": row["content"], "model_version": row["model_version"],
            "updated_at": row["updated_at"], "matched": True}


@router.get("/chapter/{book_id}/{chapter_idx}")
async def get_chapter_layer(book_id: str, chapter_idx: int):
    """【章节层API】L2：按章节索引（spine_order）查询 chapter_layer 内容；无则返回空"""
    db = _book_db(book_id)
    _ensure_columns(db, "chapter_layer", {"model_version": "TEXT", "updated_at": "TEXT"})
    # 先解析书籍主键（book_id 路由参数是文件夹名，chapters.book_id 是整数外键）
    bk = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    if not bk:
        raise HTTPException(status_code=404, detail="Book not found")
    row = db.execute(
        "SELECT cl.content, cl.model_version, cl.updated_at, ch.title "
        "FROM chapters ch LEFT JOIN chapter_layer cl ON cl.chapter_id = ch.id "
        "WHERE ch.book_id = ? AND ch.spine_order = ?",
        (bk["id"], chapter_idx),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Chapter not found")
    if not row["content"]:
        return {"chapter_idx": chapter_idx, "title": row["title"], "content": "",
                "model_version": "", "updated_at": "", "matched": False}
    return {"chapter_idx": chapter_idx, "title": row["title"], "content": row["content"],
            "model_version": row["model_version"], "updated_at": row["updated_at"],
            "matched": True}


@router.get("/concept/{book_id}")
async def get_concept(book_id: str, term: str = ""):
    """【概念层API】按词条查询概念层内容；未匹配则返回该书第一条作为填充物"""
    db = _book_db(book_id)
    _ensure_columns(db, "concept_layer", {"term": "TEXT", "source_text": "TEXT", "anchor_text": "TEXT"})
    if term:
        row = db.execute("SELECT term, content, source_text, anchor_text FROM concept_layer WHERE term = ? LIMIT 1", (term,)).fetchone()
        if row:
            return {"term": row["term"], "content": row["content"], "matched": True,
                    "source_text": row["source_text"] or "", "anchor_text": row["anchor_text"] or ""}
    return {"term": term, "content": "", "matched": False}



@router.get("/semantic/{book_id}")
async def get_semantic(book_id: str, text: str = ""):
    """【语义层API】按选中文本 MD5 查询语义层内容；未匹配则返回该书第一条作为填充物"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", _SEM_COLS)
    if text:
        h = _text_hash(text)
        row = db.execute("SELECT content, source_text, anchor_text FROM semantic_layer WHERE text_hash = ? LIMIT 1", (h,)).fetchone()
        if row:
            return {"text_hash": h, "content": row["content"], "matched": True,
                    "source_text": row["source_text"] or "", "anchor_text": row["anchor_text"] or ""}
    return {"text_hash": "", "content": "", "matched": False}



# ═══════════════════════════════════════════════════════════
# 记忆写入 / 删除（选中即缓存 → layer 表，后续再选直接命中）
# 内容当前为固定填充物；接入 AI 后只需替换生成逻辑，接口不变。
# 旧骨架库（缺 text_hash/term 列）在写入时自动 ALTER 补齐。
# ═══════════════════════════════════════════════════════════

def _book_int_id(db):
    """books 主键（表内 book_id 是整数外键）"""
    row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    return row["id"] if row else None


def _ensure_columns(db, table: str, cols: dict):
    """层表缺目标列（旧骨架库）时 ALTER 补齐，保证可写入"""
    existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in cols.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _first_row(db, table, cols):
    """返回层表第一条（未匹配查询时的填充物）"""
    return db.execute(f"SELECT {cols} FROM {table} LIMIT 1").fetchone()


# 语义层全列（旧库缺列时 _ensure_columns 自动 ALTER 补齐）
_SEM_COLS = {
    "text_hash": "TEXT", "source_text": "TEXT", "anchor_text": "TEXT",
    "detail": "TEXT", "sentences": "TEXT", "kind": "TEXT",
}


def _para_item(r):
    """段落级语义条目 → 前端结构（sentences 为逐句解释数组，与正文分句顺序一致）"""
    try:
        sents = json.loads(r["sentences"]) if r["sentences"] else []
    except (ValueError, TypeError):
        sents = []
    return {
        "text_hash": r["text_hash"], "source_text": r["source_text"] or "",
        "short": r["content"] or "", "detail": r["detail"] or "",
        "sentences": sents if isinstance(sents, list) else [],
    }


@router.post("/semantic/{book_id}")
async def save_semantic(book_id: str, text: str = Body(...), content: str = Body(""),
                        source_text: str = Body(""), anchor_text: str = Body("")):
    """【语义层写入】按 MD5(规范化文本) upsert 记忆；旧库缺列自动补齐。
    source_text=选中原文（恢复高亮用），anchor_text=所在段落前缀（精确定位用）。"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", _SEM_COLS)
    from agent.book import invalidate_generated_notes
    invalidate_generated_notes(db, source_text or text)
    bid = _book_int_id(db)
    # 记忆功能：content 缺省时统一填充全书层文本（占位，后续 AI 生成替换）
    if not content.strip():
        raise HTTPException(status_code=422, detail="请先生成或填写解释，不写入占位内容")
    h = _text_hash(text)
    src = source_text or text
    anc = anchor_text or ""
    row = db.execute("SELECT id FROM semantic_layer WHERE text_hash = ?", (h,)).fetchone()
    if row:
        db.execute("UPDATE semantic_layer SET content=?, model_version=?, updated_at=?, source_text=?, anchor_text=? WHERE id=?",
                   (content, "manual-v1", _now(), src, anc, row["id"]))
    else:
        db.execute("INSERT INTO semantic_layer (book_id, text_hash, content, model_version, updated_at, source_text, anchor_text) VALUES (?,?,?,?,?,?,?)",
                   (bid, h, content, "manual-v1", _now(), src, anc))
    db.commit()
    return {"ok": True, "text_hash": h}


@router.delete("/semantic/{book_id}")
async def delete_semantic(book_id: str, text: str = ""):
    """【语义层删除】删除指定文本的记忆（前端书签 × 号触发）"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", {"text_hash": "TEXT"})
    if text:
        h = _text_hash(text)
        from agent.book import invalidate_generated_notes
        invalidate_generated_notes(db, text)
        db.execute("DELETE FROM semantic_layer WHERE text_hash = ?", (h,))
        db.commit()
        return {"ok": True, "deleted": True, "text_hash": h}
    return {"ok": False, "deleted": False}


@router.post("/concept/{book_id}")
async def save_concept(book_id: str, term: str = Body(...), content: str = Body(""),
                      source_text: str = Body(""), anchor_text: str = Body("")):
    """【概念层写入】按词条名 upsert 记忆（键与 get_concept 查询一致，保证命中）；旧库缺列自动补齐。
    source_text=词条原文，anchor_text=所在段落前缀（恢复高亮用）。"""
    db = _book_db(book_id)
    _ensure_columns(db, "concept_layer", {
        "term": "TEXT", "model_version": "TEXT", "updated_at": "TEXT",
        "source_text": "TEXT", "anchor_text": "TEXT",
    })
    bid = _book_int_id(db)
    # 记忆功能：content 缺省时统一填充全书层文本（占位，后续 AI 生成替换）
    if not content.strip():
        raise HTTPException(status_code=422, detail="请先生成或填写解释，不写入占位内容")
    t = (term or "").strip()
    src = source_text or t
    anc = anchor_text or ""
    row = db.execute("SELECT id FROM concept_layer WHERE term = ?", (t,)).fetchone()
    if row:
        db.execute("UPDATE concept_layer SET content=?, model_version=?, updated_at=?, source_text=?, anchor_text=? WHERE id=?",
                   (content, "manual-v1", _now(), src, anc, row["id"]))
    else:
        db.execute("INSERT INTO concept_layer (book_id, term, content, model_version, updated_at, source_text, anchor_text) VALUES (?,?,?,?,?,?,?)",
                   (bid, t, content, "manual-v1", _now(), src, anc))
    db.commit()
    return {"ok": True, "term": t}


@router.delete("/concept/{book_id}")
async def delete_concept(book_id: str, term: str = ""):
    """【概念层删除】删除指定词条的记忆"""
    db = _book_db(book_id)
    _ensure_columns(db, "concept_layer", {"term": "TEXT"})
    if term:
        t = (term or "").strip()
        db.execute("DELETE FROM concept_layer WHERE term = ?", (t,))
        db.commit()
        return {"ok": True, "deleted": True, "term": t}
    return {"ok": False, "deleted": False}


# ═══════════════════════════════════════════════════════
# 全书层 / 章节层 写入（笔记界面保存用）
# 与读取接口同键（book_id / spine_order），保证命中；接口签名兼容后续 AI 生成
# （AI 生成只需替换调用方，把 content 换成模型输出即可，路由不变）。
# ═══════════════════════════════════════════════════════

@router.post("/book/{book_id}")
async def save_book_layer(book_id: str, content: str = Body(default="", embed=True)):
    """【全书层写入】upsert book_layer（L1，键=book_id）"""
    db = _book_db(book_id)
    bid = _book_int_id(db)
    now = _now()
    row = db.execute("SELECT id FROM book_layer WHERE book_id = ?", (bid,)).fetchone()
    if row:
        db.execute("UPDATE book_layer SET content=?, model_version=?, updated_at=? WHERE id=?",
                   (content, "manual-v1", now, row["id"]))
    else:
        db.execute("INSERT INTO book_layer (book_id, content, model_version, updated_at) VALUES (?,?,?,?)",
                   (bid, content, "manual-v1", now))
    db.commit()
    return {"ok": True, "updated_at": now}


@router.post("/chapter/{book_id}/{chapter_idx}")
async def save_chapter_layer(book_id: str, chapter_idx: int, content: str = Body(default="", embed=True)):
    """【章节层写入】upsert chapter_layer（L2，chapter_idx=spine_order，键=chapter_id）"""
    db = _book_db(book_id)
    bid = _book_int_id(db)
    if not bid:
        raise HTTPException(status_code=404, detail="Book not found")
    ch = db.execute(
        "SELECT id FROM chapters WHERE book_id = ? AND spine_order = ?",
        (bid, chapter_idx),
    ).fetchone()
    if not ch:
        raise HTTPException(status_code=404, detail="Chapter not found")
    now = _now()
    row = db.execute("SELECT id FROM chapter_layer WHERE chapter_id = ?", (ch["id"],)).fetchone()
    if row:
        db.execute("UPDATE chapter_layer SET content=?, model_version=?, updated_at=? WHERE id=?",
                   (content, "manual-v1", now, row["id"]))
    else:
        db.execute("INSERT INTO chapter_layer (chapter_id, book_id, content, model_version, updated_at) VALUES (?,?,?,?,?)",
                   (ch["id"], bid, content, "manual-v1", now))
    db.commit()
    return {"ok": True, "chapter_idx": chapter_idx, "updated_at": now}


# ═══════════════════════════════════════════════════════
# 语义层·段落级（预生成）：每段一条「简短解释 + 整段详细解释 + 逐句解释」
# 键 = MD5(规范化段落原文)，前端按 source_text 在正文里匹配段落（右栏灰字展示）
# kind='para' 与解释笔划词条目（kind 空/'sel'）分区，互不干扰
# ═══════════════════════════════════════════════════════

@router.get("/para_semantic/{book_id}")
async def list_para_semantic(book_id: str, chapter: int = -1):
    """【语义层·段落级读取】返回该书段落级条目（含 source_text 供前端定位段落）。
    带 chapter（阅读器章索引）时只取该章条目：先算出该章段落的哈希，再按哈希查库
    （整书条目可能上千、几 MB，不整书读盘；分批 IN 避开 SQLite 变量数上限）。"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", _SEM_COLS)
    if chapter >= 0:
        from agent.book import list_paragraph_notes
        return {"items": list_paragraph_notes(book_id, chapter)}
    from agent.book import list_paragraph_notes
    from reader import locator
    chapter_count = locator.chapter_count(book_id) if locator.has_locator(book_id) else 0
    if chapter_count:
        merged = []
        for chapter_idx in range(chapter_count):
            merged.extend(list_paragraph_notes(book_id, chapter_idx))
        return {"items": merged}
    cols = "text_hash, source_text, content, detail, sentences"
    rows = []
    if chapter >= 0:
        norms = _chapter_para_norms(book_id, chapter)
        if norms is not None:
            hashes = [_text_hash(n) for n in norms]
            for i in range(0, len(hashes), 400):
                chunk = hashes[i:i + 400]
                q = ",".join("?" * len(chunk))
                rows += db.execute(
                    f"SELECT {cols} FROM semantic_layer WHERE kind = 'para' AND text_hash IN ({q})",
                    chunk,
                ).fetchall()
            return {"items": [_para_item(r) for r in rows]}
    rows = db.execute(
        f"SELECT {cols} FROM semantic_layer WHERE kind = 'para' ORDER BY id"
    ).fetchall()
    return {"items": [_para_item(r) for r in rows]}


def _chapter_para_norms(book_id: str, chapter_idx: int):
    """该章正文段落规范化集合（与前端同口径：章节 HTML 按 <br> 切分、去标签、规范化）。
    返回值：集合 = 正常；空集 = 该章号不存在（请求方按“该章无条目”处理）；
    None = 取不到（不过滤，宁多不少：前端仍按原文精确匹配，不会误挂）。"""
    try:
        from api.server import _get_chapter_slice    # 延迟导入：应用路由反向 include 本模块
        from reader.book_service import load_book_cached
        book = load_book_cached(book_id)
        if not book:
            return None
        _, _, html = _get_chapter_slice(book_id, book, chapter_idx)
        norms = set()
        for seg in re.split(r'<br\s*/?>', html):
            seg = re.sub(r'<[^>]+>', '', seg)
            seg = (seg.replace('&nbsp;', ' ').replace('&amp;', '&')
                       .replace('&lt;', '<').replace('&gt;', '>'))
            n = _normalize_text(seg)
            if n:
                norms.add(n)
        return norms
    except HTTPException as e:
        if e.status_code == 404:
            return set()   # 章号非法 → 该章没有条目（避免把整书几条 MB 的条目当兜底返回）
        return None
    except Exception:
        return None


@router.post("/para_semantic/{book_id}")
async def save_para_semantic(book_id: str, text: str = Body(...), content: str = Body(""),
                            detail: str = Body(""), sentences: list = Body(default=[])):
    """【语义层·段落级写入】按 MD5(规范化段落原文) upsert（kind='para'）。
    text=段落原文（source_text，前端据此匹配段落）；content=简短解释；
    detail=整段详细解释；sentences=逐句解释数组（顺序与正文分句一致）。
    后续 AI 生成只需把这三项换成模型输出，路由不变。"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", _SEM_COLS)
    from agent.book import invalidate_generated_notes
    invalidate_generated_notes(db, text)
    bid = _book_int_id(db)
    h = _text_hash(text)
    now = _now()
    sents = json.dumps(sentences, ensure_ascii=False) if sentences else None
    row = db.execute("SELECT id FROM semantic_layer WHERE text_hash = ?", (h,)).fetchone()
    if row:
        db.execute(
            "UPDATE semantic_layer SET content=?, detail=?, sentences=?, kind='para', "
            "model_version=?, updated_at=?, source_text=? WHERE id=?",
            (content, detail, sents, "manual-v1", now, text, row["id"]),
        )
    else:
        db.execute(
            "INSERT INTO semantic_layer (book_id, text_hash, content, detail, sentences, kind, "
            "model_version, updated_at, source_text) VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, h, content, detail, sents, "para", "manual-v1", now, text),
        )
    db.commit()
    return {"ok": True, "text_hash": h}


@router.delete("/para_semantic/{book_id}")
async def delete_para_semantic(book_id: str, text: str = ""):
    """【语义层·段落级删除】带 text 删该段条目；不带 text 清空该书全部段落级条目
    （重新生成前清场用；只动 kind='para'，不碰解释笔划词条目）"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", _SEM_COLS)
    if text:
        h = _text_hash(text)
        from agent.book import invalidate_generated_notes
        invalidate_generated_notes(db, text)
        cur = db.execute("DELETE FROM semantic_layer WHERE text_hash = ? AND kind = 'para'", (h,))
    else:
        cur = db.execute("DELETE FROM semantic_layer WHERE kind = 'para'")
    db.commit()
    return {"ok": True, "deleted": cur.rowcount}


# ═══════════════════════════════════════════════════════
# 章节层·列表（阅读器章索引可能与 chapters.spine_order 错位，用标题对齐）
# ═══════════════════════════════════════════════════════

@router.get("/chapter_list/{book_id}")
async def list_chapter_layer(book_id: str):
    """【章节层列表】返回该书各章 {chapter_idx, chapter_id, title, has_content}。
    供阅读器把“当前显示章”对到正确的章节层条目（两边章序在部分书上并不一致）。"""
    db = _book_db(book_id)
    bk = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    if not bk:
        raise HTTPException(status_code=404, detail="Book not found")
    rows = db.execute(
        "SELECT ch.spine_order, ch.id AS chapter_id, ch.title, "
        "       CASE WHEN cl.content IS NULL OR TRIM(cl.content) = '' THEN 0 ELSE 1 END AS has_content "
        "FROM chapters ch LEFT JOIN chapter_layer cl ON cl.chapter_id = ch.id "
        "WHERE ch.book_id = ? ORDER BY ch.spine_order",
        (bk["id"],),
    ).fetchall()
    return {"items": [
        {"chapter_idx": r["spine_order"], "chapter_id": r["chapter_id"],
         "title": r["title"] or "", "has_content": bool(r["has_content"])}
        for r in rows
    ]}


# ═══════════════════════════════════════════════════════
# 语义层 / 概念层 列表（笔记界面 L3/L4 展示用）
# 阅读页仍用单条查询（semantic/{id}?text= / concept/{id}?term=）
# ═══════════════════════════════════════════════════════

@router.get("/semantic_list/{book_id}")
async def list_semantic(book_id: str):
    """【语义层列表】返回该书解释笔划词条目（笔记界面 L3 展示 / 阅读页恢复高亮）；
    段落级预生成条目（kind='para'）由 /para_semantic 单独提供，不在此列。"""
    db = _book_db(book_id)
    _ensure_columns(db, "semantic_layer", _SEM_COLS)
    rows = db.execute(
        "SELECT text_hash, content, source_text, anchor_text FROM semantic_layer "
        "WHERE COALESCE(kind, 'sel') <> 'para' ORDER BY id"
    ).fetchall()
    return {"items": [
        {"text_hash": r["text_hash"], "content": r["content"] or "",
         "source_text": r["source_text"] or "", "anchor_text": r["anchor_text"] or ""}
        for r in rows
    ]}


@router.get("/concept_list/{book_id}")
async def list_concept(book_id: str):
    """【概念层列表】返回该书全部概念层条目（笔记界面 L4 展示 / 阅读页恢复高亮）"""
    db = _book_db(book_id)
    _ensure_columns(db, "concept_layer", {"term": "TEXT", "source_text": "TEXT", "anchor_text": "TEXT"})
    rows = db.execute(
        "SELECT term, content, source_text, anchor_text FROM concept_layer ORDER BY id"
    ).fetchall()
    return {"items": [
        {"term": r["term"], "content": r["content"] or "",
         "source_text": r["source_text"] or "", "anchor_text": r["anchor_text"] or ""}
        for r in rows
    ]}
