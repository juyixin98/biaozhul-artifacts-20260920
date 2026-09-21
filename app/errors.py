class DomainError(Exception):
    """Business-rule violation, translated to an HTTP response by the handler in main.py."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
