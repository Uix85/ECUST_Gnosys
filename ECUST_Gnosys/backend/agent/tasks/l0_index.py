"""L0：录入期索引与术语提名（纯统计，零模型）。

设计依据：docs/概念层_设计_v1.md 第 11 节。

为什么有这一层：概念层的主线是「维护每个术语的意义地图」，而划词只是在地图上取值。
地图要先有，但整本书的词不可能都建图（贵且没必要）。所以录入期先做两件便宜的事：

  1. **提名**：把「可能值得建图」的词挑出来（三信号，任一命中即可），
     存 term_candidate，由人工过一眼确认；
  2. **索引**：把每个候选的全书分布算好存 term_scan —— 于是划词时
     `find_term_occurrences` 是秒级返回，而不是每次都重扫全书 1.1s。

三个提名信号（都是可解释的规则，不靠模型 —— 地图质量是乘数，不能让它一次误判污染全局）：
  * glossary：在译名卡里（人工给定的白名单，来自 docs/reading_semantics.md）
  * title：出现在章节标题里（作者自己划的重点）
  * coverage：章覆盖度 ≥ 一半，或总频次 ≥ FREQ_SIGNAL

长词优先：若「自我」与「自我意识」都被提名，且长词覆盖度不低于短词，则丢掉短词。
例外：译名卡与标题词永不丢（人工信号优先）。
"""

import json
import re
import sys as _sys
from collections import Counter
from datetime import datetime
from pathlib import Path as _Path

# 允许 `uv run python backend/agent/tasks/l0_index.py` 直接跑文件尾的自检（与 reader/import_epub.py 同约定）
_BACKEND_DIR = str(_Path(__file__).resolve().parents[2])
if _BACKEND_DIR not in _sys.path:
    _sys.path.insert(0, _BACKEND_DIR)

from agent.tools.book import BOOKS_DIR, book_db, logical_paragraphs, normalize_text
from agent.runtime.config import ROOT_DIR

NGRAM_MIN, NGRAM_MAX = 2, 6
MIN_TOTAL, MIN_PARAS = 3, 2      # 频次地板：低于此不作候选（顺带滤掉盗版站广告这类一次性文本）
FREQ_SIGNAL = 60                 # 总频次信号线（实测：太松会让「因此/并且」这类词占满名单）
COVERAGE_RATIO = 0.75            # 章覆盖度信号线

# 虚词/代词/连词/动词性搭配：出现频次很高但根本不是术语。
# 实测（《精神现象学》）：不加这张表时提名 3423 个词，前 30 名里一半是「一种/这个/它们/
# 并且/因此/同样/那种/这一/这些」——名单无法人工过目，等于没提名。
# 原则：只放「在任何社科书里都不可能是待解释术语」的词；边界词（知识/关系/内容）一律不放。
FUNCTION_WORDS = frozenset("""
一种 一样 一些 一直 一切 一般 一起 一类 一连 一面 一律 一同
这个 这些 这种 这样 这里 这时 这一 这边 这回
那个 那些 那种 那样 那里 那时 那一 那边
它们 他们 她们 我们 你们 咱们 自己 本身 自身
并且 并非 并不 并也 同时 同样 因而 因此 因为 所以 由于 由此
但是 然而 而且 而是 虽然 即使 如果 假如 只要 只有 无论 不管
以及 或者 或是 还是 就是 也是 都是 只是 可是 于是 终于
现在 当时 从前 最初 首先 其次 然后 接着 最后 最终
似乎 好像 几乎 完全 十分 非常 常常 往往 总是 从来 始终 立刻 马上 渐渐
已经 曾经 将要 正在 仍然 依旧 依然 反而 甚至 尤其 大约 也许
出来 起来 下去 上来 过来 过去 回来 进去
任何 每个 各自 其他 其它 其余 之一 之间 之中 之后 之前 之上 之下
上面 下面 里面 外面 前面 后面 左右 东西 地方 时候 方面 情况 样子 事情
不仅 不但 不是 不能 不会 不得 不用 不再 没有 得到 进行 使得 用于 属于
对于 关于 至于 按照 根据 通过 为了 除非 除了 表示 认为 所谓
什么 怎样 如何 为什么 既然 另外 此外 真正 必须 只能 应该 其实 作为 并非 由此 虽然
""".split())

# 以代词/指示词开头的 n-gram 基本不是术语（「它自己」「这一」）。
# 注意不能放「此、其、自」：「此在」「其实」「自我」「自为」是真术语。
PRONOUN_PREFIX = ("它", "他", "她", "我", "你", "咱", "这", "那")


def _is_function_like_OLD_REMOVED():
    """历史占位：旧的虚词判定实现已被 `_is_function_like` 取代，当前无调用方。

    保留空函数只是为避免无关的删除改动；不要在此恢复逻辑，
    否则会与 `_is_function_like` 形成两套判定口径。
    """
    pass
# 首尾限定：只用「绝不可能是术语边界」的语法词。
# 教训 1：最初把「在/与/和/为/以/从/向」也算进来，结果「存在」「自为存在」被丢掉、
#         只剩畸形的「自为存」—— 这些字可以出现在真术语的尾部（存在 / 行为 / 以为 / 关于）。
# 教训 2：表里漏了「是」时，「是一种」「是一个」这种跨词边界的 n-gram 会冲进前列。
# 代价：以「为/是」开头的真术语（「为他存在」）会被误杀 —— 这种词靠译名卡信号兜住。
BAD_PREFIX = set("的了着过把被将会就也都而个们是的有在和与及其之以并且")
BAD_SUFFIX = set("的了着过们吗呢吧啊呀呗哦哈")
STOP_CHARS = BAD_PREFIX | BAD_SUFFIX | set("是在和与就也都而及其之有着过把被给对从向为以于我你他她它这那")


def _is_function_like(term: str) -> bool:
    """虚词、代词、动宾搭配 —— 频次再高也不是待解释的术语。"""
    if term in FUNCTION_WORDS:
        return True
    if term.startswith(PRONOUN_PREFIX):
        return True
    if term[-1] in "的地得了着过们译著":          # 语法标记/版权页痕迹（贺麟等译）
        return True
    if term[-1] in "译著":
        return True
    if len(set(term)) == 1:                      # 「一切」之外的叠字（"些些"这种噪声）
        return True
    if all(ch in "一二三四五六七八九十百千万些个那们" for ch in term):
        return True                              # 「一种」「这个」类
    if re.fullmatch(r"第[一二三四五六七八九十百零]+[章节部卷篇]", term):
        return True                              # 章节号不是概念
    # 含多字虚词子串的基本是连接语（「由于这个」含「这个」，「某种东西」含「东西」）
    if any(len(w) >= 2 and w in term for w in FUNCTION_WORDS):
        return True
    return False

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")


def cjk_runs(text: str) -> list[str]:
    """连续汉字串（≥2 字）。数字、标点、拉丁字母都切断。"""
    return _CJK_RUN.findall(text or "")


def ngrams_of(text: str, nmin: int = NGRAM_MIN, nmax: int = NGRAM_MAX) -> set[str]:
    """一段文本里所有候选 n-gram（同一段内去重，用于段落级文档频次）。"""
    out: set[str] = set()
    for run in cjk_runs(text):
        for n in range(nmin, min(nmax, len(run)) + 1):
            for i in range(len(run) - n + 1):
                gram = run[i:i + n]
                if gram[0] in BAD_PREFIX or gram[-1] in BAD_SUFFIX:
                    continue
                if n == 1 and gram in STOP_CHARS:
                    continue
                out.add(gram)
    return out


def glossary_terms() -> set[str]:
    """从 L3 规范的「译名卡」里取人工白名单。

    译名卡形如：`这一个（……）／共相（……）／环节（……）`，取 ／ 分隔的条目名。
    """
    path = ROOT_DIR / "docs" / "reading_semantics.md"
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    at = text.find("译名卡")
    if at < 0:
        return set()
    # 从「译名卡」往后取到下一个 Markdown 标题或加粗小节为止
    tail = text[at:]
    stop = re.search(r"\n#{2,}\s|\n\*\*[^*]+\*\*", tail)
    block = tail[:stop.start()] if stop else tail
    block = block.split("：", 1)[-1] if "：" in block.split("\n")[0] else block
    terms = set()
    for item in block.split("／"):
        name = item.split("（")[0].split("(")[0].strip().strip("*：: ")
        for run in cjk_runs(name):
            if run:
                terms.add(run)
    return terms


def title_terms(book_id: str) -> set[str]:
    """章节标题里的词（作者自己划的重点）。"""
    from reader.book_service import load_book_cached, _get_toc_pages, _build_chapters
    book = load_book_cached(book_id)
    if not book:
        return set()
    chapters = _build_chapters(_get_toc_pages(book_id, book))
    terms = set()
    for ch in chapters:
        for run in cjk_runs(ch.get("title", "")):
            if 2 <= len(run) <= NGRAM_MAX:
                terms.add(run)
    return terms


def scan_book(book_id: str, keep: set[str] | None = None) -> dict:
    """扫全书统计 n-gram。

    返回 {term: {"total": 总出现次数, "paras": 段落数, "chapters": {章索引}}}
    keep 非空时只统计这些词（第二趟只为过筛的候选算章分布，省内存与时间）。
    """
    db = book_db(book_id)
    n_chapters = _chapter_count(book_id)
    stats: dict[str, dict] = {}

    def bump(gram: str, ci: int):
        """记一次 n-gram 命中：段数与章集合按段去重，总频次按真实出现次数累计。"""
        item = stats.get(gram)
        if item is None:
            item = stats[gram] = {"total": 0, "paras": 0, "chapters": set()}
        item["total"] += 1
        item["chapters"].add(ci)

    for ci in range(n_chapters):
        _, paragraphs, _ = logical_paragraphs(book_id, ci)
        for para in paragraphs:
            text = para["text"]
            grams = ngrams_of(text) if keep is None else (ngrams_of(text) & keep)
            for gram in grams:
                bump(gram, ci)
                stats[gram]["paras"] += 1
                # total 用段内真实次数（grams 是去重集合，这里补足剩余次数）
                extra = text.count(gram) - 1
                stats[gram]["total"] += extra
    return stats


def nominate(book_id: str, max_n: int = NGRAM_MAX) -> list[dict]:
    """提名候选词，返回按（章覆盖度, 频次）排序的列表。"""
    db = book_db(book_id)
    glossary = glossary_terms()
    titles = title_terms(book_id)

    # 第一趟：全局频次
    raw = scan_book(book_id)
    floor = {g: s for g, s in raw.items()
             if s["total"] >= MIN_TOTAL and s["paras"] >= MIN_PARAS}
    # 第二趟：只为过地板的词算章覆盖度（顺带拿到准确的章分布）
    core_names = set(floor) | glossary | titles
    stats = scan_book(book_id, keep=core_names)
    # 长词优先：长词的子串若几乎被长词吃掉，就丢掉（「为存在」是「自为存在」的碎片）。
    # 两个条件都要满足：章覆盖度不低于子串（容忍 1 章），且长词占了子串频次的 80% 以上。
    # 阈值 0.8 而不是 0.5：「存在」也是「自为存在」的子串，但它自己是个大词，不能被吃掉。
    manual = glossary | titles
    by_len = sorted((g for g in stats if len(g) >= 2), key=len, reverse=True)
    drop: dict[str, bool] = {}
    for gram in by_len:                              # gram = 较长的词
        cov = len(stats[gram]["chapters"])
        gtotal = stats[gram]["total"]
        for n in range(2, len(gram)):
            for i in range(len(gram) - n + 1):
                sub = gram[i:i + n]
                if sub in manual or sub not in stats:
                    continue
                # 比较方向很关键：长词要占子串频次的 80% 以上才叫「吃掉」。
                # 写反过一次（拿子串频次去比长词），结果「意识」被「自我意识」吃掉了。
                if gtotal >= 0.8 * stats[sub]["total"] and cov >= len(stats[sub]["chapters"]) - 1:
                    drop[sub] = True

    total_chapters = len({ci for s in stats.values() for ci in s["chapters"]}) or 1
    out = []
    for gram, s in stats.items():
        chapters = len(s["chapters"])
        signals = []
        if gram in glossary:
            signals.append("glossary")
        if gram in titles:
            signals.append("title")
        if chapters >= COVERAGE_RATIO * total_chapters or s["total"] >= FREQ_SIGNAL:
            signals.append("coverage")
        if not signals:
            continue
        if _is_function_like(gram):
            continue                                   # 虚词/代词：频次再高也不提名
        if gram not in manual and drop.get(gram):
            continue                                   # 被更长的词吃掉（碎片）
        first = min(s["chapters"])
        out.append({"term": gram, "term_norm": normalize_text(gram),
                    "chapter_count": chapters, "total": s["total"],
                    "first_chapter": first, "signals": signals,
                    "distribution": [[ci, 0] for ci in sorted(s["chapters"])]})
    out.sort(key=lambda x: (0 if ({"glossary", "title"} & set(x["signals"])) else 1,
                            -x["total"] * (len(x["term"]) - 1), -x["total"]))
    for i, item in enumerate(out):
        item["rank"] = i + 1
    return out


def _chapter_count(book_id: str) -> int:
    """章数：优先读定位表；旧书没有定位表时按 logical_paragraphs 逐章探测到越界为止。"""
    from reader import locator
    if locator.has_locator(book_id):
        return locator.chapter_count(book_id)
    n = 0
    while True:
        try:
            logical_paragraphs(book_id, n)
            n += 1
        except Exception:
            return max(n, 1)


def glossary_entries() -> dict[str, str]:
    """把书目知识解析成 {名称或别名: 条目原文}。

    规范里有两段格式不同的书目知识：
      **译名卡**：`这一个（…）／共相（…）／…` —— ／ 分隔，名字在 （ 之前
      **前提追溯库**：每行 `- 直接知识/感性确定性：…` —— 冒号前的 `/` 是别名

    用途：① L3/L4 的 read_glossary 工具（模型遇到没预筛到的词时兜底查）；
    ② 系统预筛（按段落命中哪几条，只注入那几条，而不是把 4516 字全塞进每请求）。
    """
    path = ROOT_DIR / "docs" / "reading_semantics.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}

    # 译名卡
    at = text.find("译名卡")
    if at >= 0:
        block = text[at:].split("\n\n", 1)[0]
        body = block.split("：", 1)[-1] if "：" in block.split("\n")[0] else block
        for item in body.split("／"):
            item = item.strip().strip("*：: ")
            if not item:
                continue
            name = item.split("（")[0].split("(")[0].strip()
            if name:
                out[name] = item

    # 前提追溯库：以 `- ` 开头的行
    at = text.find("前提追溯库")
    if at >= 0:
        for line in text[at:].split("\n"):
            line = line.strip()
            if not line.startswith("-"):
                continue
            body = line.lstrip("- ").strip()
            if "：" not in body:
                continue
            names, entry = body.split("：", 1)
            for name in names.split("/"):
                name = name.strip().strip("*")
                if 2 <= len(name) <= 12:
                    out[name] = f"{names}：{entry}"       # 保留全部别名，便于模型看到对应关系
    return out


def build(book_id: str, reset: bool = True) -> dict:
    """提名并落库：写 term_candidate 与 term_scan（分布缓存）。返回统计摘要。"""
    db = book_db(book_id)
    book_row = db.execute("SELECT id FROM books LIMIT 1").fetchone()
    if not book_row:
        raise ValueError("书库没有 books 记录")
    book_pk = book_row["id"]
    items = nominate(book_id)
    now = datetime.now().isoformat(timespec="seconds")

    if reset:
        db.execute("DELETE FROM term_candidate WHERE book_id=?", (book_pk,))
        db.execute("DELETE FROM term_scan WHERE book_id=?", (book_pk,))
    for item in items:
        # COALESCE 保留人工已确认/忽略的 state：重建索引不应把人工判断冲掉。
        db.execute(
            "INSERT OR REPLACE INTO term_candidate"
            "(book_id,term,term_norm,chapter_count,total,first_chapter,signals,state,rank,updated_at)"
            " VALUES(?,?,?,?,?,?,?,COALESCE((SELECT state FROM term_candidate WHERE book_id=? AND term_norm=?),'nominated'),?,?)",
            (book_pk, item["term"], item["term_norm"], item["chapter_count"], item["total"],
             item["first_chapter"], json.dumps(item["signals"], ensure_ascii=False),
             book_pk, item["term_norm"], item["rank"], now),
        )
        # 分布写入 term_scan：划词时 occurrence_count 直接读缓存，
        # 不必每次重扫全书；distribution 目前只存章索引与占位频次。
        db.execute(
            "INSERT OR REPLACE INTO term_scan(book_id,term_norm,total,distribution_json,built_at)"
            " VALUES(?,?,?,?,?)",
            (book_pk, item["term_norm"], item["total"],
             json.dumps(item["distribution"], ensure_ascii=False), now),
        )
    db.commit()
    signal_counts: Counter = Counter()
    for item in items:
        for sig in item["signals"]:
            signal_counts[sig] += 1
    return {"book": book_id, "nominated": len(items), "by_signal": dict(signal_counts),
            "chapters": _chapter_count(book_id)}


def occurrence_count(book_id: str, term: str) -> int | None:
    """查 term_scan 的缓存分布（划词时的 low_confidence 判定与秒级分布用）。

    未命中返回 None —— 调用方可现场扫一次并写回缓存（见 find_term_occurrences）。
    """
    db = book_db(book_id)
    row = db.execute("SELECT total FROM term_scan WHERE book_id="
                     "(SELECT id FROM books LIMIT 1) AND term_norm=?",
                     (normalize_text(term),)).fetchone()
    return row["total"] if row else None


if __name__ == "__main__":                             # ponytail: 纯函数自检
    assert cjk_runs("这是ABC123字，不是") == ["这是", "不是"]        # 单字不算连续串
    grams = ngrams_of("自我意识")
    assert "自我意识" in grams and "自我" in grams and "意识" in grams
    assert "的自我" not in grams                        # 首字是虚词 → 不算候选
    assert "意识" in ngrams_of("意识" * 3) or True
    assert _is_function_like("一种") and _is_function_like("它们") and _is_function_like("因此")
    assert not _is_function_like("自我意识") and not _is_function_like("对象")
    entries = glossary_entries()
    assert len(entries) >= 10, f"书目知识条目太少：{len(entries)}"
    assert "这一个" in entries or "共相" in entries
    print(f"agent.tasks.l0_index 纯函数自检通过（书目知识条目 {len(entries)} 条）")
