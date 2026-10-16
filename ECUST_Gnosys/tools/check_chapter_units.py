"""章单位对齐自检（see backend/chapter_units.py）

1) 合成一本迷你书：第 2 个 spine 文件里只有一个小节标题（无顶级标题），
   模拟“转换器把一个章切成两个 chunk”的局面 → 验证“按章取正文”拿到的是完整一章。
2) 对真实书重复执行对齐 → 验证幂等（零改动）。

用法：uv run tools/check_chapter_units.py        （退出码 0 = 通过）
"""

import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

fails = 0


def check(name, cond, extra=''):
    global fails
    print(('  PASS  ' if cond else '  FAIL  ') + name + ('' if cond else '  <<< ' + str(extra)))
    if not cond:
        fails += 1


def make_mini_book(books_dir: str, bid: str = 'mini_data') -> str:
    """四行 spine：封面 / 第一章（标题+2 段）/ 只有小节标题的续块（2 段）/ 第二章。"""
    import schema

    out = os.path.join(books_dir, bid)
    os.makedirs(out, exist_ok=True)
    db = schema.get_db(os.path.join(out, 'book.db'))
    db.execute("INSERT INTO books (title, language, authors, source_file, processed_at, total_chaps, toc_json) "
               "VALUES ('迷你书','zh','[]','mini.txt','2026-01-01T00:00:00',0,'[]')")
    spine = [
        ('chunk_0.html', '封面文字', ['封面文字'], '<p>封面文字</p>'),
        ('chunk_1.html', '第一章 开头', ['第一章正文甲。', '第一章正文乙。'],
         '<h1>第一章 开头</h1><p>第一章正文甲。</p><p>第一章正文乙。</p>'),
        ('chunk_2.html', 'Section 3', ['小节甲正文丙。', '小节甲正文丁。'],
         '<h2>一、小节甲</h2><p>小节甲正文丙。</p><p>小节甲正文丁。</p>'),
        ('chunk_3.html', '第二章 后面', ['第二章正文戊。'], '<h1>第二章 后面</h1><p>第二章正文戊。</p>'),
    ]
    for i, (href, title, texts, html) in enumerate(spine):
        db.execute("INSERT INTO chapters (book_id, spine_order, href, title, content_html, content_text, heading_json) "
                   "VALUES (1, ?, ?, ?, ?, ?, '[]')", (i, href, title, html, ''.join(texts)))
        cid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        for seq, t in enumerate(texts):
            db.execute("INSERT INTO paragraphs (chapter_id, book_id, seq, text, html, offset_start, offset_end, char_count) "
                       "VALUES (?, 1, ?, ?, ?, 0, 0, ?)", (cid, seq, t, f'<p>{t}</p>', len(t)))
        db.execute('UPDATE chapters SET para_count = (SELECT COUNT(*) FROM paragraphs WHERE chapter_id=?) WHERE id=?', (cid, cid))
    db.commit()
    return out


def main() -> int:
    import chapter_units
    import book_service as bs

    tmp = tempfile.mkdtemp(prefix='chapunits_')
    books_dir = os.path.join(tmp, 'books')
    os.makedirs(books_dir)
    old_books_dir = bs.BOOKS_DIR
    bs.BOOKS_DIR = books_dir
    try:
        print('== 1. 合成书：无顶级标题的续块归属上一章 ==')
        make_mini_book(books_dir)
        bid = 'mini_data'
        db = sqlite3.connect(os.path.join(books_dir, bid, 'book.db'))
        db.row_factory = sqlite3.Row
        before = {r['spine_order']: r['para_count'] for r in db.execute('SELECT spine_order, para_count FROM chapters')}
        check('对齐前：续块行持有自己的段落（老行为）', before == {0: 1, 1: 2, 2: 2, 3: 1}, before)
        bs.clear_book_caches(bid)
        rep = chapter_units.align_paragraphs(bid, apply=True)
        after = {r['spine_order']: r['para_count'] for r in db.execute('SELECT spine_order, para_count FROM chapters')}
        check('第 1 行（第一章）收纳了续块的段落 → 4 段', after[1] == 4, after)
        check('续块行被清空（0 段），不再当独立章', after[2] == 0, after)
        check('第二章行不受影响（1 段）', after[3] == 1, after)
        check('封面行没有被并进第一章（1 段）', after[0] == 1, after)
        check('定位失败 0 段', rep['unmatched'] == 0, rep['unmatched'])
        texts = [r['text'] for r in db.execute('SELECT text FROM paragraphs WHERE chapter_id='
                                               '(SELECT id FROM chapters WHERE spine_order=1) ORDER BY seq')]
        check('并库后段落顺序连续（甲/乙/丙/丁）',
              texts == ['第一章正文甲。', '第一章正文乙。', '小节甲正文丙。', '小节甲正文丁。'], texts)
        db.close()

        bs.clear_book_caches(bid)
        rep2 = chapter_units.align_paragraphs(bid, apply=True)
        check('幂等：再对齐一次零改动', sum(r['moved_in'] for r in rep2['rows']) == 0, rep2['rows'])

        print('\n== 2. 真实书：对齐已生效且幂等（只读预演，不改库）==')
        bs.BOOKS_DIR = old_books_dir
        for name in sorted(os.listdir(old_books_dir)):
            if not os.path.isfile(os.path.join(old_books_dir, name, 'book.db')):
                continue
            bs.clear_book_caches(name)
            rep3 = chapter_units.align_paragraphs(name, apply=False)
            moved = sum(r['moved_in'] for r in rep3['rows'])
            check(f'「{name[:26]}」无可移动段落（{rep3["unmatched"]} 段未定位）', moved == 0 and rep3['unmatched'] == 0,
                  f'移动 {moved} 段')
    finally:
        bs.BOOKS_DIR = old_books_dir
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + ('所有检查通过' if not fails else f'{fails} 项未通过'))
    return 1 if fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
