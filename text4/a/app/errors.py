class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        super().__init__(message)


def not_found(message: str = "resource not found") -> AppError:
    return AppError(404, "NOT_FOUND", message)


def conflict(code: str, message: str) -> AppError:
    return AppError(409, code, message)


def unprocessable(code: str, message: str) -> AppError:
    return AppError(422, code, message)


def forbidden(message: str = "forbidden") -> AppError:
    return AppError(403, "FORBIDDEN", message)
