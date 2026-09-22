"""分析内核：指标公式、重复片段、样本不足处理、相似度可分性。

测试不依赖 spaCy 模型（正则管线即可验证公式与契约）；
模型存在时额外验证 spaCy 后端可用。
"""

from django.test import SimpleTestCase

from engine.analysis import metrics as M
from engine.analysis import style as S
from engine.analysis.nlp import annotate
from engine.analysis.version import MIN_STYLE_SAMPLES


class _FakeSample:
    def __init__(self, sid, name, label, vec):
        self.id = sid
        self.name = name
        self.label = label
        self.feature_vector = vec


def _vec(text):
    doc = annotate(text)
    keys, values = S.build_feature_vector(doc)
    return {"version": "local-style-v1", "keys": keys, "v": values}


class MetricTests(SimpleTestCase):
    def test_ttr_basic(self):
        words = ["cat", "dog", "cat", "fish"]
        self.assertAlmostEqual(M.type_token_ratio(words), 0.75, places=4)

    def test_hapax(self):
        words = ["a", "a", "b", "c"]
        # 词型 {a,b,c}，hapax {b,c} → 2/3
        self.assertAlmostEqual(M.hapax_ratio(words), 2 / 3, places=4)

    def test_mtld_constant_text_is_low(self):
        words = ["same"] * 200
        val = M.mtld(words)
        # 词汇极度贫乏：每 3 个词 TTR 就跌破 0.72，因子数 ≈ 200/3*k，
        # 故 MTLD 接近 2.x（值越低越贫乏）。
        self.assertLess(val, 3.0)

    def test_mtld_direction(self):
        # MTLD 值越高 → 词汇越多样。
        # 全唯一词：TTR 恒为 1，因子几乎不增长，MTLD ≈ 词长（最高）；
        # 小词表重复：TTR 很快跌破 0.72，频繁产生因子，MTLD 低。
        diverse = [f"word{i}" for i in range(200)]
        repetitive = (["alpha", "beta", "gamma", "delta", "epsilon"] * 40)[:200]
        self.assertGreater(M.mtld(diverse), 2.0 * M.mtld(repetitive))

    def test_repeated_ngram_detects_paste(self):
        sentence = "the quick brown fox jumps over the lazy dog near the river bank today"
        text = f"{sentence}. {sentence}."  # 整句重复
        doc = annotate(text)
        words = M.alpha_words(doc)
        rep = M.repeated_fragments(words)
        self.assertGreater(rep["repeated_5gram_share"], 0.3)
        self.assertGreaterEqual(rep["longest_repeat_span_words"], 10)

    def test_repeated_ngram_clean_text_near_zero(self):
        text = (
            "Different ideas appear in every sentence here. "
            "Unique words fill the following clause as well. "
            "Fresh expressions continue throughout this short passage."
        )
        doc = annotate(text)
        words = M.alpha_words(doc)
        rep = M.repeated_fragments(words)
        self.assertLess(rep["repeated_5gram_share"], 0.05)
        self.assertEqual(rep["longest_repeat_span_words"], 0)

    def test_paragraph_stats(self):
        text = "one two three.\n\nfour five.\n\nsix seven eight nine ten."
        stats = M.paragraph_stats(text)
        self.assertEqual(stats["paragraph_count"], 3)
        self.assertEqual(stats["words_per_paragraph"]["min"], 2)
        self.assertEqual(stats["words_per_paragraph"]["max"], 5)


class StyleTests(SimpleTestCase):
    def test_insufficient_samples_returns_null(self):
        doc = annotate("Some target text with a couple of short sentences here.")
        few = [_FakeSample(i, f"s{i}", "student", _vec(f"sample number {i} text body"))
               for i in range(MIN_STYLE_SAMPLES - 1)]
        out = S.compare(doc, few)
        self.assertIsNone(out["style_similarity"])
        self.assertIn("不足", out["note"])

    def test_self_match_excluded_by_caller(self):
        """内容摘要相同的样本由 pipeline 层在传入前排除（见 test_pipeline）。"""
        text = "A reasonably distinctive target sentence with several unusual words."
        doc = annotate(text)
        vec = _vec(text)
        # 即使错误传入相同向量，余弦=1，但调用方（pipeline）会按 hash 排除
        out = S.compare(doc, [_FakeSample(i, str(i), "student", vec) for i in range(3)])
        self.assertAlmostEqual(
            out["style_similarity"]["max"], 1.0, places=2
        )

    def test_style_separates_two_registers(self):
        formal = (
            "Furthermore, researchers have demonstrated that institutional frameworks "
            "substantially influence economic behavior across multiple regions. "
            "Consequently, policymakers should consider these structural factors carefully."
        )
        informal = (
            "Yesterday I went to the park with my dog. We ran around and I ate ice cream. "
            "It was super fun and I want to go back again really soon."
        )
        target_formal = annotate(
            "Evidence indicates that organizations significantly affect performance; "
            "therefore analysts must account for institutional variables in their models."
        )
        samples = [
            _FakeSample(1, "f1", "reference", _vec(formal)),
            _FakeSample(2, "f2", "reference", _vec(formal.replace("researchers", "scholars"))),
            _FakeSample(3, "f3", "reference", _vec(formal.replace("economic", "social"))),
            _FakeSample(4, "i1", "student", _vec(informal)),
        ]
        out = S.compare(target_formal, samples)
        self.assertIsNotNone(out["style_similarity"])
        nearest = out["style_similarity"]["nearest"]
        self.assertEqual(nearest["label"], "reference")
        self.assertGreater(nearest["similarity"], 0.8)
