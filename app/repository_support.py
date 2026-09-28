"""组合根（composition root）：把数据库、保险库、仓储、服务装在一起。

应用与复现实验都通过这里构造，避免在多处重复接线。
"""
from __future__ import annotations

from .core.vault import SeedVault
from .evidence.audit_log import AuditFileLogger
from .service import AllocationService
from .storage.database import Database
from .storage.repository import Repository


def make_service(*, db_path: str, seeds_path: str,
                 log_path: str | None = None) -> dict:
    db = Database(db_path)
    vault = SeedVault(seeds_path)
    repo = Repository()
    service = AllocationService(db, vault, repo)
    audit_log = AuditFileLogger(log_path)
    return {
        "db": db, "vault": vault, "repo": repo,
        "service": service, "audit_log": audit_log,
        "locations": {"database": db_path, "seeds": seeds_path,
                      "audit_log": log_path},
    }
