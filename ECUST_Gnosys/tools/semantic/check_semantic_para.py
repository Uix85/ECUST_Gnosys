"""语义层（段落级）自检：确认每条语义条目的段落原文能对上正文段落，且分句数与逐句解释数一致。

对照口径与前端 SemMargin 一致：正文段落 = 章节 HTML 按 <br> 切分，匹配键 = NFKC + 去空白 + 小写。

用法：
    python check_semantic_para.py                     # 检查 books/ 下全部书
    python check_semantic_para.py "江村经济_data"      # 只检查一本
"""

import os
import re
import sys
import json
import unicodedata

# 后端模块在 backend/：脚本已按功能分区到 tools/<区>/ 下，所以往上两级
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'backend'))

from reader.book_service import BOOKS_DIR, load_book_cached
from db.schema import get_db
from api.server import _get_chapter_slice
from reader.auxiliary_reading import needs_semantic
from agent.tools.book import split_sentences

# 分句规则统一取自 agent.tools.book.split_sentences（与前端 splitSentences 同规则），
# 本地不再另写一份 —— 三处分句器漂移过一次（词内 ASCII 句点），代价是逐句解释错位。


def norm(text: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or '')).lower()


def chapter_paragraphs(book_id: str, book, chapters) -> set:
    """该书全部章节的段落规范化文本集合（正文段落 = 按 <br> 切分）"""
    norms = set()
    for idx in range(len(chapters)):
        _, _, html = _get_chapter_slice(book_id, book, idx)
        for seg in re.split(r'<br\s*/?>', html):
            text = re.sub(r'<[^>]+>', '', seg)
            text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                        .replace('&lt;', '<').replace('&gt;', '>'))
            n = norm(text)
            if n:
                norms.add(n)
    return norms


def check_book(folder: str, verbose: bool = False) -> int:
    db = get_db(os.path.join(BOOKS_DIR, folder, "book.db"))
    cols = {r[1] for r in db.execute("PRAGMA table_info(semantic_layer)")}
    if "kind" not in cols:
        return 0   # 未升级的旧库：无段落级条目
    rows = db.execute(
        "SELECT text_hash, source_text, content, detail, sentences FROM semantic_layer WHERE kind = 'para'"
    ).fetchall()
    if not rows:
        return 0

    book = load_book_cached(folder)
    if not book:
        print(f"  [跳过] {folder}：无法加载书籍")
        return 1
    chapters, _, _ = _get_chapter_slice(folder, book, 0)
    norms = chapter_paragraphs(folder, book, chapters)

    bad = 0
    skipped = 0   # 落在“不需要解释”段落上的条目（占位数据会如此，不算错）
    print(f"  {folder}：段落级条目 {len(rows)} 条")
    for r in rows:
        n = norm(r["source_text"])
        label = (r["source_text"] or "")[:18]
        if n not in norms:
            print(f"    [不匹配] {label}… 段落原文对不上正文（前端不会显示）")
            bad += 1
            continue
        if not needs_semantic(r["source_text"] or ""):
            skipped += 1
            continue
        try:
            sents = json.loads(r["sentences"]) if r["sentences"] else []
        except ValueError:
            sents = []
        body = split_sentences(r["source_text"] or "")
        if len(sents) != len(body):
            print(f"    [句数不一致] 正文 {len(body)} 句 / 逐句解释 {len(sents)} 条  {label}…")
            bad += 1
        elif verbose:
            print(f"    OK  {label}…  简短{len(r['content'] or '')}字 详细{len(r['detail'] or '')}字 逐句{len(sents)}条")
    if skipped:
        print(f"    （另有 {skipped} 条落在标题/页眉/书目行上——这些段不需要解释，占位数据会带上，不影响显示）")
    return bad


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "-v"]
    verbose = "-v" in sys.argv
    targets = args or [
        d for d in os.listdir(BOOKS_DIR)
        if os.path.isdir(os.path.join(BOOKS_DIR, d)) and d.endswith("_data")
    ]
    bad = 0
    for folder in targets:
        if folder not in os.listdir(BOOKS_DIR):
            print(f"  [跳过] {folder}：目录不存在")
            continue
        bad += check_book(folder, verbose)
    print("语义层段落自检：" + ("全部通过" if bad == 0 else f"{bad} 处问题"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
