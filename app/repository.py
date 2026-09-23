"""存储层：统一仓储接口 + 进程内内存实现 / PostgreSQL 实现。

事件表只增不改（append-only）；报告按版本永久保留（迟到事件重算 -> 新版本）。
"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Base, EventRow, ReportRow


class EventRepository(Protocol):
    async def init_schema(self) -> None: ...
    async def get_existing_ids(self, ids: list[str]) -> set[str]: ...
    async def get_max_ts(self) -> int | None: ...
    async def insert_events(
        self, rows: list[dict], *, late: bool, replay_version: int
    ) -> list[dict]: ...
    async def list_events(self) -> list[dict]: ...
    async def save_report(
        self,
        *,
        digest: str,
        prev_digest: str | None,
        body: dict,
        signature: str,
        created_from_ts: int,
    ) -> int: ...
    async def get_report(self, version: int | None) -> dict | None: ...
    async def list_report_versions(self) -> list[dict]: ...
    async def reset(self) -> None: ...


class MemoryRepository:
    """进程内内存实现：测试与无 DATABASE_URL 时使用，语义与 PG 版一致。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._reports: dict[int, dict] = {}

    async def init_schema(self) -> None:
        return None

    async def get_existing_ids(self, ids: list[str]) -> set[str]:
        known = {e["event_id"] for e in self._events}
        return {i for i in ids if i in known}

    async def get_max_ts(self) -> int | None:
        return max((e["ts"] for e in self._events), default=None)

    async def insert_events(
        self, rows: list[dict], *, late: bool, replay_version: int
    ) -> list[dict]:
        saved: list[dict] = []
        for r in rows:
            seq = len(self._events) + 1
            row = {
                "seq": seq,
                "event_id": r["event_id"],
                "ts": r["ts"],
                "type": r["type"],
                "payload": r["payload"],
                "sig": r.get("sig"),
                "late": late,
                "replay_version_after": replay_version,
            }
            self._events.append(row)
            saved.append(dict(row))
        return saved

    async def list_events(self) -> list[dict]:
        return [dict(e) for e in self._events]

    async def save_report(
        self,
        *,
        digest: str,
        prev_digest: str | None,
        body: dict,
        signature: str,
        created_from_ts: int,
    ) -> int:
        version = len(self._reports) + 1
        self._reports[version] = {
            "version": version,
            "digest": digest,
            "prev_digest": prev_digest,
            "body": body,
            "signature": signature,
            "created_from_ts": created_from_ts,
        }
        return version

    async def get_report(self, version: int | None) -> dict | None:
        if version is None:
            if not self._reports:
                return None
            version = max(self._reports)
        row = self._reports.get(version)
        return None if row is None else dict(row)

    async def list_report_versions(self) -> list[dict]:
        return [
            {
                "version": v,
                "digest": r["digest"],
                "prev_digest": r["prev_digest"],
                "created_from_ts": r["created_from_ts"],
            }
            for v, r in sorted(self._reports.items())
        ]

    async def reset(self) -> None:
        self._events.clear()
        self._reports.clear()


class PgRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def init_schema(self) -> None:
        async with self.s.bind.begin() as conn:  # type: ignore[union-attr]
            await conn.run_sync(Base.metadata.create_all)

    async def get_existing_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        result = await self.s.execute(
            select(EventRow.event_id).where(EventRow.event_id.in_(ids))
        )
        return {row[0] for row in result.all()}

    async def get_max_ts(self) -> int | None:
        result = await self.s.execute(select(func.max(EventRow.ts)))
        return result.scalar_one_or_none()

    async def insert_events(
        self, rows: list[dict], *, late: bool, replay_version: int
    ) -> list[dict]:
        saved: list[dict] = []
        for r in rows:
            self.s.add(
                EventRow(
                    event_id=r["event_id"],
                    ts=r["ts"],
                    type=r["type"],
                    payload=r["payload"],
                    sig=r.get("sig"),
                    late=late,
                    replay_version_after=replay_version,
                )
            )
        await self.s.flush()  # 一次性分配 seq / 检测唯一冲突
        # 同一事务内按 event_id 回读 seq（一次查询）
        ids = [r["event_id"] for r in rows]
        result = await self.s.execute(
            select(EventRow).where(EventRow.event_id.in_(ids))
        )
        by_id = {obj.event_id: obj for obj in result.scalars().all()}
        for r in rows:
            obj = by_id[r["event_id"]]
            saved.append(
                {
                    "seq": obj.seq,
                    "event_id": obj.event_id,
                    "ts": obj.ts,
                    "type": obj.type,
                    "payload": obj.payload,
                    "sig": obj.sig,
                    "late": obj.late,
                    "replay_version_after": obj.replay_version_after,
                }
            )
        await self.s.commit()
        return saved

    async def list_events(self) -> list[dict]:
        result = await self.s.execute(select(EventRow).order_by(EventRow.seq))
        return [
            {
                "seq": r.seq,
                "event_id": r.event_id,
                "ts": r.ts,
                "type": r.type,
                "payload": r.payload,
                "sig": r.sig,
                "late": r.late,
                "replay_version_after": r.replay_version_after,
            }
            for r in result.scalars().all()
        ]

    async def save_report(
        self,
        *,
        digest: str,
        prev_digest: str | None,
        body: dict,
        signature: str,
        created_from_ts: int,
    ) -> int:
        row = ReportRow(
            digest=digest,
            prev_digest=prev_digest,
            body=body,
            signature=signature,
            created_from_ts=created_from_ts,
        )
        self.s.add(row)
        await self.s.flush()
        version = row.version
        await self.s.commit()
        return version

    async def get_report(self, version: int | None) -> dict | None:
        stmt = select(ReportRow)
        if version is None:
            stmt = stmt.order_by(desc(ReportRow.version)).limit(1)
        else:
            stmt = stmt.where(ReportRow.version == version)
        r = (await self.s.execute(stmt)).scalar_one_or_none()
        if r is None:
            return None
        return {
            "version": r.version,
            "digest": r.digest,
            "prev_digest": r.prev_digest,
            "body": r.body,
            "signature": r.signature,
            "created_from_ts": r.created_from_ts,
        }

    async def list_report_versions(self) -> list[dict]:
        result = await self.s.execute(
            select(
                ReportRow.version,
                ReportRow.digest,
                ReportRow.prev_digest,
                ReportRow.created_from_ts,
            ).order_by(ReportRow.version)
        )
        return [
            {
                "version": v,
                "digest": d,
                "prev_digest": pd,
                "created_from_ts": cts,
            }
            for v, d, pd, cts in result.all()
        ]

    async def reset(self) -> None:
        from sqlalchemy import text

        await self.s.execute(ReportRow.__table__.delete())
        await self.s.execute(EventRow.__table__.delete())
        # 重置自增序列，使全新一轮的 seq/version 从 1 开始
        await self.s.execute(text("ALTER SEQUENCE reports_version_seq RESTART WITH 1"))
        await self.s.execute(text("ALTER SEQUENCE events_seq_seq RESTART WITH 1"))
        await self.s.commit()
