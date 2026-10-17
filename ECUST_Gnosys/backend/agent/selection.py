"""划词输入拦截：把任意脏划词收敛成规范的 (term, 位置)。

设计依据：docs/概念层_设计_v1.md §3、附录 E1/E2。
纯本地判定，**不发模型请求**；前端做粗筛（空/长度）拿即时反馈，后端做精筛（回归原文）。

四种结果（不是一个布尔）：

| action    | 含义                       | 谁决定后续             |
|-----------|----------------------------|------------------------|
| pass      | 干净，直接用               | 走定位/建图            |
| corrected | 已按原文校正（脏字/错字）  | 直接用，并告知读者     |
| confirm   | 不确定（划多了 / 像划错字）| 给疑似候选，等读者点选 |
| reject    | 不解释                     | 返回统一失败用语       |

两条铁律：
  * **校正后的原文片段才是 term** —— 它同时是缓存键与入库键，于是手滑、多划、
    错字三种输入会收敛到同一条词条（否则同一术语会因划法不同生出多份解释）。
  * **严重不确定时不猜** —— 超长、像划错字一律走 confirm 让读者确认，不静默改写。
    静默校正只给「同一段文字的不同写法」（全半角/空白），那没有歧义。
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# 实测（三本书正文统计）：真实术语最长 12 字（「费希特和谢林的体系之差异」），
# 超出 12 字的几乎全是书名（最长 17）与句子片段。
MAX_TERM_CHARS = 12
# 全书只出现 1 次 → 大概率是 OCR 错字，标 low_confidence（不拒绝）
MIN_OCCURRENCES = 2
# 容错对齐的门槛：低于 CONFIRM 直接判「原文没有」；低于 AUTO 只提示不静默改写
FUZZY_CONFIRM_RATIO = 0.5
FUZZY_AUTO_RATIO = 0.75
# 容错只对「较长片段」启用：读者是用鼠标划词、不会手打错字，短串的差异
# 更像是划错了地方或前后端文本不一致，此时猜出来的候选是赌运气
# （实测「对像」会被猜成「对于」），不如直接请他重划。
FUZZY_MIN_LEN = 4

BOUNDARY = "，。、；：？！“”‘’《》〈〉（）【】〔〕…—·,.;:?!\"'()[]{}<> \t\n"
# 单字虚词只用于「整串都是虚词」的判定
STOPWORDS = frozenset("的了是在和与就也都而及其之有着过把被给对从向为以于")
ALL_STOPWORDS = STOPWORDS | frozenset("我你他她它这那")


@dataclass(frozen=True)
class Decision:
    action: str                                  # pass / corrected / confirm / reject
    term: str = ""                               # 以原文为准的词条（confirm 时为空）
    reason: str = ""                             # 机器可读：not_text / not_a_term / ...
    message: str = ""                            # 给读者看的话
    candidates: tuple[str, ...] = ()             # confirm 时的疑似候选
    offset: tuple[int, int] | None = None        # 在段落原文里的字符区间
    low_confidence: bool = False

    @property
    def ok(self) -> bool:
        return self.action != "reject"


def _norm_index(text: str) -> tuple[str, list[int]]:
    """规范化文本 + 「规范化下标 → 原文下标」映射（把命中的位置还原成原文坐标）。

    与 agent/book.py.normalize_text 同规则（NFKC + 去空白 + 小写），但额外保留下标映射，
    否则去空白之后位置就和原文对不上了。
    """
    out, idx = [], []
    for i, ch in enumerate(text or ""):
        n = unicodedata.normalize("NFKC", ch).lower()
        if n.isspace():
            continue
        out.append(n)
        idx.append(i)
    return "".join(out), idx


def locate(selection: str, paragraph: str) -> tuple[int, int] | None:
    """精确 → 归一化（全半角/空白/引号）。返回 selection 在 paragraph 里的**原文**区间。"""
    start = paragraph.find(selection)
    if start >= 0:
        return start, start + len(selection)
    norm_sel, _ = _norm_index(selection)
    if not norm_sel:
        return None
    norm_par, idx = _norm_index(paragraph)
    at = norm_par.find(norm_sel)
    if at >= 0:
        return idx[at], idx[at + len(norm_sel) - 1] + 1
    return None


def fuzzy_window(selection: str, paragraph: str) -> tuple[float, int, int] | None:
    """在长度相近的窗口里找最像的一段 —— 处理「划的字不一样」。

    返回 (相似度, 起, 止)。只用 ≥ n 的窗口，见下面的注释。
    """
    n = len(selection)
    if not n or not paragraph:
        return None
    best: tuple[float, int, int] | None = None
    # 窗口不得短于原选（原文应该覆盖住划选内容）。允许更短的窗口会让相似度
    # 偏向“截短后更整齐”的片段（实测会把「只能根据已获致的结果」截成
    # 「根据已获致的结」而拿到更高分），所以宽度只从 len 开始往上试。
    for width in range(n, n + 3):
        if width > len(paragraph):
            continue
        for start in range(len(paragraph) - width + 1):
            window = paragraph[start:start + width]
            ratio = SequenceMatcher(None, selection, window).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, start, start + width)
    if best and best[0] >= FUZZY_CONFIRM_RATIO:
        return best
    return None


def _strip(text: str) -> str:
    return text.strip(BOUNDARY).strip()


def _is_function_word_run(text: str) -> bool:
    """整串几乎都是虚词（「的了是」「的话」）—— 不是术语，不必去原文里找。"""
    if not text or len(text) > 4:
        return False
    return sum(1 for ch in text if ch in ALL_STOPWORDS) / len(text) >= 0.6


def _looks_like_sentence(text: str) -> bool:
    if re.search(r"[。！？!?；;]", text):
        return True                                   # 含句末标点 = 句子，不是术语
    if len(text) <= MAX_TERM_CHARS:
        return False
    stop = sum(1 for ch in text if ch in ALL_STOPWORDS)
    return stop >= 3 or stop / len(text) > 0.25        # 虚词扎堆 = 句子片段


def candidates_of(raw: str, glossary: dict[str, str] | None = None) -> tuple[str, ...]:
    """划多了时的疑似候选。可靠性从高到低：术语表反向匹配 → 标点切分 → 两端收缩。"""
    if glossary:
        hits = [t for t in glossary if t in raw]
        if hits:
            return tuple(sorted(hits, key=len, reverse=True))[:3]
    parts = [_strip(p) for p in re.split(r"[、，,／/；;]", raw)]
    parts = [p for p in parts if p and len(p) <= MAX_TERM_CHARS]
    if parts:
        return tuple(parts)[:3]
    if len(raw) > MAX_TERM_CHARS:                      # 无标点长片段：两端各取一段
        return (_strip(raw[:MAX_TERM_CHARS]), _strip(raw[-MAX_TERM_CHARS:]))
    return ()


def gate(selection: str, paragraph: str, occurrences: int | None = None,
         glossary: dict[str, str] | None = None) -> Decision:
    """入口。

    paragraph   用户划词所在段落的原文（前端带过来，或后端按段落文本反查）
    occurrences 该词在全书的出现次数（由调用方查 L0 索引 / term_scan；None 表示不判）
    glossary    术语表（term → 说明），用于超长时的候选匹配；目前可为 None
    """
    s = _strip(selection or "")
    if not s:
        return Decision("reject", reason="not_text", message="没有选中文本")
    if not re.search(r"[^\W\d_]", s, re.UNICODE) or re.fullmatch(r"[\W\d_]+", s):
        return Decision("reject", reason="not_text", message="这不是文字，请选中一个术语")
    if _is_function_word_run(s):
        return Decision("reject", reason="not_a_term", message="这是一个常见虚词，不需要解释")

    low = bool(occurrences is not None and occurrences < MIN_OCCURRENCES)
    note = ""
    span = locate(s, paragraph)
    if span is None:
        fuzzy = fuzzy_window(s, paragraph) if len(s) >= FUZZY_MIN_LEN else None
        if fuzzy is None:
            return Decision("reject", reason="no_source_evidence",
                            message="这段原文里找不到你划的内容，请重新划选")
        ratio, a, b = fuzzy
        guess = paragraph[a:b]
        if ratio < FUZZY_AUTO_RATIO:                   # 低相似度：只提示，不静默改写
            return Decision("confirm", reason="typo_suspect", offset=(a, b),
                            candidates=(guess,), low_confidence=low,
                            message=f"原文里找到的是「{guess}」，你要解释的是它吗？")
        span, note = (a, b), f"已按原文校正为「{guess}」"

    raw = paragraph[span[0]:span[1]]                   # 以原文为准
    if len(raw) > MAX_TERM_CHARS or _looks_like_sentence(raw):
        cands = candidates_of(raw, glossary)
        if not cands:
            return Decision("reject", reason="too_long",
                            message="划的范围太长，请只划一个术语")
        return Decision("confirm", reason="oversized", offset=span, candidates=cands,
                        low_confidence=low, message="划的范围偏大，你想解释的是哪个？")

    note = note or (f"已按原文校正为「{raw}」" if raw != s else "")
    return Decision("corrected" if note else "pass", term=raw, offset=span,
                    reason="corrected" if note else "ok", message=note, low_confidence=low)


if __name__ == "__main__":                             # ponytail: 一条可运行的自检
    para = "对于这个对象，我们现在必须加以较确切的规定，只能根据已获致的结果。"

    assert gate("对象", para) == Decision("pass", term="对象", offset=(4, 6), reason="ok")
    assert gate("对象，", para).term == "对象"                       # 划多了标点 → 静默校正
    assert gate("对象　", para).term == "对象"                       # 全角空白
    assert gate("的了是", para).action == "reject"                    # 虚词扎堆
    # 「存在」含虚词「在」，不能被虚词规则误杀（阈值故意留在 0.6）
    assert gate("存在", "存在与时间。").action == "pass"
    assert gate("页面", para).reason == "no_source_evidence"          # 原文没有
    d = gate("必须加以较确切的规定，只能根据已获致的结果", para)          # 超长
    assert d.action == "confirm" and d.candidates and not d.term       # 不自动采纳
    d2 = gate("对像", para)                                           # 短串对不上：请他重划
    assert d2.action == "reject" and d2.reason == "no_source_evidence"
    d3 = gate("它所根据已获致的结果", para)                            # 长且像 → 静默校正
    assert d3.action == "corrected" and d3.term == "只能根据已获致的结果"
    assert gate("畏", "畏是一种情绪。").action == "pass"                # 单字术语放行
    assert gate("自我意识", "自我意识是真理。").action == "pass"         # 不被虚词表误杀
    assert gate("对象", para, occurrences=1).low_confidence            # 只出现 1 次
    assert gate("对象", para, occurrences=370).low_confidence is False
    gl = {"对象": "", "自我意识": ""}
    p3 = "在书中，对象与自我意识的关系构成了全书的主线。"
    d4 = gate("对象与自我意识的关系构成了全书的主线", p3, glossary=gl)
    assert d4.action == "confirm" and d4.candidates == ("自我意识", "对象")   # 术语表反向匹配优先
    print("agent.selection 自检通过")
