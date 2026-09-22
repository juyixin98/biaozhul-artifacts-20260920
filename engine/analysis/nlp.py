"""NLP 标注层：spaCy 为主，缺模型/缺依赖时退化为正则分词。

退化模式下结果仍可计算，但会在结果 warnings 中标注 ``spacy_unavailable``，
使指标的算法前提对调用方透明。NLTK 负责功能词表（punkt 缺失时回退正则分句）。
"""

import logging
import re
from dataclasses import dataclass

from django.conf import settings

from .version import ALGORITHM_VERSION

logger = logging.getLogger(__name__)


@dataclass
class Token:
    text: str
    lemma: str
    pos: str  # 统一到 UD 粗词性（NOUN/VERB/...）
    is_alpha: bool


@dataclass
class Sentence:
    text: str
    tokens: list  # list[Token]


@dataclass
class Doc:
    text: str
    sentences: list  # list[Sentence]
    backend: str  # "spacy" 或 "regex"


_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_POS_FALLBACK = {"be": "AUX", "am": "AUX", "is": "AUX", "are": "AUX", "was": "AUX",
                 "were": "AUX", "been": "AUX", "being": "AUX", "have": "VERB",
                 "has": "VERB", "had": "VERB", "do": "VERB", "does": "VERB",
                 "did": "VERB", "will": "AUX", "would": "AUX", "can": "AUX",
                 "could": "AUX", "should": "AUX", "shall": "AUX", "may": "AUX",
                 "might": "AUX", "must": "AUX"}


def _load_spacy():
    try:
        import spacy
    except Exception:
        return None, "spacy_import_failed"
    try:
        nlp = spacy.load(settings.SPACY_MODEL, disable=["ner", "lemmatizer"])
        return nlp, "spacy"
    except Exception:
        return None, "model_missing"


_NLP_CACHE = {}


def get_nlp():
    if "nlp" not in _NLP_CACHE:
        _NLP_CACHE["nlp"], _NLP_CACHE["backend"] = _load_spacy()
    return _NLP_CACHE["nlp"], _NLP_CACHE["backend"]


_STOPWORDS = None


def stopwords():
    """英文功能/停用词表；NLTK 数据缺失时用内置小词表兜底。"""
    global _STOPWORDS
    if _STOPWORDS is not None:
        return _STOPWORDS
    try:
        from nltk.corpus import stopwords as nltk_sw

        words = set(nltk_sw.words("english"))
    except Exception:
        words = {
            "the", "a", "an", "and", "or", "but", "if", "while", "of", "at", "by",
            "for", "with", "about", "against", "between", "into", "through", "during",
            "before", "after", "above", "below", "to", "from", "up", "down", "in",
            "out", "on", "off", "over", "under", "again", "further", "then", "once",
            "is", "am", "are", "was", "were", "be", "been", "being", "have", "has",
            "had", "do", "does", "did", "will", "would", "shall", "should", "can",
            "could", "may", "might", "must", "i", "me", "my", "we", "our", "you",
            "your", "he", "him", "his", "she", "her", "it", "its", "they", "them",
            "their", "this", "that", "these", "those", "as", "not", "no", "so",
            "than", "too", "very", "just", "also", "which", "who", "whom", "what",
        }
    _STOPWORDS = words
    return words


def _regex_doc(text: str) -> Doc:
    sentences = []
    for chunk in _SENT_SPLIT_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        toks = []
        for m in _WORD_RE.finditer(chunk):
            w = m.group(0).lower()
            head = w.split("'")[0]
            pos = _POS_FALLBACK.get(head, "X")
            toks.append(Token(text=m.group(0), lemma=head, pos=pos, is_alpha=True))
        if toks:
            sentences.append(Sentence(text=chunk, tokens=toks))
    return Doc(text=text, sentences=sentences, backend="regex")


def _spacy_doc(text: str, nlp) -> Doc:
    spacy_doc = nlp(text)
    sentences = []
    for sent in spacy_doc.sents:
        toks = []
        for t in sent:
            if not t.text.strip():
                continue
            is_alpha = bool(re.search(r"[A-Za-z]", t.text))
            lemma = (t.lemma_ or t.text or "").lower().strip() or t.text.lower()
            toks.append(
                Token(
                    text=t.text,
                    lemma=lemma if lemma and not lemma.startswith("-") else t.text.lower(),
                    pos=t.pos_ or "X",
                    is_alpha=is_alpha,
                )
            )
        if toks:
            sentences.append(Sentence(text=sent.text.strip(), tokens=toks))
    return Doc(text=text, sentences=sentences, backend="spacy")


def annotate(text: str) -> Doc:
    nlp, backend = get_nlp()
    if nlp is not None:
        try:
            return _spacy_doc(text, nlp)
        except Exception:
            logger.exception("spaCy 标注运行期失败，退化为正则管线。")
    return _regex_doc(text)
