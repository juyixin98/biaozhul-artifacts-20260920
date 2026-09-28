"""查询层错误类别。"""


class QueryError(RuntimeError):
    """查询层错误基类。"""

    error_code = "query_error"
    http_status = 400


class QueryRejectedError(QueryError):
    """查询输入非法，请求被拒绝（区别于"查无此词"）。"""

    error_code = "query_rejected"
    http_status = 422


class IndexNotReadyError(QueryError):
    """索引尚未构建，无法服务查询。"""

    error_code = "index_not_ready"
    http_status = 503
