"""
基础阅读器服务（book_service）—— 标题检测 + 渲染管线 合并模块。

第一部分  标题检测与目录构建（原 headings.py）：正则托底区分标题、层级判定、
          评分、文本检测、锚点注入、目录树构建
第二部分  渲染管线（原 book_service.py）：统一 HTML 构建、内容切片、
          分页/分章、锚点映射、Book 加载、缓存

server.py 只保留路由；本模块是阅读器全部基础逻辑的唯一承载处。
"""

import os
import re
import json
from typing import Optional, List, Dict
from urllib.parse import unquote

from bs4 import BeautifulSoup

from models import Book, TOCEntry, ChapterContent, BookMetadata
from reader3 import _filter_placeholder_images
from schema import get_db


# ── 共享正则片段（检测/层级/评分共用，避免多处不一致）──
_CN_NUM = r'[一二三四五六七八九十百千〇零\d]+'            # 中文数字+阿拉伯（含百千零）
_CN_NUM_PURE = r'[一二三四五六七八九十百千〇零]+'          # 纯中文数字
_ROMAN = r'[IVXLCDM]{1,4}'                                # 罗马数字
_UNI_ROMAN = r'[\u2160-\u2169]+'                          # Unicode 罗马数字（Ⅰ．等）
_CN_CHAP_TAIL = r'[章节篇回部集卷节讲课]'                  # 章级称谓（含"节/讲/课"）
_CN_PREFIX_ORDER = r'(?:[序前引导绪]言|(?:总|代|自|再版|原|编者)\s*序)'  # 序言类

# 标题评分阈值（统一命名，避免魔法数字分散）
_SCORE_MIN_DETECT = 30          # 文本检测加入（Method A/C/D 等）
_SCORE_MIN_START = 20           # Method B：章首标题
_SCORE_MIN_CLEAN = 20           # 目录清洗保留
_SCORE_MIN_ATTACH = 25          # 子标题挂载
_SCORE_HIGH_CONFIDENCE = 60     # 高置信度（无 TOC 时的兜底树）

_HEADING_PATTERNS = [
    # Chinese chapter/section markers
    re.compile(rf'^第{_CN_NUM}{_CN_CHAP_TAIL}[：:　\s]?'),
    re.compile(rf'^{_CN_PREFIX_ORDER}[：:　]?'),
    re.compile(r'^[跋后]记[：:　]?'),
    re.compile(r'^附录[：:　]?'),
    re.compile(r'^译者序[：:　]?'),
    re.compile(r'^出版说明[：:　]?'),
    re.compile(r'^内容提要[：:　]?'),
    # Parenthetical Chinese markers: （一）, 一、, 1. 等
    re.compile(rf'^[（(]{_CN_NUM}[）)]'),
    re.compile(rf'^{_CN_NUM}[、．.]'),
    # Parenthetical letters: (a), (b), (c), （a）（b）（c）, a）, a)
    re.compile(r'^[（(][a-zA-Z][）)]'),
    re.compile(r'^[a-zA-Z][）)]\s*\S'),
    # English chapter/section markers
    re.compile(r'^(Chapter|Part|Section|§)\s*[\dIVXLCDM]+[\.\s]?', re.IGNORECASE),
    re.compile(r'^(Preface|Introduction|Appendix|Foreword|Afterword)[\s:]', re.IGNORECASE),
    # Roman numeral headings: I., II., III., IV., IX. etc.
    # 要求后接大写字母/数字/汉字，排除 "I. e." "A. …" 这类正文缩写误报
    re.compile(rf'^{_ROMAN}\.\s+(?=[A-Z0-9\u4e00-\u9fff])'),
    # Letter headings: A., B., C., a., b., c.
    re.compile(r'^[A-Za-z]\.\s+\S'),
    # Unicode Roman numerals: Ⅰ．, Ⅱ．, Ⅲ．, Ⅳ．etc.
    re.compile(rf'^{_UNI_ROMAN}[．\.]'),
    # Square-bracket headings: [一、...], [1．...], [三、哲学的认识], [Ⅰ．...] etc.
    re.compile(rf'^\[(?:{_CN_NUM}|{_UNI_ROMAN})[、．\.]\s*\S'),
    # Book metadata in square brackets (精神现象学 风格) —— 注意：必须是最后一个模式
    re.compile(r'^【.*?】'),
]


def _match_level(text: str) -> int:
    """【层级匹配】按标题文本模式返回层级（1=章,2=节,3=子节,4=罗马级,5=数字级,6=数字括号枚举；0=非标题模式）"""
    # Strip enclosing brackets before pattern matching
    detect = re.sub(r'^\[([^\]]+)\]$', r'\1', text.strip())

    if re.match(rf'^第{_CN_NUM}{_CN_CHAP_TAIL}', detect):
        return 1
    if re.match(r'(^(Section|Chapter|Part)\b|^§)', detect, re.IGNORECASE):
        return 1
    if re.match(rf'^{_CN_PREFIX_ORDER}', detect):
        return 1
    # 数字括号 (1)(2) 常见为正文枚举 → 最低级（先于汉字括号判定，修复原死代码）
    if re.match(r'^[（(]\d+[）)]', detect):
        return 6
    if re.match(rf'^[（(]{_CN_NUM_PURE}[）)]', detect):
        return 2
    if re.match(rf'^{_CN_NUM_PURE}[、．.]', detect):
        return 2
    if re.match(r'^[A-Z]\.\s', detect):
        return 2
    if re.match(r'^[（(][a-zA-Z][）)]', detect):
        return 3
    if re.match(r'^[a-zA-Z][）)]\s*\S', detect):
        return 3
    if re.match(r'^[a-z]\.\s', detect):
        return 3
    if re.match(r'^【', detect):
        return 4
    if re.match(rf'^{_ROMAN}\.\s', detect):
        return 4
    if re.match(rf'^{_UNI_ROMAN}[．\.]', detect):
        return 4
    if re.match(r'^\d+[．\.、](?!\d)', detect):
        return 5
    return 0


def _determine_level(text: str) -> int:
    """【层级判定】根据标题文本模式判断层级（1=章,2=节,5=数字子节等）；无模式时兜底 2"""
    return _match_level(text) or 2


def _score_heading_candidate(text: str) -> int:
    """【标题评分】评估一段文本是否为标题（0=非标题，越高越像）"""
    if len(text) < 2 or len(text) > 80:
        return 0

    # Reject outright: Chinese headings never contain sentence terminators
    if '。' in text or text.rstrip()[-1] in '！？!?':
        return 0

    score = 0

    # Check against known heading patterns
    for i, pat in enumerate(_HEADING_PATTERNS):
        if pat.match(text):
            # Metadata patterns (【...】) get low fixed score: 元数据标记不是标题候选，
            # 直接返回（不叠加短文本/无句号加分，避免版权页元数据污染目录）
            if i == len(_HEADING_PATTERNS) - 1:  # last pattern = metadata
                return 10
            score = max(score, 80)

    # Bonus for being short（原重复加分合并）
    if len(text) <= 30:
        score += 15
    elif len(text) <= 50:
        score += 5

    # Bonus: does NOT end with period
    if not text.endswith('.') :
        score += 10

    # Penalty: contains obvious body-text markers
    if '的' in text and len(text) > 30:
        score -= 5

    # Reject table-of-contents / index entries (…digits, like page references)
    if re.search(r'…+\s*\d', text):
        return 0

    # Reject glued headings: chapter-title + sub-heading stuck together
    # e.g., "第二章：调查区域江村经济1．调查区域的界定" / "第二章：调查区域一、自然条件"
    # The chapter-pattern part and sub-heading part are both detected separately
    if re.match(rf'^第{_CN_NUM}{_CN_CHAP_TAIL}', text):
        tail = text[len(text) // 2:]
        if re.search(r'\d+[．\.、]', tail) or re.search(r'[一二三四五六七八九十百千]+[、．.]', tail):
            return 0

    # Downgrade digit-based parentheticals like (1), (2) vs legitimate （一）, （二）
    # These are often body-text enumerations, not real headings
    if re.match(r'^[(（]\d+[)）]', text) and not re.match(rf'^[（(]{_CN_NUM_PURE}[）)]', text):
        score = min(score, 25)

    # Downgrade abbreviation-style enumerations: "I. e.", "A. B. C." etc.
    if re.match(r'^[A-Za-z]\.\s+[A-Za-z]\.', text):
        score = min(score, 25)

    return score


def _detect_headings_from_text(soup, chapter_href: str, book_title: str = '') -> list:
    """【文本标题检测】从纯文本 EPUB 中检测标题（方法A/B/C/D四轮）"""
    headings = []
    hkey = re.sub(r'[^a-zA-Z0-9]', '', chapter_href)[-8:]

    def _add(level, title, anchor, score):
        headings.append({
            'level': level,
            'title': title,
            'anchor': anchor,
            'score': score,
            'chapter_order': 0,
            'chapter_href': chapter_href,
        })

    # Method A: Split by <br> (typical for TXT-converted content)
    html_str = str(soup)
    br_segments = []
    for seg in html_str.replace('<br/>', '<br>').split('<br>'):
        seg_soup = BeautifulSoup(seg, 'html.parser')
        text = seg_soup.get_text(strip=True)
        if text:
            br_segments.append(text)

    seen_texts = set()
    for idx, text in enumerate(br_segments):
        score = _score_heading_candidate(text)
        if score >= _SCORE_MIN_DETECT and text not in seen_texts:
            seen_texts.add(text)
            # Determine level based on patterns
            level = _determine_level(text)
            _add(level, text, f"txt-hdr-{hkey}-{idx}", score)

    # Method B: For each chapter, also try to extract a clean heading from the start
    full_text = soup.get_text()
    # Take first non-empty line / segment
    for line in full_text.replace('<br>', '\n').replace('<br/>', '\n').split('\n'):
        line = line.strip()
        if line and len(line) >= 2:
            # Try _clean_title on it (truncates at \u3000\u3000 etc.)
            cleaned = _clean_title(line, max_len=60)
            if cleaned and len(cleaned) >= 2 and len(cleaned) <= 60 and cleaned not in seen_texts:
                # Only add if it actually got truncated (meaning it had body text)
                if cleaned != line or len(line) <= 30:
                    score = _score_heading_candidate(cleaned)
                    if score >= _SCORE_MIN_START:
                        seen_texts.add(cleaned)
                        _add(1, cleaned, f"txt-hdr-start-{hkey}", score)
            break

    # Method C: Scan full continuous text for numbered sub-heading patterns
    # Catches patterns like "1．调查区域的界定" embedded in body text (江村经济 style)
    full_text = soup.get_text()
    # Normalize separators to find sentence boundaries
    normalized = full_text
    for sep in ['\u3000\u3000', '  ', '\n', '；']:
        normalized = normalized.replace(sep, '\n')
    lines = [ln.strip() for ln in normalized.split('\n') if ln.strip()]

    for idx, line in enumerate(lines):
        if len(line) < 3 or len(line) > 60:
            continue
        if line in seen_texts:
            continue
        # 用统一层级匹配判断子标题（内部剥外层方括号，覆盖 [一、xxx] [1．xxx] 与
        # 英文 Chapter/Section/Part 等无 <br> 的 EPUB 格式）
        sub_lvl = _match_level(line)
        if sub_lvl > 0:
            # 连续文本中的"第一章"等中文章级标题由 Method A/B 与原始 TOC 覆盖，
            # 这里仅收集英文 Chapter/Section 等章级（无 <br> 英文书的结构信号）
            if sub_lvl == 1 and not re.match(r'^(Chapter|Part|Section|§)', line.strip(), re.IGNORECASE):
                continue
            # Must end without body-terminating punctuation (period only, not ；)
            if line.rstrip()[-1] in '。！？!?':
                continue
            score = _score_heading_candidate(line)
            if score >= _SCORE_MIN_DETECT:
                seen_texts.add(line)
                _add(sub_lvl, line, f"txt-hdr-full-{hkey}-{idx}", score)

    # Method D: Scan segments for inline sub-heading patterns
    # (e.g. "江村经济1．调查区域的界定" embedded at end of body text)
    # This handles TXT-converted EPUBs where sub-headings are inline
    # rather than on separate lines. The book title consistently appears
    # before actual sub-headings in this format.
    # Use a reproducible anchor so inject_heading_ids can match them.
    if book_title:
        escaped_title = re.escape(book_title)
        _INLINE_SUB_PAT = re.compile(
            rf'{escaped_title}(\d+)\s*[．\.]\s*([^。；、！？\n\u3000]{{1,40}})'
        )
        for idx, line in enumerate(lines):
            for m in _INLINE_SUB_PAT.finditer(line):
                num = m.group(1)
                title_part = m.group(2).strip()
                if re.match(r'^\d', title_part) or re.search(r'[\d．\.]+\s*(亩|岁|％|元|蒲式耳|英里|小时)', title_part):
                    continue
                full_title = '%s．%s' % (num, title_part)
                if len(full_title) < 3 or len(full_title) > 50:
                    continue
                if full_title in seen_texts:
                    continue
                score = _score_heading_candidate(full_title)
                if score >= _SCORE_MIN_DETECT:
                    seen_texts.add(full_title)
                    # Reproducible anchor from title text (not position-based)
                    _add(5, full_title, 'hdr-inline-' + re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]', '', full_title)[:30], score)

    # Sort by original position (not score) to preserve document order
    for i, h in enumerate(headings):
        h['_orig_idx'] = i
    headings.sort(key=lambda h: h['_orig_idx'])

    return headings


def _clean_title(title: str, max_len: int = 100) -> str:
    """【标题清洗】截断正文后缀、去除括号噪音、限制长度"""
    title = title.strip()
    if not title:
        return title

    # Strip matching square brackets (精神现象学: [一、...] → 一、...)
    if title.startswith('[') and title.endswith(']'):
        inner = title[1:-1].strip()
        if inner and len(inner) >= 2:
            title = inner

    # Split trailing "导言"/"引言" from chapter titles when preceded by space
    # e.g. "第五章　理性的确定性与真理性　导言" → split into two parts
    # Only splits when "导言" is in the latter half of the title
    for sep_suffix in [('\u3000导言', 3), ('\u3000引言', 3), (' 导言', 3), (' 引言', 3)]:
        sep, _ = sep_suffix
        idx = title.rfind(sep)
        if idx > len(title) // 2 and idx > 3:
            before = title[:idx].strip()
            if len(before) >= 4 and _score_heading_candidate(before) >= _SCORE_MIN_DETECT:
                title = before
                break

    # Truncate at double Chinese space (wide space) — common in Chinese EPUBs
    idx = title.find('\u3000\u3000')
    if idx > 0:
        title = title[:idx]
    # Truncate at double regular space
    idx = title.find('  ')
    if idx is not None and 10 < idx < len(title) - 10:
        title = title[:idx]
    # Truncate at Chinese period + space (sentence boundary with body text)
    for punct in '。！？':
        idx = title.find(punct)
        if 5 < idx < max_len and idx < len(title) - 5:
            title = title[:idx + 1]
            break
    # Enforce max length at a sentence boundary
    if len(title) > max_len:
        for punct in '。！？.!?':
            idx = title.rfind(punct, 0, max_len)
            if idx > 10:
                title = title[:idx + 1]
                break
        else:
            title = title[:max_len].rstrip() + '…'
    return title.strip()


def _clean_toc_entries(entries: list, depth: int = 0, filter_depth0: bool = True) -> list:
    """【目录清洗】递归清洗 TOC 条目，过滤非标题条目"""
    cleaned = []
    for entry in entries:
        new_title = _clean_title(entry.title)
        # Skip entries that don't look like headings after cleaning:
        # - Very long strings that aren't proper titles
        # - Strings that are just filenames
        if depth == 0 and filter_depth0:
            if _score_heading_candidate(new_title) < _SCORE_MIN_CLEAN:
                # Check if it has children to keep
                if entry.children:
                    new_children = _clean_toc_entries(entry.children, depth + 1, filter_depth0)
                    cleaned.extend(new_children)
                continue
        new_children = _clean_toc_entries(entry.children, depth + 1, filter_depth0) if entry.children else []
        cleaned.append(TOCEntry(
            title=new_title,
            href=entry.href,
            file_href=entry.file_href,
            anchor=entry.anchor,
            children=new_children,
        ))
    return cleaned


def inject_heading_ids(chapter: ChapterContent, book_title: str = '') -> str:
    """【锚点注入】为章节内的标题标签注入 id 属性（方法1:HTML标签 方法2:文本检测 方法3:内联）"""
    soup = BeautifulSoup(chapter.content, 'html.parser')
    modified = False

    # Method 1: Inject IDs for <h1>-<h6> tags
    heading_tags = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    for idx, tag in enumerate(heading_tags):
        if not tag.get('id'):
            tag['id'] = f"hdr-{chapter.order}-{idx}"
            modified = True

    # Method 2: If no heading tags, inject anchors for text-detected headings
    if not heading_tags:
        html_str = str(soup)
        # 与 _detect_headings_from_text Method A 完全一致的规范化切分，保证锚点索引对准
        html_str = html_str.replace('<br/>', '<br>')
        parts = html_str.split('<br>')
        new_parts = []
        for p_idx, part in enumerate(parts):
            part_text = BeautifulSoup(part, 'html.parser').get_text(strip=True)
            if part_text and _score_heading_candidate(part_text) >= _SCORE_MIN_DETECT:
                # Deterministic anchor from chapter href
                hkey = re.sub(r'[^a-zA-Z0-9]', '', chapter.href)[-8:]
                anchor_id = f"txt-hdr-{hkey}-{p_idx}"
                # Avoid double-injection
                if anchor_id not in part:
                    part = f'<span id="{anchor_id}"></span>' + part
                    modified = True
            new_parts.append(part)
        if modified:
            html_str = '<br>'.join(new_parts)

        # Method 3: Inject anchors for inline sub-headings
        # (e.g., "江村经济<br><br>1．调查区域的界定" — detected by Method D)
        # Uses the book title to locate patterns and generates matching
        # reproducible anchor IDs. Accounts for HTML tags between elements.
        if book_title:
            escaped = re.escape(book_title)
            _inj_pat = re.compile(
                rf'({escaped})(?:<[^>]+>|\s)*?(\d+)\s*[．\.]\s*([^。；、！？\n\u3000]{{1,40}})'
            )
            def _inject_inline_anchor(m):
                """【内联锚点注入】为内联子标题注入 <span> 锚点"""
                num = m.group(2)
                title_part = m.group(3).strip()
                if re.match(r'^\d', title_part):
                    return m.group(0)
                full = '%s．%s' % (num, title_part)
                if len(full) < 3:
                    return m.group(0)
                anchor_id = 'hdr-inline-' + re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]', '', full)[:30]
                return '<span id="%s"></span>%s' % (anchor_id, m.group(0))
            new_html = _inj_pat.sub(_inject_inline_anchor, html_str)
            if new_html != html_str:
                return new_html

        if modified:
            return html_str

    return str(soup) if modified else chapter.content


def _attach_sub_headings(toc_entries: List[TOCEntry], book: Book) -> List[TOCEntry]:
    """【子标题挂载】检测章节内的子标题并挂载到父级 TOC 条目下"""
    def _titles_are_similar(a: str, b: str) -> bool:
        """【标题相似判断】检查两个标题是否近似重复"""
        if a == b:
            return True
        # One is a prefix/suffix of the other (after cleaning)
        a_clean = re.sub(r'[\[\]（）()\s]', '', a)
        b_clean = re.sub(r'[\[\]（）()\s]', '', b)
        if len(a_clean) >= 3 and len(b_clean) >= 3:
            if a_clean in b_clean or b_clean in a_clean:
                return True
        return False

    def _clean_sub_title(title: str, parent_title: str) -> str:
        """【子标题清洗】剥离父标题前缀和括号噪音"""
        t = _clean_title(title)
        if not t or t == parent_title:
            return ''
        # Strip parent title prefix
        if parent_title and t.startswith(parent_title):
            remainder = t[len(parent_title):].strip()
            if remainder and _score_heading_candidate(remainder) >= _SCORE_MIN_DETECT:
                t = remainder
        # Find sub-heading pattern in remaining text
        m = re.search(r'([一二三四五六七八九十]+[．\.、][^。\n]{2,40})$', t)
        if m:
            cand = m.group(1).strip()
            if _score_heading_candidate(cand) >= _SCORE_MIN_DETECT:
                t = cand
        # Also try digit pattern
        m2 = re.search(r'(\d+[．\.、][^。\n]{2,40})$', t)
        if m2 and not m:
            cand = m2.group(1).strip()
            if _score_heading_candidate(cand) >= _SCORE_MIN_DETECT:
                t = cand
        return t

    # Build map: file_href → TOC entries
    toc_by_file: Dict[str, List[TOCEntry]] = {}
    for entry in toc_entries:
        toc_by_file.setdefault(entry.file_href, []).append(entry)

    def _recompute_level(title: str) -> int:
        """【层级重算】从清洗后的标题重新计算层级"""
        return _determine_level(title)

    # 确定每个 spine 文件的可挂载父条目（按 spine 顺序遍历）：
    # 不在 toc_by_file 中的文件（其 TOC 条目被 _clean_toc_entries 过滤，
    # 如 TXT 转换书的正文续篇，标题是正文首句而非真实标题）不能直接丢弃——
    # 挂到前一个相邻章条目下，避免其中检测出的子标题丢失（精神现象学序言三、四部分）。
    file_to_parent: Dict[str, TOCEntry] = {}
    last_parent: Optional[TOCEntry] = None
    for ch in book.spine:
        if ch.href in toc_by_file:
            last_parent = toc_by_file[ch.href][-1]
            file_to_parent[ch.href] = last_parent
        elif last_parent is not None:
            file_to_parent[ch.href] = last_parent

    # 按父章收集清洗后的标题（spine 顺序 = 文档顺序；同父章的多文件子标题合并后统一建树）
    heads_by_parent: Dict[int, list] = {}
    seq = 0  # 全局序号：跨文件合成时保证文档顺序（_orig_idx 每文件内从 0 开始）
    for ch in book.spine:
        parent = file_to_parent.get(ch.href)
        if parent is None:
            continue

        soup = BeautifulSoup(ch.content, 'html.parser')
        detected = _detect_headings_from_text(soup, ch.href, book.metadata.title)
        good = [h for h in detected if h['score'] >= _SCORE_MIN_ATTACH]

        if not good:
            continue

        # Clean and deduplicate titles
        seen_clean = set()
        cleaned_heads = []
        for h in good:
            t = _clean_sub_title(h['title'], parent.title)
            if not t or t in seen_clean:
                continue
            # Check similarity with already-added titles
            is_dup = False
            for existing in seen_clean:
                if _titles_are_similar(t, existing):
                    is_dup = True
                    break
            if is_dup:
                continue
            seen_clean.add(t)
            # Recompute level from cleaned title (fixes glued heading bug)
            new_level = _recompute_level(t)
            seq += 1
            cleaned_heads.append({
                'level': new_level,
                'title': t,
                'anchor': h['anchor'],
                '_orig_idx': seq,
                'chapter_order': 0,
                'chapter_href': ch.href,
            })

        if not cleaned_heads:
            continue

        heads_by_parent.setdefault(id(parent), []).extend(cleaned_heads)

    # 统一构建子标题树并挂载（顺序：按 spine 内检测到的文档顺序；同一父章多文件合并，互不覆盖）
    for parent_key, cleaned_heads in heads_by_parent.items():
        # Find parent entry by identity
        parent = None
        for entries in toc_by_file.values():
            for e in entries:
                if id(e) == parent_key:
                    parent = e
                    break
            if parent is not None:
                break
        if parent is None:
            continue
        # Sort by original position (not score) for correct tree building
        cleaned_heads.sort(key=lambda h: h.get('_orig_idx', 0))
        # Build hierarchy among sub-headings and attach
        sub_tree = _headings_to_tree(cleaned_heads, use_levels=True)
        parent.children = sub_tree

    return toc_entries


def build_heading_based_toc(book: Book) -> List[TOCEntry]:
    """【标题目录构建】构建完整层级目录——优先 HTML 标签，回退到文本检测+NCX"""
    all_headings: List[dict] = []
    has_any_html_headings = False

    for ch in book.spine:
        soup = BeautifulSoup(ch.content, 'html.parser')
        html_headings = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])

        if html_headings:
            has_any_html_headings = True
            for idx, tag in enumerate(html_headings):
                level = int(tag.name[1])
                text = tag.get_text(strip=True)
                if not text:
                    continue
                hid = tag.get('id') or f"hdr-{ch.order}-{idx}"
                all_headings.append({
                    'level': level,
                    'title': text,
                    'anchor': hid,
                    'chapter_order': ch.order,
                    'chapter_href': ch.href,
                })

    if has_any_html_headings:
        # Clean up titles from real heading tags
        for h in all_headings:
            h['title'] = _clean_title(h['title'])

        # Fix hierarchy for books where all headings share the same HTML level
        # (e.g. all-<h1> books like 富爸爸穷爸爸).  Within each spine file,
        # if subsequent headings have the same level as the first, demote them
        # by one level so they become children rather than siblings.
        by_file: Dict[str, List[dict]] = {}
        for h in all_headings:
            by_file.setdefault(h['chapter_href'], []).append(h)
        for _, headings in by_file.items():
            if len(headings) <= 1:
                continue
            first_level = headings[0]['level']
            for i, h in enumerate(headings):
                if i > 0 and h['level'] <= first_level:
                    h['level'] = first_level + 1

        return _headings_to_tree(all_headings, use_levels=True)

    # No <h1>-<h6> tags found.
    # Strategy: use cleaned original TOC as base, supplemented by
    # high-confidence text-detected headings.

    cleaned_original = _clean_toc_entries(book.toc)

    # Collect high-confidence text headings (score >= 60, i.e. strong heading matches)
    text_headings: List[dict] = []
    for ch in book.spine:
        soup = BeautifulSoup(ch.content, 'html.parser')
        detected = _detect_headings_from_text(soup, ch.href)
        for h in detected:
            if h['score'] >= _SCORE_HIGH_CONFIDENCE:
                h['chapter_order'] = ch.order
                h['title'] = _clean_title(h['title'])
                text_headings.append(h)

    # If original TOC is decent (>=3 entries), use it with sub-headings attached.
    if len(cleaned_original) >= 3:
        # Check if content has enough structural separation to support sub-heading detection.
        # Some EPUBs use <br> to separate headings from body (精神现象学, 资本论),
        # others use double wide spaces \u3000\u3000 (江村经济).
        sample_spine = book.spine[:min(3, len(book.spine))]
        br_count = sum(
            len(BeautifulSoup(ch.content, 'html.parser').find_all('br'))
            for ch in sample_spine
        )
        ws_count = sum(
            BeautifulSoup(ch.content, 'html.parser').get_text().count('\u3000\u3000')
            for ch in sample_spine
        )
        has_structure = br_count >= 50 or ws_count >= 20
        if has_structure:
            return _attach_sub_headings(cleaned_original, book)
        return cleaned_original

    # Use text-detected headings if we have enough
    if len(text_headings) >= 3:
        return _headings_to_tree(text_headings, use_levels=False)

    # Last resort: return whatever we have (cleaned original or text)
    return cleaned_original if cleaned_original else _headings_to_tree(text_headings, use_levels=False)


def _headings_to_tree(headings: List[dict], use_levels: bool = True) -> List[TOCEntry]:
    """【标题树构建】将扁平的标题 dict 列表转换为嵌套的 TOCEntry 树"""
    if not use_levels:
        return [
            TOCEntry(
                title=h['title'],
                href=f"{h['chapter_href']}#{h['anchor']}",
                file_href=h['chapter_href'],
                anchor=h['anchor'],
                children=[],
            )
            for h in headings
        ]

    root: List[TOCEntry] = []
    stack: List[tuple] = []

    for h in headings:
        entry = TOCEntry(
            title=h['title'],
            href=f"{h['chapter_href']}#{h['anchor']}",
            file_href=h['chapter_href'],
            anchor=h['anchor'],
            children=[],
        )
        level = h['level']
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].append(entry)
        else:
            root.append(entry)
        stack.append((level, entry.children))

    return root



# ═══════════════════════════════════════════
# 第二部分：渲染管线（原 book_service.py）
# ═══════════════════════════════════════════

# 书库目录固定在仓库根的 books/（与 backend/ 同级）
BOOKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "books")

_heading_toc_cache: Dict[str, List[TOCEntry]] = {}
_book_cache: Dict[str, dict] = {}  # book_id → {Book object, db_path}
_unified_html_cache: Dict[str, str] = {}  # book_id → 拼接全书的 HTML（已注入锚点）
_spine_offsets_cache: Dict[str, List[int]] = {}  # book_id → spine 文件字节偏移（与统一HTML同构建）
_anchor_map_cache: Dict[str, Dict[str, int]] = {}  # book_id → {anchor id: 章节索引}


def clear_book_caches(book_id: str) -> None:
    """【缓存清理】清除某本书的全部缓存（上传新书后调用，不影响其他书）"""
    _heading_toc_cache.pop(book_id, None)
    _book_cache.pop(book_id, None)
    _unified_html_cache.pop(book_id, None)
    _spine_offsets_cache.pop(book_id, None)
    _anchor_map_cache.pop(book_id, None)
    if hasattr(_get_toc_pages, '_cache'):
        _get_toc_pages._cache.pop(book_id + '_pages', None)


# ── 统一 HTML 构建 ──

def _chapter_html(ch: ChapterContent, book_title: str, images_dir: str) -> str:
    """【章节HTML】注入标题锚点并统一处理图片（供统一HTML与偏移量共用，保证一致）"""
    html_with_ids = inject_heading_ids(ch, book_title)
    return _filter_placeholder_images(html_with_ids, images_dir)


def _get_unified_html_cached(book_id: str, book: Book) -> str:
    """【统一HTML缓存】构建并缓存全书统一 HTML，同时顺带算出各 spine 文件字节偏移（一次解析）。"""
    if book_id not in _unified_html_cache:
        images_dir = os.path.join(BOOKS_DIR, book_id, 'images')
        unified = ''
        offsets = []
        pos = 0
        for ch in book.spine:
            offsets.append(pos)
            html = _chapter_html(ch, book.metadata.title, images_dir)
            unified += html
            pos += len(html)
        _unified_html_cache[book_id] = unified
        _spine_offsets_cache[book_id] = offsets
    return _unified_html_cache[book_id]


# ── Spine 文件字节偏移 ──

def _get_spine_offsets(book_id: str, book: Book) -> List[int]:
    """【文件偏移】返回每个 spine 文件在统一 HTML 中的起始字节偏移（构建统一 HTML 时已缓存）。"""
    _get_unified_html_cached(book_id, book)
    return _spine_offsets_cache[book_id]


# ── 标题目录缓存 ──

# 空分区/分卷标题页判定：标题形如"第X部分/篇/卷"且无子标题 → 纯标题页（无正文）
_PART_TITLE_RE = re.compile(r'^第[一二三四五六七八九十百千〇零\d]+[部分篇卷]')


def _get_heading_toc(book_id: str, book: Book) -> List[TOCEntry]:
    """【标题目录缓存】获取带缓存的标题式目录。

    顶级标题中形如"第X部分/篇/卷"且无子标题的分区标题页会被过滤掉
    （如"第一部分：意识"仅一个 <h1> 无正文），避免目录出现空白章；
    其标题仍作为下一章正文的一部分展示。
    """
    if book_id not in _heading_toc_cache:
        toc = build_heading_based_toc(book)
        _heading_toc_cache[book_id] = [
            e for e in toc
            if not (_PART_TITLE_RE.match((e.title or '').strip()) and not e.children)
        ]
    return _heading_toc_cache[book_id]


# ── 分页构建（标题树 → 页面列表）──

def _build_toc_pages(book: Book, heading_toc: List[TOCEntry]) -> List[dict]:
    """【分页构建】将标题树展平为页面列表，每标题一页。

    全书 spine 文件已拼接为统一 HTML，所有标题在同一文档中切片。
    depth==0 的标题标记为 _chapter_start（章边界）。
    """
    raw_pages = []

    def _flatten(entries, depth=0):
        for e in entries:
            spine_order = None
            for ch in book.spine:
                if ch.href == e.file_href:
                    spine_order = ch.order
                    break
            raw_pages.append({
                'title': e.title,
                'anchor': e.anchor,
                'spine_order': spine_order,
                '_chapter_start': (depth == 0),
            })
            if e.children:
                _flatten(e.children, depth + 1)

    _flatten(heading_toc)
    return raw_pages


# ── 分页缓存 ──

def _get_toc_pages(book_id: str, book: Book) -> List[dict]:
    """【分页缓存】获取带缓存的分页列表"""
    cache_key = f"{book_id}_pages"
    if not hasattr(_get_toc_pages, '_cache'):
        _get_toc_pages._cache = {}
    if cache_key not in _get_toc_pages._cache:
        heading_toc = _get_heading_toc(book_id, book)
        _get_toc_pages._cache[cache_key] = _build_toc_pages(book, heading_toc)
    return _get_toc_pages._cache[cache_key]


# ── 章构建（页面 → 章分组）──

def _build_chapters(toc_pages: List[dict]) -> List[dict]:
    """【章构建】将页面按 _chapter_start 分组为章列表。

    每章包含：起始页索引、标题、子节列表、有效锚点（用于切片）。
    若章标题无锚点（NCX 条目），则用章内第一个子节的锚点作为切片起点。
    空分区标题页已在 _get_heading_toc 阶段过滤（见 _PART_TITLE_RE）。
    """
    chapters = []
    current = None
    for pi, p in enumerate(toc_pages):
        if p.get('_chapter_start') or current is None:
            if current:
                chapters.append(current)
            current = {
                'title': p['title'],
                'start_page': pi,
                'anchor': p['anchor'],
            }
    if current:
        chapters.append(current)
    return chapters


# ── 章切片锚点计算 ──

def _chapter_slice_anchors(chapters: List[dict], toc_pages: List[dict],
                           book: Book, book_id: str, chapter_idx: int):
    """返回第 idx 章的切片 (start_anchor, end_anchor)。

    优先用章标题锚点；若锚点无效（不存在于统一 HTML，或出现多次——如 calibre
    页码锚点 calibre_pb_N 全书重复出现，无法唯一定位章节边界），回退到 spine
    文件边界字节偏移做精确切片。第 0 章始终从文档头开始。
    """
    def _anchor_ok(anchor: str) -> bool:
        """锚点是否可唯一定位：须存在于统一 HTML 且仅出现一次"""
        if not anchor or anchor.startswith('__byte__'):
            return True
        unified = _get_unified_html_cached(book_id, book)
        pat = f'id="{anchor}"'
        return pat in unified and unified.count(pat) == 1

    def _spine_byte(page: dict) -> Optional[str]:
        """页面所属 spine 文件的字节偏移锚点；无法确定时返回 None"""
        si = page.get('spine_order')
        if si is None:
            return None
        offsets = _get_spine_offsets(book_id, book)
        if si < len(offsets):
            return '__byte__{}'.format(offsets[si])
        return None

    ch = chapters[chapter_idx]
    start = '' if chapter_idx == 0 else ch.get('anchor', '')

    # 防御：锚点无效（不存在 / 全书重复）→ 视为无效，走字节偏移回退
    if start and not _anchor_ok(start):
        start = ''
    if start == '' and chapter_idx > 0:
        # 用本章第一个标题所在 spine 文件的边界做精确切片
        start = _spine_byte(toc_pages[ch['start_page']]) or ''

    # 终点：下一章锚点，或下一章文件边界
    end = None
    if chapter_idx + 1 < len(chapters):
        next_ch = chapters[chapter_idx + 1]
        end = next_ch.get('anchor', '')
        if end and not _anchor_ok(end):
            end = None
        if not end:
            end = _spine_byte(toc_pages[next_ch['start_page']])

    return start, end


# ── 锚点 → 章节索引 映射（用于正文内超链接跳转）──

def _get_anchor_chapter_map(book_id: str, book: Book) -> Dict[str, int]:
    """【锚点映射】建立 anchor id → 章节索引 的映射，供正文内超链接跳转定位。

    只保留正文内超链接实际引用的锚点，大幅缩小模板内联 JSON（
    无引用的 id 不需要映射，跳转用不到）。
    """
    if book_id in _anchor_map_cache:
        return _anchor_map_cache[book_id]
    toc_pages = _get_toc_pages(book_id, book)
    chapters = _build_chapters(toc_pages)
    unified = _get_unified_html_cached(book_id, book)

    # 第一遍：收集全书所有 id → 首次出现的章节
    id_to_ch: Dict[str, int] = {}
    for ci, _ in enumerate(chapters):
        start_a, end_a = _chapter_slice_anchors(chapters, toc_pages, book, book_id, ci)
        html = _slice_content(unified, start_a, end_a)
        for mid in re.findall(r'id="([^"]+)"', html):
            id_to_ch.setdefault(mid, ci)

    # 第二遍：只保留正文内 <a href="...#anchor"> 引用的锚点
    targets: set = set()
    for _, anch in re.findall(r'''href=["']([^"']*)#([^"']+)["']''', unified):
        try:
            anch = unquote(anch)
        except Exception:
            pass
        targets.add(anch)
    m = {t: id_to_ch[t] for t in targets if t in id_to_ch}
    _anchor_map_cache[book_id] = m
    return m


# ── HTML 标签起始位置查找 ──

def _find_tag_start(html: str, id_pos: int) -> int:
    """【标签定位】从 id 属性位置反查所在 HTML 标签的开头 < 位置"""
    tag_start = html.rfind('<', 0, id_pos)
    if tag_start < 0:
        return id_pos
    # Verify this '<' opens a tag whose '>' is after id_pos
    tag_end = html.find('>', tag_start)
    if tag_end < 0 or tag_end < id_pos:
        return id_pos
    return tag_start


# ── 切片 div 平衡 ──

def _balance_divs(html: str) -> str:
    """【切片平衡】切片起点若落在包裹 div 内部，切片开头会带有多余的 </div>
    （对应切片之前就已打开的包裹 div 的闭合标签）；同时切片末尾可能缺失
    若干 </div>。浏览器解析时多余的 </div> 会把外层容器（#ch-content）提前
    闭合，导致正文内容溢出容器、容器内超链接点击事件失效。

    这里用堆栈扫描：丢弃孤立的 </div>（栈为空时遇到闭合标签），并在末尾
    补齐未闭合的 <div>，使切片 div 结构闭合平衡。
    """
    out = []
    stack = []
    i = 0
    n = len(html)
    for m in re.finditer(r'</?div\b', html):
        out.append(html[i:m.start()])
        ge = html.find('>', m.end())
        if ge < 0:
            ge = n
        full_tag = html[m.start():ge + 1]
        if m.group(0).startswith('</'):
            if stack:
                stack.pop()
                out.append(full_tag)
            # 栈为空 → 多余的 </div>，丢弃
        else:
            stack.append(m.start())
            out.append(full_tag)
        i = ge + 1
    out.append(html[i:])
    result = ''.join(out)
    if stack:
        result += '</div>' * len(stack)
    return result


# ── 内容切片（锚点间 HTML 片段提取）──

def _slice_content(full_html: str, anchor: str, next_anchor: Optional[str]) -> str:
    """【内容切片】从完整 HTML 中切出两个锚点之间的内容片段。

    anchor/next_anchor 可以是标准 HTML id，也可以是 '__byte__N' 格式的字节偏移。
    切片结果会经过 _balance_divs 平衡 div 闭合标签。
    """
    # ── 处理字节偏移 ──
    start_id_pos = -1   # anchor 为普通 id 时记录其 id 属性位置（用于终点查找跳过自身）
    start_pattern = ''
    if anchor and anchor.startswith('__byte__'):
        byte_pos = int(anchor[8:])
        start_tag = byte_pos
    elif not anchor:
        start_tag = 0
    else:
        start_pattern = f'id="{anchor}"'
        start_id_pos = full_html.find(start_pattern)
        if start_id_pos < 0:
            return full_html
        start_tag = _find_tag_start(full_html, start_id_pos)

    if next_anchor and next_anchor.startswith('__byte__'):
        end_tag = int(next_anchor[8:])
        return _balance_divs(full_html[start_tag:end_tag])

    result = full_html[start_tag:]

    if next_anchor:
        end_pattern = f'id="{next_anchor}"'
        # 从起点 id 之后开始查找终点 id：避免 start/end 为同一 id 时
        # （如 calibre 页码锚点全书重复）误命中起点自身导致切片退化为全文
        search_from = (start_id_pos + len(start_pattern)) if start_id_pos >= 0 else (start_tag + 1)
        end_id_pos = full_html.find(end_pattern, search_from)
        if end_id_pos >= 0:
            end_tag = _find_tag_start(full_html, end_id_pos)
            if end_tag > start_tag:
                result = full_html[start_tag:end_tag]

    return _balance_divs(result)


# ── 从 SQLite 重建 Book 对象 ──

def _load_book_from_sqlite(folder_name: str) -> Optional[Book]:
    """【SQLite加载】从 SQLite 数据库重建 Book 对象（含缓存）"""
    # Check cache first
    if folder_name in _book_cache:
        return _book_cache[folder_name]['book']

    db_path = os.path.join(BOOKS_DIR, folder_name, "book.db")
    if not os.path.exists(db_path):
        return None

    db = get_db(db_path)
    row = db.execute("SELECT * FROM books LIMIT 1").fetchone()
    if not row:
        return None

    # Build BookMetadata
    authors = json.loads(row['authors'] or '[]')
    metadata = BookMetadata(
        title=row['title'],
        language=row['language'] or 'zh',
        authors=authors,
    )

    # Build spine (ChapterContent list)
    chapters = db.execute(
        "SELECT * FROM chapters WHERE book_id = ? ORDER BY spine_order", (row['id'],)
    ).fetchall()

    spine = []
    for ch in chapters:
        spine.append(ChapterContent(
            id=str(ch['id']),
            href=ch['href'],
            title=ch['title'],
            content=ch['content_html'],
            text='',  # content_text 服务端未使用，不载入内存（省内存；需要时从 DB 取）
            order=ch['spine_order'],
        ))

    # Build TOC (flat from headings cache — will be rebuilt later)
    # For now create a minimal TOC from chapters
    toc = []
    for ch in chapters:
        toc.append(TOCEntry(
            title=ch['title'],
            href=ch['href'],
            file_href=ch['href'],
            anchor="",
            children=[],
        ))

    book = Book(
        metadata=metadata,
        spine=spine,
        toc=toc,
        source_file=row['source_file'] or '',
        processed_at=row['processed_at'],
    )
    _book_cache[folder_name] = {'book': book, 'db_path': db_path}
    return book


# ── 书籍加载公开接口 ──

def load_book_cached(folder_name: str) -> Optional[Book]:
    """【书籍加载】从 SQLite 数据库加载一本书"""
    return _load_book_from_sqlite(folder_name)
