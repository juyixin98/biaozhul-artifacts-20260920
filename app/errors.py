class DomainError(Exception):
    """Business-rule violation mapped to an HTTP response."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
