"""算法版本与方法论说明。

所有指标随结果一起持久化 ``methodology``，保证历史结果可复现、可解释。
升级算法时提升 ALGORITHM_VERSION：旧结果保留，重跑会生成新版本结果。
"""

ALGORITHM_VERSION = "local-style-v1"
FEATURE_VERSION = "local-style-v1"

# 风格相似度最少参照样本数；不足时不输出相似度，仅给出提示。
MIN_STYLE_SAMPLES = 3
# 文本过短阈值（词数）：低于该值指标可靠性显著下降，仅给线索。
MIN_RELIABLE_WORDS = 50
# 重复片段 n-gram 配置
REPEAT_NGRAMS = (5, 10)
# MTLD 词汇丰富度因子阈值（McCarthy & Jarvis 2010 标准值）
MTLD_THRESHOLD = 0.72

METHODOLOGY = {
    "algorithm_version": ALGORITHM_VERSION,
    "pipeline": [
        "EXTRACT：TXT 使用 chardet 检测编码解码；DOCX 使用 python-docx 提取段落文本。",
        "NLP：spaCy 英文管线（tok2vec/tag/parser/senter）做分句、词性标注；"
        "NLTK 提供功能词/停用词表。环境缺少模型时退化为内置正则分词并在 warnings 中标注。",
        "METRICS：段落长度、句子长度、词汇丰富度（TTR/MTLD/hapax）、重复片段。",
        "STYLE：功能词频 + 词性粗类分布 + 标量统计构成风格向量（L2 归一化），"
        "与课程样本向量逐一计算余弦相似度。",
    ],
    "formulas": {
        "ttr": "TTR = 不同词型数 V / 总词标数 N（统一小写，仅计字母词）。",
        "mtld": (
            "MTLD（Measure of Textual Lexical Diversity，McCarthy & Jarvis, 2010）："
            "顺序累计词标，每当因子内 TTR <= 0.72 记一个完整因子，"
            "因子数 = 完整因子数 + (1 - 末段TTR) / (1 - 0.72)；"
            "MTLD = N / 因子数。取正向、逆向两次计算的均值。"
        ),
        "hapax_ratio": "hapax_ratio = 全文档仅出现 1 次的词型数 / 词型总数 V。",
        "paragraph_words": "段落以空行切分；段落词数为该段落字母词标数。",
        "repeated_ngram_share": (
            "对词级 n-gram（n=5,10）统计：repeated_share = "
            "Σ_ngram (出现次数-1)*n / N，即可被重复片段解释的词标占比。"
        ),
        "longest_repeat_span": (
            "在所有重复 5-gram 中，按相邻滑动窗口拼接得到的最长连续重复词跨度（词数）。"
        ),
        "repeated_sentence_ratio": "规范化（小写、折叠空白）后重复出现的句子实例数 / 句子总数。",
        "style_cosine": (
            "风格向量由 30 个高频功能词的相对频率、12 个词性粗类相对频率、"
            "6 个标量（均词长、均句长、TTR、MTLD/100、hapax、逗号/句）组成，做 L2 归一化；"
            "相似度 = 两向量点积（模长均为 1），范围约 [0,1]，越高表示统计风格越接近。"
        ),
    },
    "insufficient_sample_handling": (
        f"课程内可参照样本少于 {MIN_STYLE_SAMPLES} 份（或全部样本与被分析文本内容摘要相同）时，"
        "style_similarity 置为 null 并给出 insufficient_samples 提示，不猜测相似度。"
        f"全文字数低于 {MIN_RELIABLE_WORDS} 词时输出 low_word_count 警告，数值仍返回但仅供参考。"
    ),
    "interpretation": (
        "所有数值均为描写性统计线索，受题材、篇幅、写作模板影响；"
        "不会也不能据此输出“确定由 AI 生成”或任何作弊结论。"
    ),
}

DISCLAIMER = "本结果仅为线索性统计指标，不构成“AI 生成”或学术不端的确定性结论。"
