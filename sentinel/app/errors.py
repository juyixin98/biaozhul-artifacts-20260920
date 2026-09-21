from __future__ import annotations

from fastapi import HTTPException, status


class ConflictError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class BatchRejected(HTTPException):
    def __init__(self, detail):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
