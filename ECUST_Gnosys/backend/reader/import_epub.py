"""
Parses an EPUB file into a structured object that can be used to serve the book via a web interface.

模块划分：
- models.py          数据模型（Book / TOCEntry / ChapterContent / BookMetadata）
- book_service.py    基础阅读器（标题检测 + 渲染管线）
- 本文件只保留：EPUB 解析、图片处理、TOC 解析、SQLite 入库
"""

import os
import sys
from pathlib import Path
import json
import shutil
from typing import List, Dict
from datetime import datetime
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from bs4 import BeautifulSoup, Comment

from reader.models import TOCEntry, BookMetadata


# --- Utilities ---

def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:
    """【HTML清理】移除 script/style/iframe/nav/form 等无关标签"""

    # Remove dangerous/useless tags
    for tag in soup(['script', 'style', 'iframe', 'video', 'nav', 'form', 'button']):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Remove input tags
    for tag in soup.find_all('input'):
        tag.decompose()

    return soup


# ── 图片统一处理（封面/占位图）──

_placeholder_cache: Dict[str, bool] = {}


def _is_placeholder_image(img_path: str, blank_ratio: float = 0.95, tol: int = 25) -> bool:
    """【占位图检测】判断图片是否为空白占位图（绝大多数像素与主色接近时视为占位）"""
    if img_path in _placeholder_cache:
        return _placeholder_cache[img_path]
    result = False
    try:
        from PIL import Image
        from collections import Counter
        with Image.open(img_path) as im:
            im = im.convert('RGB').resize((50, 50))
            pixels = list(im.getdata())
            top_color = Counter(pixels).most_common(1)[0][0]
            near = sum(1 for p in pixels if all(abs(p[i] - top_color[i]) <= tol for i in range(3)))
            result = (near / len(pixels)) >= blank_ratio
    except Exception:
        result = False
    _placeholder_cache[img_path] = result
    return result


def _filter_placeholder_images(html: str, images_dir: str) -> str:
    """【图片统一处理】统一 <img>/<image> 标签：
    原文件有封面图片则正常展示（<image> 修正为 <img>），
    图片缺失或为空白占位图则移除不展示。"""
    soup = BeautifulSoup(html, 'html.parser')
    tags = soup.find_all(['img', 'image'])
    if not tags:
        return html
    changed = False
    for tag in tags:
        src = tag.get('src') or tag.get('xlink:href') or tag.get('href')
        if not src:
            tag.decompose()
            changed = True
            continue
        fname = os.path.basename(unquote(src))
        local_path = os.path.join(images_dir, fname)
        if not os.path.exists(local_path) or _is_placeholder_image(local_path):
            # 图片文件缺失或为空白占位图 → 不展示
            tag.decompose()
            changed = True
            continue
        # 正常图片：统一为 <img src="images/文件名">
        rel = 'images/' + fname
        if tag.name != 'img':
            tag.name = 'img'
            tag['src'] = rel
            for attr in ('xlink:href', 'href'):
                if attr in tag.attrs:
                    del tag.attrs[attr]
            changed = True
        elif tag.get('src') != rel:
            tag['src'] = rel
            changed = True

    # 封面页常被包在 <svg width="100%" height="100%"> 里，会产生巨大空白 → 解包 <svg>
    for svg in soup.find_all('svg'):
        if svg.find(['img', 'image']):
            svg.replace_with(*svg.contents)
            changed = True

    return str(soup) if changed else html


def parse_toc_recursive(toc_list, depth=0) -> List[TOCEntry]:
    """【NCX目录解析】递归解析 EPUB 原始 NCX 目录为 TOCEntry 树"""
    def _mk(href: str, title: str) -> TOCEntry:
        return TOCEntry(
            title=title,
            href=href,
            file_href=href.split('#')[0],
            anchor=href.split('#')[1] if '#' in href else "",
        )

    result = []
    for item in toc_list:
        # ebooklib TOC items are either `Link` objects or tuples (Section, [Children])
        if isinstance(item, tuple):
            section, children = item
            entry = _mk(section.href, section.title)
            entry.children = parse_toc_recursive(children, depth + 1)
            result.append(entry)
        elif isinstance(item, (epub.Link, epub.Section)):
            result.append(_mk(item.href, item.title))

    return result


def get_fallback_toc(book_obj) -> List[TOCEntry]:
    """【兜底目录】当 NCX 为空时，从 Spine 构建平坦目录"""
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            # Try to guess a title from the content or ID
            title = item.get_name().replace('.html', '').replace('.xhtml', '').replace('_', ' ').title()
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def extract_metadata_robust(book_obj) -> BookMetadata:
    """【元数据提取】健壮地提取 EPUB 元数据（标题、作者、语言等）"""
    def get_list(key):
        data = book_obj.get_metadata('DC', key)
        return [x[0] for x in data] if data else []

    def get_one(key):
        data = book_obj.get_metadata('DC', key)
        return data[0][0] if data else None

    return BookMetadata(
        title=get_one('title') or "Untitled",
        language=get_one('language') or "en",
        authors=get_list('creator'),
        description=get_one('description'),
        publisher=get_one('publisher'),
        date=get_one('date'),
        identifiers=get_list('identifier'),
        subjects=get_list('subject')
    )


# --- 标题检测与目录构建已迁移至 book_service.py ---

# --- SQLite processing ---

def _extract_headings(html: str) -> str:
    """从章 HTML 抽出标题层级，返回 JSON 字符串 `[{"level": 1, "text": "…"}]`。

    为什么必须记：epub 源里的 `<h1>`-`<h6>` 就是结构真相（章→节→子节），
    但段落是按 `<br>`/块级标签切平的，**不记下来结构就丢了** ——
    语义层要「上承章节结构」、术语解释要用子节作为适用范围，全都要靠它。
    历史数据里这个字段有的是那位另一版代码导入的（带 level+text），格式保持一致。

    txt 源没有标题标签，这里会返回空数组 —— 那种情况改由 book_outline 的
    文本行模式推断兜底（见 backend/agent/outline.py）。
    """
    import re as _re
    out = []
    for m in _re.finditer(r"<h([1-6])\b[^>]*>(.*?)</h\1>", html or "", _re.S | _re.I):
        text = " ".join(_re.sub(r"<[^>]+>", "", m.group(2)).split())
        if text:
            out.append({"level": int(m.group(1)), "text": text})
    return json.dumps(out, ensure_ascii=False)


def _split_paragraphs(html: str) -> list:
    """【段落拆分】将章节 HTML 拆分为段落列表（策略: <p> → <br> → \u3000\u3000）"""
    soup = BeautifulSoup(html, 'html.parser')
    p_tags = soup.find_all('p')
    
    if p_tags:
        # Strategy A: <p> tags, but split <br>-heavy <p>s into sub-paras
        result = []
        for tag in p_tags:
            tag_html = str(tag)
            tag_text = tag.get_text().strip()
            if not tag_text or len(tag_text) < 2:
                continue
            
            # If this <p> has many <br> tags, split into sub-paragraphs
            br_count = tag_html.lower().count('<br')
            if br_count >= 5:
                sub_segments = tag_html.replace('<br/>', '<br>').split('<br>')
                running = 0
                for sub in sub_segments:
                    sub_soup = BeautifulSoup(sub, 'html.parser')
                    sub_text = sub_soup.get_text().strip()
                    sub_html = str(sub_soup)
                    if sub_text and len(sub_text) >= 2:
                        result.append({
                            'html': sub_html,
                            'text': sub_text,
                            'offset_start': running,
                            'offset_end': running + len(sub_html),
                        })
                    running += len(sub) + 4  # +4 for <br>
            else:
                offset_start = html.find(tag_html)
                offset_end = offset_start + len(tag_html) if offset_start != -1 else 0
                result.append({
                    'html': tag_html,
                    'text': tag_text,
                    'offset_start': offset_start,
                    'offset_end': offset_end,
                })
        if result:
            return result

    # Strategy B+C: No <p> tags found — split by <br>, then \u3000\u3000 for long segments
    normalized = html.replace('<br/>', '<br>')
    segments = normalized.split('<br>')
    result = []
    running_offset = 0
    
    for seg in segments:
        seg_soup = BeautifulSoup(seg, 'html.parser')
        seg_text = seg_soup.get_text().strip()
        seg_html = str(seg_soup)
        
        if not seg_text or len(seg_text) < 2:
            running_offset += len(seg) + 4
            continue
        
        if len(seg_text) > 300 and '\u3000\u3000' in seg_text:
            sub_segs = seg_text.split('\u3000\u3000')
            for sub in sub_segs:
                sub = sub.strip()
                if sub and len(sub) >= 2:
                    result.append({
                        'html': f'<p>{sub}</p>',
                        'text': sub,
                        'offset_start': running_offset,
                        'offset_end': running_offset + len(sub),
                    })
        else:
            result.append({
                'html': seg_html,
                'text': seg_text,
                'offset_start': running_offset,
                'offset_end': running_offset + len(seg_html),
            })
        running_offset += len(seg) + 4
    
    return result


def process_epub_to_sqlite(epub_path: str, books_dir: str = None) -> str:
    """【EPUB处理入库】将 EPUB 解析后写入 SQLite 数据库，返回输出目录路径
    books_dir 缺省为仓库根 books/（不依赖当前工作目录）"""
    if books_dir is None:
        books_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "books")
    from db.schema import get_db
    
    # 1. Load & extract
    print(f"Loading {epub_path}...")
    book_obj = epub.read_epub(epub_path)
    metadata = extract_metadata_robust(book_obj)
    
    # 2. Prepare directories
    base_name = os.path.splitext(os.path.basename(epub_path))[0]
    out_dir = os.path.join(books_dir, base_name + "_data")
    images_dir = os.path.join(out_dir, 'images')
    
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(images_dir, exist_ok=True)
    
    # 3. Extract images（src 重写统一由 _filter_placeholder_images 处理）
    print("Extracting images...")
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            safe_fname = "".join(c for c in os.path.basename(item.get_name()) if c.isalnum() or c in '._-').strip()
            with open(os.path.join(images_dir, safe_fname), 'wb') as f:
                f.write(item.get_content())
    
    # 4. Parse TOC
    print("Parsing Table of Contents...")
    toc_structure = parse_toc_recursive(book_obj.toc)
    if not toc_structure:
        print("Warning: Empty TOC, building fallback from Spine...")
        toc_structure = get_fallback_toc(book_obj)

    # 5. Open DB
    db_path = os.path.join(out_dir, 'book.db')
    db = get_db(db_path)
    
    # 6. Insert book row
    now = datetime.now().isoformat()
    db.execute(
        "INSERT INTO books (title, language, authors, source_file, processed_at, total_chaps, toc_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (metadata.title, metadata.language, json.dumps(metadata.authors, ensure_ascii=False),
         os.path.basename(epub_path), now, 0, '[]')
    )
    book_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    # 7. Process chapters & paragraphs
    print("Processing chapters and paragraphs...")
    total_paras = 0
    
    for i, spine_item in enumerate(book_obj.spine):
        item_id, _ = spine_item
        item = book_obj.get_item_with_id(item_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        
        raw_content = item.get_content().decode('utf-8', errors='ignore')
        soup = BeautifulSoup(raw_content, 'html.parser')

        # Clean HTML
        soup = clean_html_content(soup)
        
        # Extract body
        body = soup.find('body')
        if body:
            final_html = "".join(str(x) for x in body.contents)
        else:
            final_html = str(soup)

        # 统一图片处理：修正 <image> 标签、移除缺失/空白占位图
        final_html = _filter_placeholder_images(final_html, images_dir)

        full_text = soup.get_text()
        full_text_clean = ' '.join(full_text.split())
        
        # Chapter title: try TOC lookup first
        ch_title = f"Section {i+1}"
        for t in toc_structure:
            if t.file_href == item.get_name():
                ch_title = t.title
                break
        
        # heading_json：从正文里的 <h1>-<h6> 抽出层级（[{level, text}]）。
        # 曾经这里恒传 '[]' 并在注释里自称「预留字段、服务端不读取」—— 那是错的：
        # 语义层与术语层都要用这份结构（上承章节结构），epub 源的标题就在这里。
        db.execute(
            "INSERT INTO chapters (book_id, spine_order, href, title, content_html, content_text, heading_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (book_id, i, item.get_name(), ch_title, final_html, full_text_clean,
             _extract_headings(final_html))
        )
        chapter_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # Split and insert paragraphs
        paragraphs = _split_paragraphs(final_html)
        for seq, para in enumerate(paragraphs):
            db.execute(
                "INSERT INTO paragraphs (chapter_id, book_id, seq, text, html, offset_start, offset_end, char_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chapter_id, book_id, seq, para['text'], para['html'],
                 para['offset_start'], para['offset_end'], len(para['text']))
            )
        
        db.execute("UPDATE chapters SET para_count = ? WHERE id = ?", (len(paragraphs), chapter_id))
        total_paras += len(paragraphs)
    
    # 8. Update book totals
    db.execute(
        "UPDATE books SET total_chaps = (SELECT COUNT(*) FROM chapters WHERE book_id = ?), total_paras = ? WHERE id = ?",
        (book_id, total_paras, book_id)
    )
    db.commit()
    
    print(f"Saved to {db_path}")
    print(f"  Chapters: {db.execute('SELECT COUNT(*) FROM chapters WHERE book_id=?',(book_id,)).fetchone()[0]}")
    print(f"  Paragraphs: {total_paras}")

    # 9.【章单位对齐】把段落归属对齐到“阅读器真正展示的章”（详见 chapter_units.py）。
    # 转换器给的一个 chunk 里可能没有顶级标题（如序言被切成两段），入库时按 chunk 分章
    # 会让该 chunk 的正文落到下一章；这里按统一 HTML 的章切片重新归属，防止复发。
    # 失败不影响入库结果（只影响“按章取正文”的准确性）。
    try:
        from reader.chapter_units import align_paragraphs, format_report
        rep = align_paragraphs(os.path.basename(out_dir), apply=True)
        print(format_report(rep))
    except Exception as e:
        print(f"  [warn] 章单位对齐跳过：{e}")

    return out_dir

# --- CLI ---

if __name__ == "__main__":

    import sys
    if len(sys.argv) < 2:
        print("Usage: python backend/reader/import_epub.py <file.epub>")
        sys.exit(1)

    epub_file = sys.argv[1]
    assert os.path.exists(epub_file), "File not found."
    
    out_dir = process_epub_to_sqlite(epub_file)
    print(f"\nDone → {out_dir}")
