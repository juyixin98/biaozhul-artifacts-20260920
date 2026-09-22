"""任务执行管线：EXTRACT → NLP → METRICS → STYLE → PERSIST。

每个阶段独立抛 StageError（携带阶段标签），保证单文档失败可定位、可重试，
且不会中断整批。``run_all`` 供管理命令/测试一次性使用；worker 按分阶段
函数调用以便在阶段间写入进度。
"""

from . import metrics as metric_mod
from .extract import ExtractionError, content_hash_of, extract
from .nlp import annotate
from .style import FEATURE_VERSION, build_feature_vector, compare
from .version import (
    ALGORITHM_VERSION,
    DISCLAIMER,
    MIN_RELIABLE_WORDS,
    METHODOLOGY,
)


class StageError(Exception):
    def __init__(self, stage, code, message, detail=None):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.detail = detail or {}


def stage_extract(submission):
    try:
        raw = bytes(submission.raw_content)
        text = extract(submission.source_format, raw)
    except ExtractionError as exc:
        raise StageError("EXTRACT", exc.code, exc.message)
    except Exception as exc:
        raise StageError("EXTRACT", "UNEXPECTED", f"提取阶段异常：{exc}")
    return text, content_hash_of(text)


def stage_nlp(text):
    try:
        return annotate(text)
    except Exception as exc:
        raise StageError("NLP", "ANNOTATE_FAILED", f"NLP 标注失败：{exc}")


def stage_metrics(text, doc):
    try:
        words = metric_mod.alpha_words(doc)
        return {
            "paragraph_length": metric_mod.paragraph_stats(text),
            "sentence_length": metric_mod.sentence_stats(doc),
            "lexical_diversity": metric_mod.lexical_diversity(words),
            "repeated_fragments": metric_mod.repeated_fragments(words),
            "repeated_sentences": metric_mod.repeated_sentences(doc),
        }
    except Exception as exc:
        raise StageError("METRICS", "METRIC_FAILED", f"指标计算失败：{exc}")


def stage_style(submission, doc, input_hash):
    try:
        samples = list(
            submission.course.style_samples.exclude(content_hash=input_hash).order_by("id")
        )
        return compare(doc, samples)
    except Exception as exc:
        raise StageError("STYLE", "STYLE_FAILED", f"风格相似度计算失败：{exc}")


def finalize_metrics(result_metrics, doc, style):
    result_metrics["style_similarity"] = style["style_similarity"]
    result_metrics["style_eligible_sample_count"] = style["eligible_sample_count"]
    if style["note"]:
        result_metrics["style_note"] = style["note"]

    warnings = []
    word_count = result_metrics["lexical_diversity"]["word_count"]
    if word_count < MIN_RELIABLE_WORDS:
        warnings.append(
            f"low_word_count：全文仅 {word_count} 词（建议 ≥ {MIN_RELIABLE_WORDS}），"
            "指标方差较大，请谨慎解读。"
        )
    if doc.backend != "spacy":
        warnings.append(
            "spacy_unavailable：未加载 spaCy 语言模型，词性/句法相关特征使用"
            "正则退化管线，风格相似度区分力下降（公式不变，见 methodology）。"
        )

    result_metrics["disclaimer"] = DISCLAIMER
    result_metrics["warnings"] = warnings
    result_metrics["nlp_backend"] = doc.backend
    return result_metrics


def run_all(submission):
    """一次性跑完全部分析阶段，返回 (metrics, methodology, input_hash)。"""
    text, input_hash = stage_extract(submission)
    doc = stage_nlp(text)
    result_metrics = stage_metrics(text, doc)
    style = stage_style(submission, doc, input_hash)
    finalize_metrics(result_metrics, doc, style)
    result_metrics["char_count"] = len(text)
    return result_metrics, METHODOLOGY, input_hash


# 向后兼容别名（worker/命令历史调用名）
run_pipeline = run_all


def feature_vector_for_sample(text):
    """上传风格样本时计算并持久化其特征向量。"""
    doc = annotate(text)
    keys, values = build_feature_vector(doc)
    return {"version": FEATURE_VERSION, "keys": keys, "v": values}
