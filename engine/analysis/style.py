"""风格相似度：人工设计的统计风格向量 + 余弦相似度，完全离线。

刻意不使用“AI 判定”模型：这里只测量文本之间在功能词用法、词性分布和
表层统计上的接近程度，可用于“与某批样本风格接近/差异明显”的线索性提示。
"""

import math
from collections import Counter

from .metrics import (
    alpha_words,
    hapax_ratio,
    mtld,
    sentence_stats,
    type_token_ratio,
)
from .nlp import Doc
from .version import FEATURE_VERSION, MIN_STYLE_SAMPLES

# 固定顺序、固定集合：保证不同文本/不同时间的向量维度一致（v1 契约）。
FUNCTION_WORDS = [
    "the", "a", "an", "and", "or", "but", "if", "because", "while", "of",
    "at", "by", "for", "with", "to", "from", "in", "on", "as", "that",
    "which", "who", "what", "this", "it", "is", "are", "was", "be", "not",
]
POS_GROUPS = [
    "NOUN", "PROPN", "VERB", "AUX", "ADJ", "ADV",
    "DET", "ADP", "PRON", "CCONJ", "SCONJ", "PART",
]


def _l2_normalize(values):
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        return values
    return [v / norm for v in values]


def build_feature_vector(doc: Doc):
    """返回 (keys, normalized_values)；keys 是 v1 的维度契约。"""
    words = alpha_words(doc)
    n_words = max(1, len(words))
    word_counter = Counter(words)

    pos_counter = Counter()
    for sent in doc.sentences:
        for tok in sent.tokens:
            if tok.is_alpha:
                pos_counter[tok.pos] += 1
    n_pos = max(1, sum(pos_counter.values()))

    # --- 6 个标量 ---
    lengths = [len(w) for w in words]
    mean_word_len = sum(lengths) / n_words if words else 0.0
    sent_counts = [
        sum(1 for t in s.tokens if t.is_alpha) for s in doc.sentences
    ]
    mean_sent_len = (
        sum(sent_counts) / len(sent_counts) if sent_counts else 0.0
    )
    ttr = type_token_ratio(words)
    mtld_val = mtld(words)
    hapax = hapax_ratio(words)
    commas = 0
    for s in doc.sentences:
        commas += s.text.count(",") + s.text.count("，")
    commas_per_sent = commas / len(doc.sentences) if doc.sentences else 0.0
    scalars = {
        "mean_word_len": mean_word_len,
        "mean_sentence_len/10": mean_sent_len / 10.0,
        "ttr": ttr,
        "mtld/100": mtld_val / 100.0,
        "hapax": hapax,
        "commas_per_sentence": commas_per_sent,
    }

    keys = [f"fw:{w}" for w in FUNCTION_WORDS]
    values = [word_counter.get(w, 0) / n_words for w in FUNCTION_WORDS]
    keys += [f"pos:{p}" for p in POS_GROUPS]
    values += [pos_counter.get(p, 0) / n_pos for p in POS_GROUPS]
    keys += [f"stat:{k}" for k in scalars]
    values += list(scalars.values())

    return keys, _l2_normalize(values)


def cosine(vec_a, vec_b):
    # 向量均已 L2 归一化；为稳健仍显式处理模长。
    na = math.sqrt(sum(v * v for v in vec_a))
    nb = math.sqrt(sum(v * v for v in vec_b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(vec_a, vec_b)) / (na * nb)


def compare(doc: Doc, eligible_samples):
    """将文档与样本逐一比对。

    eligible_samples: 已排除内容摘要相同（同文本自比）的样本 queryset/list。
    样本不足时返回 ``None`` 而不是猜测数值。
    """
    if len(eligible_samples) < MIN_STYLE_SAMPLES:
        return {
            "style_similarity": None,
            "eligible_sample_count": len(eligible_samples),
            "note": (
                f"可参照样本不足 {MIN_STYLE_SAMPLES} 份（已排除与本文内容摘要相同的样本），"
                "不输出相似度，避免小样本误导。"
            ),
        }

    _, target_vec = build_feature_vector(doc)
    scored = []
    for sample in eligible_samples:
        stored = sample.feature_vector or {}
        if stored.get("version") != FEATURE_VERSION or "v" not in stored:
            continue
        sim = cosine(target_vec, stored["v"])
        scored.append(
            {
                "sample_id": sample.id,
                "name": sample.name,
                "label": sample.label,
                "similarity": round(sim, 4),
            }
        )

    if len(scored) < MIN_STYLE_SAMPLES:
        return {
            "style_similarity": None,
            "eligible_sample_count": len(scored),
            "note": "具有可比特征向量的样本不足，跳过相似度计算。",
        }

    scored.sort(key=lambda x: -x["similarity"])
    values = [s["similarity"] for s in scored]
    by_label = {}
    for s in scored:
        by_label.setdefault(s["label"], []).append(s["similarity"])
    label_mean = {
        label: round(sum(vals) / len(vals), 4) for label, vals in by_label.items()
    }
    return {
        "style_similarity": {
            "mean": round(sum(values) / len(values), 4),
            "max": scored[0]["similarity"],
            "nearest": scored[0],
            "top3": scored[:3],
            "mean_by_label": label_mean,
            "comparison_count": len(scored),
            "interpretation_hint": (
                "相似度高仅表示统计风格接近（可能来自同一作者、同题材或共同模板），"
                "不代表抄袭或 AI 生成。"
            ),
        },
        "eligible_sample_count": len(scored),
        "note": "",
    }
