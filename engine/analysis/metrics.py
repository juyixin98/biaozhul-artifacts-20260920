"""描写性指标：段落/句子长度、词汇丰富度、重复片段。

所有数值为统计线索，不做任何“是否 AI 生成”的判定。
"""

import math
import re
from collections import Counter

from .nlp import Doc
from .version import MTLD_THRESHOLD, REPEAT_NGRAMS


def alpha_words(doc: Doc):
    """全文字母词标（小写原形序列），是 TTR/MTLD 等指标的统一样本空间。"""
    words = []
    for sent in doc.sentences:
        for tok in sent.tokens:
            if tok.is_alpha and tok.lemma.isalpha():
                words.append(tok.lemma.lower())
    return words


def paragraph_stats(text: str):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    counts = [len(_WORD_RE.findall(p)) for p in paragraphs]
    counts = [c for c in counts if c > 0]
    if not counts:
        # 没有空行段落结构时，整体视为一个段落
        counts = [len(_WORD_RE.findall(text))]
    return {
        "paragraph_count": len(counts),
        "words_per_paragraph": _describe(counts),
    }


_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _describe(values):
    n = len(values)
    if n == 0:
        return {"min": 0, "max": 0, "mean": 0.0, "median": 0.0, "stdev": 0.0, "hist": []}
    s = sorted(values)
    mean = sum(s) / n
    var = sum((v - mean) ** 2 for v in s) / n
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    # 直方图：便于教师快速看分布，固定 5 档
    lo, hi = s[0], s[-1]
    hist = [0] * 5
    if hi > lo:
        for v in s:
            idx = min(4, int((v - lo) / (hi - lo + 1e-9) * 5))
            hist[idx] += 1
    else:
        hist[2] = n
    return {
        "min": lo,
        "max": hi,
        "mean": round(mean, 2),
        "median": round(float(median), 2),
        "stdev": round(math.sqrt(var), 2),
        "hist": hist,
    }


def sentence_stats(doc: Doc):
    lengths = [
        sum(1 for t in s.tokens if t.is_alpha)
        for s in doc.sentences
    ]
    lengths = [n for n in lengths if n > 0]
    return {
        "sentence_count": len(lengths),
        "words_per_sentence": _describe(lengths),
    }


def type_token_ratio(words):
    if not words:
        return 0.0
    return len(set(words)) / len(words)


def _mtld_direction(words, threshold=MTLD_THRESHOLD):
    factors = 0.0
    types = set()
    tokens = 0
    for w in words:
        types.add(w)
        tokens += 1
        ttr = len(types) / tokens
        if ttr <= threshold:
            factors += 1.0
            types = set()
            tokens = 0
    if tokens > 0:
        # 部分因子按比例折算
        ttr = len(types) / tokens
        factors += (1.0 - ttr) / (1.0 - threshold)
    return len(words) / factors if factors > 0 else float(len(words))


def mtld(words):
    """双向 MTLD 均值（标准做法），值越高词汇越多样。"""
    if len(words) < 2:
        return 0.0
    forward = _mtld_direction(words)
    backward = _mtld_direction(list(reversed(words)))
    return (forward + backward) / 2.0


def hapax_ratio(words):
    if not words:
        return 0.0
    counts = Counter(words)
    hapax = sum(1 for c in counts.values() if c == 1)
    return hapax / len(counts)


def lexical_diversity(words):
    counts = Counter(words)
    return {
        "word_count": len(words),
        "type_count": len(counts),
        "ttr": round(type_token_ratio(words), 4),
        "mtld": round(mtld(words), 2),
        "hapax_legomena_ratio": round(hapax_ratio(words), 4),
    }


def repeated_fragments(words):
    """词级 n-gram 重复度。

    repeated_share_n：n-gram 多出来的出现所覆盖的词标占全部词标的比例；
    longest_repeat_span：重复 5-gram 在词序上能拼出的最长连续跨度；
    examples：最长重复片段对应的原文短语（便于教师人工核对）。
    """
    result = {}
    n_words = len(words)
    for n in REPEAT_NGRAMS:
        grams = [" ".join(words[i : i + n]) for i in range(n_words - n + 1)]
        counter = Counter(grams)
        if n_words >= n and grams:
            repeated_tokens = sum((c - 1) * n for c in counter.values() if c > 1)
            share = repeated_tokens / n_words
        else:
            share = 0.0
        result[f"repeated_{n}gram_share"] = round(share, 4)

    span, examples = _longest_repeat_span(words)
    result["longest_repeat_span_words"] = span
    result["examples"] = examples
    return result


def _longest_repeat_span(words, n=5, max_examples=3):
    """最长连续重复片段：把每个“起始于该位置的 n-gram 是重复的”位置视为标记，
    最长连续标记段长度 + (n-1) 即最长重复跨度（词数）。"""
    n_words = len(words)
    if n_words < n:
        return 0, []
    grams = [" ".join(words[i : i + n]) for i in range(n_words - n + 1)]
    positions = {}
    for i, g in enumerate(grams):
        positions.setdefault(g, []).append(i)
    repeated = {g for g, pos in positions.items() if len(pos) > 1}
    if not repeated:
        return 0, []

    mark_set = {i for i, g in enumerate(grams) if g in repeated}
    best = 0
    best_start = -1
    for i in mark_set:
        j = i
        while j in mark_set:
            j += 1
        length = (j - i) + n - 1
        if length > best:
            best, best_start = length, i

    examples = []
    if best_start >= 0:
        examples.append(" ".join(words[best_start : best_start + best])[:300])
        top = sorted(repeated, key=lambda g: -len(positions[g]))
        for g in top:
            if len(examples) >= max_examples:
                break
            if g not in examples:
                examples.append(g)
    return best, examples


def repeated_sentences(doc: Doc):
    norm = [re.sub(r"\s+", " ", s.text.lower()).strip() for s in doc.sentences]
    total = len(norm)
    if not total:
        return {"sentence_count": 0, "repeated_sentence_ratio": 0.0}
    counter = Counter(norm)
    # 多余出现次数（每个句子的第一次出现不计重复）
    dup_instances = sum(c - 1 for c in counter.values() if c > 1)
    return {
        "sentence_count": total,
        "repeated_sentence_ratio": round(dup_instances / total, 4),
    }
