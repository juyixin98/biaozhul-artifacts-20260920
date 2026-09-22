from rest_framework.pagination import PageNumberPagination


class CursorPageNumberPagination(PageNumberPagination):
    """通用页码分页（同步接口另有游标分页，不受此影响）。"""

    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 500
