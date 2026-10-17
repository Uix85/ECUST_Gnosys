"""Import into a private staging directory, then atomically publish a unique book."""

import html
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import chardet
from db.schema import close_db, get_db
from reader.import_epub import _extract_headings, _split_paragraphs, _write_epub


def _write_txt(path, output_root):
    raw = Path(path).read_bytes()
    if not raw or b'\x00' in raw[:512] and not raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        raise ValueError('TXT 文件为空或不是文本')
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            encoding = chardet.detect(raw).get('encoding') or 'gb18030'
            text = raw.decode(encoding)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip():
        raise ValueError('TXT 没有正文')
    from agent.outline import heading_level
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sections, title, content = [], Path(path).stem, []
    for line in lines:
        level = heading_level(line)
        if level == 0 and content:
            sections.append((title, content))
            content = []
        if level == 0:
            title = line
        tag = 'h' + str(min(6, level + 1)) if level is not None else 'p'
        content.append(f'<{tag}>{html.escape(line)}</{tag}>')
    if content:
        sections.append((title, content))
    output = Path(output_root) / (Path(path).stem + '_data')
    output.mkdir()
    db = get_db(str(output / 'book.db'))
    toc = [{'title': title, 'href': f'chapter{i}.html', 'file_href': f'chapter{i}.html', 'anchor': '', 'children': []}
           for i, (title, _) in enumerate(sections)]
    db.execute('INSERT INTO books(title,language,authors,source_file,processed_at,total_chaps,toc_json) VALUES(?,?,?,?,?,?,?)',
               (Path(path).stem, 'zh', '[]', Path(path).name, datetime.now(timezone.utc).isoformat(), len(sections), json.dumps(toc, ensure_ascii=False)))
    bid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    total = 0
    for i, (title, parts) in enumerate(sections):
        markup = '\n'.join(parts)
        paras = _split_paragraphs(markup)
        db.execute('INSERT INTO chapters(book_id,spine_order,href,title,content_html,content_text,heading_json,para_count) VALUES(?,?,?,?,?,?,?,?)',
                   (bid, i, f'chapter{i}.html', title, markup, re.sub('<[^>]+>', '', markup), _extract_headings(markup), len(paras)))
        cid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        for seq, p in enumerate(paras):
            db.execute('INSERT INTO paragraphs(chapter_id,book_id,seq,text,html,offset_start,offset_end,char_count) VALUES(?,?,?,?,?,?,?,?)',
                       (cid, bid, seq, p['text'], p['html'], p['offset_start'], p['offset_end'], len(p['text'])))
        total += len(paras)
    db.execute('UPDATE books SET total_paras=?', (total,))
    db.commit()
    return str(output)


def import_document(source, books_dir, original_name=None):
    name = Path((original_name or Path(source).name).replace('\\', '/')).name
    if Path(name).suffix.lower() not in ('.txt', '.epub'):
        raise ValueError('只支持 EPUB 和 TXT')
    root = Path(books_dir)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.import-', dir=root) as stage:
        import shutil
        incoming = Path(stage) / name
        shutil.copyfile(source, incoming)
        output = None
        try:
            output = _write_txt(incoming, stage) if incoming.suffix.lower() == '.txt' else _write_epub(str(incoming), stage)
            db = get_db(str(Path(output) / 'book.db'))
            if not db.execute('SELECT 1 FROM chapters LIMIT 1').fetchone():
                raise ValueError('文件没有可阅读章节')
            db.execute('UPDATE books SET source_file=?', (name,))
            db.commit()
        finally:
            # Also close a partially written EPUB database on parse failure.
            for path in Path(stage).glob('*/book.db'):
                close_db(str(path))
        stem = re.sub(r'[^\w\- .\u4e00-\u9fff]', '_', Path(name).stem).strip('. ')[:100] or 'book'
        destination = root / f'{stem}-{uuid.uuid4().hex[:12]}_data'
        os.replace(output, destination)
        return str(destination)
