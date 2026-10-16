"""【章单位对齐】把 paragraphs 的归属对齐到“阅读器真正展示的章”。

背景：阅读器把各 spine 文件（chunk）拼成一份统一 HTML，再按标题锚点/文件边界切章
（book_service._build_chapters + _chapter_slice_anchors）；而入库时 paragraphs 是按
chunk 文件分章的。当一个 chunk 内部没有顶级标题时两者就不一致，例如《精神现象学》的
序言被转换器切成 chunk_1/chunk_2，序言「三、四」节落在 chunk_2 里 —— 阅读器显示完整，
但任何“按章取正文”的流程只看到「一、二」。

本模块按统一 HTML 的章切片重新归属段落，只改 paragraphs.chapter_id 与 chapters.para_count；
章行本身不增不删（渲染用的 content_html、spine 顺序、锚点全都不动）。
入库流程 process_epub_to_sqlite 结束时会自动调用本模块（防复发）。
"""

import html
import os
import re
import sqlite3
from bisect import bisect_right
from typing import List, Optional

import book_service as bs


def _db_path(book_id: str) -> str:
    return os.path.join(bs.BOOKS_DIR, book_id, 'book.db')


def _resolve_pos(text: str, anchor: str, default: int) -> int:
    """锚点 → 统一 HTML 中的字符位置；空锚点/找不到时用 default。"""
    if not anchor:
        return default
    if anchor.startswith('__byte__'):
        try:
            return int(anchor[len('__byte__'):])
        except ValueError:
            return default
    for a in (anchor, anchor.replace('#', '')):
        i = text.find(f'id="{a}"')
        if i >= 0:
            return i
    return default


def _normalize_title(t: str) -> str:
    import unicodedata
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', t or '')).lower()


def _match_row(rows: List[dict], title: str) -> Optional[dict]:
    """与前端 chapterLayerIdxFor 同一套规则：完全相同优先，否则取最长公共前缀（≥4 字）。"""
    want = _normalize_title(title)
    best, best_len = None, 0
    for r in rows:
        t = _normalize_title(r['title'])
        if not t:
            continue
        if t == want:
            return r
        n = min(len(t), len(want))
        k = 0
        while k < n and t[k] == want[k]:
            k += 1
        if k == n and n >= 4 and n > best_len:
            best, best_len = r, n
    return best


def chapter_ranges(book_id: str, book) -> List[dict]:
    """阅读器每一章在统一 HTML 中的字符区间 [{ci, title, start, end}]（位置对原始统一 HTML，不做转义）。"""
    unified = bs._get_unified_html_cached(book_id, book)
    pages = bs._get_toc_pages(book_id, book)
    chapters = bs._build_chapters(pages)
    offsets = bs._get_spine_offsets(book_id, book)
    out = []
    for ci, ch in enumerate(chapters):
        start_a, end_a = bs._chapter_slice_anchors(chapters, pages, book, book_id, ci)
        start = _resolve_pos(unified, start_a, -1)
        if start < 0:
            # 第 0 章（或锚点失效）：用本章起始 spine 文件的边界，把前置页（封面/版权）留在外面
            si = pages[ch['start_page']].get('spine_order')
            start = offsets[si] if si is not None and si < len(offsets) else 0
        end = _resolve_pos(unified, end_a, -1) if ci + 1 < len(chapters) else len(unified)
        if end < 0:
            end = len(unified)
        out.append({'ci': ci, 'title': ch['title'], 'start': start, 'end': max(end, start)})
    return out, unified


_ENT_RE = re.compile(r'&(#\d+|[a-zA-Z][a-zA-Z0-9]*);')


def _dense_index(unified: str) -> tuple:
    """纯文本紧凑串 + 每个字符在原文中的下标。

    跳过空白、HTML 标签与属性（如注入的锚点 <span id="...">），实体按其解码结果写入，
    使比较不受标签/排版/实体差异影响。
    """
    chars, idx = [], []
    i, n = 0, len(unified)
    while i < n:
        c = unified[i]
        if c == '<':
            j = unified.find('>', i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '&':
            m = _ENT_RE.match(unified, i)
            if m:
                for u in html.unescape(m.group(0)):
                    if not u.isspace():
                        chars.append(u)
                        idx.append(i)
                i = m.end()
                continue
        if not c.isspace():
            chars.append(c)
            idx.append(i)
        i += 1
    return ''.join(chars), idx


def align_paragraphs(book_id: str, apply: bool = False) -> dict:
    """按阅读器的章切片重新归属段落。返回报表（dry-run 与 apply 都返回完整计划）。"""
    book = bs.load_book_cached(book_id)
    if book is None:
        return {'book': book_id, 'error': '书加载失败'}
    ranges, unified = chapter_ranges(book_id, book)
    nows, idx_map = _dense_index(unified)

    db = sqlite3.connect(_db_path(book_id))
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute('SELECT id, spine_order, title FROM chapters ORDER BY spine_order')]
    for r in rows:
        r['target'] = _match_row(rows, r['title'])
    # 每个阅读器章 → 目标章行（按标题匹配，规则与前端一致）
    targets = {}
    for c in ranges:
        row = _match_row(rows, c['title'])
        targets[c['ci']] = row['id'] if row else None

    paras = [dict(r) for r in db.execute(
        'SELECT p.id, p.chapter_id, p.seq, p.text, c.spine_order '
        'FROM paragraphs p JOIN chapters c ON c.id = p.chapter_id '
        'ORDER BY c.spine_order, p.seq')]

    starts = [c['start'] for c in ranges]
    cursor = 0
    wanted = {}
    unmatched = 0
    keep_in_place = {c['ci']: 0 for c in ranges}
    for p in paras:
        key = re.sub(r'\s+', '', p['text'] or '')
        if not key:
            continue
        pos = nows.find(key, cursor)
        if pos < 0:
            pos = nows.find(key[:40], 0)
        if pos < 0:
            unmatched += 1
            continue
        cursor = pos + len(key)
        gi = idx_map[pos]
        ci = bisect_right(starts, gi) - 1
        if ci < 0 or gi >= ranges[-1]['end']:
            continue            # 章范围之外（如封面/版权等前置页）→ 保持原归属
        tid = targets.get(ci)
        if tid:
            wanted[p['id']] = tid
        elif ci in keep_in_place:
            keep_in_place[ci] += 1      # 该阅读器章没有对应章行（标题对不上）→ 段落保持原归属

    per_row = {}
    for r in rows:
        per_row[r['id']] = {'title': r['title'], 'spine_order': r['spine_order'], 'before': 0, 'after': 0, 'moved_in': 0, 'moved_out': 0}
    for p in paras:
        per_row[p['chapter_id']]['before'] += 1
        tid = wanted.get(p['id'], p['chapter_id'])
        per_row[tid]['after'] += 1
        if tid != p['chapter_id']:
            per_row[p['chapter_id']]['moved_out'] += 1
            per_row[tid]['moved_in'] += 1

    # 目标章内的新 seq：按阅读顺序（统一 HTML 顺序 = spine 顺序 + seq）重编为 0..n-1，
    # 否则并库后与目标章原有 seq 撞 UNIQUE(chapter_id, seq)
    next_seq = {}
    plan = []
    for p in paras:
        cid = wanted.get(p['id'], p['chapter_id'])
        s = next_seq.get(cid, 0)
        next_seq[cid] = s + 1
        plan.append((cid, s, p['id']))

    report = {
        'book': book_id, 'applied': bool(apply), 'unmatched': unmatched,
        'chapters': [{'ci': c['ci'], 'title': c['title'], 'row': targets.get(c['ci']),
                      'no_target': keep_in_place[c['ci']],
                      'before': per_row[targets[c['ci']]]['before'] if targets.get(c['ci']) else 0,
                      'after': per_row[targets[c['ci']]]['after'] if targets.get(c['ci']) else 0}
                     for c in ranges],
        'rows': [per_row[r['id']] for r in rows],
    }

    if apply:
        with db:
            db.execute('UPDATE paragraphs SET seq = -id')          # 借负值做中转，避开 UNIQUE
            db.executemany('UPDATE paragraphs SET chapter_id = ?, seq = ? WHERE id = ?', plan)
            db.execute('UPDATE chapters SET para_count = (SELECT COUNT(*) FROM paragraphs p WHERE p.chapter_id = chapters.id)')
        report['changed'] = sum(1 for p in paras if wanted.get(p['id'], p['chapter_id']) != p['chapter_id'])
    db.close()
    return report


def format_report(rep: dict) -> str:
    if rep.get('error'):
        return f"  {rep['book']}: {rep['error']}"
    lines = [f"  【{rep['book']}】{'已写入' if rep['applied'] else '预演(dry-run)'}"
             f"{'，移动段落 ' + str(rep.get('changed', 0)) + ' 段' if rep['applied'] else ''}"
             f"，定位失败 {rep['unmatched']} 段"]
    for c in rep['chapters']:
        if c['row'] is None:
            lines.append(f"    第 {c['ci']} 章 {c['title'][:26]:<28} 无对应章行：{c['no_target']} 段保持原归属")
            continue
        flag = '' if c['before'] == c['after'] else f"  ← {c['before']} → {c['after']}"
        lines.append(f"    第 {c['ci']} 章 行#{c['row']}  {c['title'][:26]:<28} {c['after']:>4} 段{flag}")
    for r in rep['rows']:
        if r['moved_in'] or r['moved_out']:
            lines.append(f"    · 行 spine={r['spine_order']} {r['title'][:22]!r}: {r['before']} → {r['after']}"
                         f"（转出 {r['moved_out']}，转入 {r['moved_in']}）")
    return '\n'.join(lines)


def align_all(apply: bool = False, only: Optional[List[str]] = None) -> List[dict]:
    out = []
    for name in sorted(os.listdir(bs.BOOKS_DIR)):
        if only and name not in only:
            continue
        if not os.path.isfile(os.path.join(bs.BOOKS_DIR, name, 'book.db')):
            continue
        bs.clear_book_caches(name)
        out.append(align_paragraphs(name, apply=apply))
    return out
