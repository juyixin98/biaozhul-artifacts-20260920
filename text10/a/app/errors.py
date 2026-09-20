class DomainError(Exception):
    """Business-rule violation mapped to an HTTP response."""

    def __init__(self, status_code: int, code: str, detail):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def register_exception_handlers(app):
    from fastapi.responses import JSONResponse

    @app.exception_handler(DomainError)
    async def domain_error_handler(request, exc: DomainError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "detail": exc.detail}},
        )
