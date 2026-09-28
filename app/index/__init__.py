"""索引与模型层：SQLite 持久化、引用完整性校验、索引编排。"""

from app.index.errors import (
    IndexError as IndexError,
    IndexIntegrityError,
    IntegrityViolation,
)
from app.index.model import IndexMetadata, PersistedIndex
from app.index.repository import SQLiteIndexRepository
from app.index.service import IndexService

__all__ = [
    "IndexError",
    "IndexIntegrityError",
    "IntegrityViolation",
    "IndexMetadata",
    "PersistedIndex",
    "SQLiteIndexRepository",
    "IndexService",
]
