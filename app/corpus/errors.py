"""语料相关错误类别。

错误类别固定且明确，API 层据此映射 HTTP 状态码，禁止把异常或未知状态
统一吞成成功响应。
"""


class CorpusError(ValueError):
    """语料规范错误基类。"""

    error_code = "corpus_error"
    http_status = 400


class InvalidWordError(CorpusError):
    """词项不是字符串或包含不允许的字符。"""

    error_code = "invalid_word"
    http_status = 422


class EmptyWordError(CorpusError):
    """空词被规则明确拒绝（见 ``spec.accept_empty_word``）。"""

    error_code = "empty_word_rejected"
    http_status = 422


class UnorderedCorpusError(CorpusError):
    """输入词表不是严格有序，且未声明允许排序。"""

    error_code = "unordered_corpus"
    http_status = 422


class FixtureNotFoundError(CorpusError):
    """请求的命名合成夹具不存在。"""

    error_code = "unknown_fixture"
    http_status = 404
