"""Ingestion and streaming detection rules.

Every rule creates alerts through a unique ``dedup_key`` so that concurrent or
replayed ingestion, and out-of-order late events, never produce duplicate
alerts. The download-burst rule recomputes the whole affected region per user
(order-independent); the off-hours and USB rules key off stable event
identities.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Alert, AlertRule, Device, Event, EventType, Organization, User
from .hashes import content_hash
from .timeutil import is_outside_work_hours

settings = get_settings()


def insert_alert_if_absent(
    db: Session,
    *,
    rule: AlertRule,
    dedup_key: str,
    title: str,
    user_id: int,
    severity: str,
    evidence: dict,
    device_id: int | None = None,
    window_start=None,
    window_end=None,
    baseline_id: int | None = None,
) -> int | None:
    """Insert an alert unless its dedup_key already exists. Returns new id or None."""
    stmt = (
        pg_insert(Alert.__table__)
        .values(
            rule=rule.value,
            status="open",
            severity=severity,
            user_id=user_id,
            device_id=device_id,
            window_start=window_start,
            window_end=window_end,
            dedup_key=dedup_key,
            title=title,
            evidence=evidence,
            baseline_id=baseline_id,
            version=1,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Alert.__table__.c.id)
    )
    row = db.execute(stmt).first()
    return int(row[0]) if row else None


def _bulk_insert_events(db: Session, rows: list[dict]) -> int:
    """Multi-row insert, returning the number of rows genuinely created.

    Concurrent identical batches both execute an INSERT, but only one can win
    the unique (device_id, event_id) constraint. The loser takes the
    DO UPDATE path; for those rows xmax is set to the current xid, whereas
    freshly inserted rows keep xmax = 0. Parameters are bound (never
    string-interpolated), so payloads cannot inject SQL.
    """
    if not rows:
        return 0
    # Single multi-row INSERT with numbered bind parameters. A list-of-dicts
    # execute() becomes executemany, which psycopg3 will not let RETURNING
    # through; expanding the values list inline keeps one statement and one
    # result set. All values remain bound parameters.
    cols = ["device_id", "user_id", "event_id", "type", "occurred_at", "payload", "content_hash"]
    fragments = []
    params: dict = {}
    for i, r in enumerate(rows):
        names = [f"{c}_{i}" for c in cols]
        fragments.append("(" + ", ".join(f":{n}" for n in names) + ")")
        for c, n in zip(cols, names):
            params[n] = r[c]
    sql = text(
        f"""
        INSERT INTO events
            ({', '.join(cols)})
        VALUES {', '.join(fragments)}
        ON CONFLICT (device_id, event_id) DO UPDATE
            SET event_id = EXCLUDED.event_id
        RETURNING (xmax = 0) AS inserted
        """
    )
    # psycopg3 needs an explicit JSON wrapper for dict parameters.
    from psycopg.types.json import Jsonb as _PgJsonb

    for i in range(len(rows)):
        params[f"payload_{i}"] = _PgJsonb(rows[i]["payload"] or {})
    result = db.execute(sql, params)
    return sum(1 for r in result if r[0])


def _load_context(db: Session, device_keys: list[str]):
    devices = db.scalars(select(Device).where(Device.device_key.in_(device_keys))).all()
    device_map = {d.device_key: d for d in devices}
    user_ids = {d.user_id for d in device_map.values()}
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()} if user_ids else {}
    org_ids = {u.org_id for u in users.values() if u.org_id is not None}
    orgs = {o.id: o for o in db.scalars(select(Organization).where(Organization.id.in_(org_ids))).all()} if org_ids else {}
    return device_map, users, orgs


def _detect_off_hours(db, batch, device_map, users, orgs) -> list[int]:
    ids: list[int] = []
    for ev in batch:
        device = device_map[ev["device_key"]]
        user = users[device.user_id]
        tz_name = orgs[user.org_id].timezone if user.org_id and user.org_id in orgs else "UTC"
        if is_outside_work_hours(
            ev["occurred_at"], tz_name, settings.workday_start_hour, settings.workday_end_hour
        ):
            from .timeutil import local_time

            local = local_time(ev["occurred_at"], tz_name)
            new_id = insert_alert_if_absent(
                db,
                rule=AlertRule.off_hours_access,
                dedup_key=f"off_hours:{device.id}:{ev['event_id']}",
                title=f"After-hours activity ({ev['type'].value}) at {local.strftime('%Y-%m-%d %H:%M')} {tz_name}",
                user_id=user.id,
                device_id=device.id,
                severity="low",
                window_start=ev["occurred_at"],
                window_end=ev["occurred_at"],
                evidence={
                    "event_id": ev["event_id"],
                    "event_type": ev["type"].value,
                    "occurred_at_utc": ev["occurred_at"].isoformat(),
                    "local_time": local.isoformat(),
                    "timezone": tz_name,
                    "allowed_hours": f"[{settings.workday_start_hour:02d}:00,{settings.workday_end_hour:02d}:00)",
                },
            )
            if new_id:
                ids.append(new_id)
    return ids


def _detect_usb_first(db, user_ids: set[int]) -> list[int]:
    ids: list[int] = []
    for uid in user_ids:
        first = db.execute(
            select(Event.event_id, Event.device_id, Event.occurred_at)
            .where(Event.user_id == uid, Event.type == EventType.usb_connect)
            .order_by(Event.occurred_at, Event.id)
            .limit(1)
        ).first()
        if first is None:
            continue
        event_id, device_id, occurred_at = first
        evidence = {
            "first_event_id": event_id,
            "occurred_at": occurred_at.isoformat(),
            "reason": "Earliest usb_connect event on record for this employee",
        }
        new_id = insert_alert_if_absent(
            db,
            rule=AlertRule.usb_first,
            dedup_key=f"usb_first:{uid}",
            title="First USB device connection observed",
            user_id=uid,
            device_id=device_id,
            severity="medium",
            window_start=occurred_at,
            window_end=occurred_at,
            evidence=evidence,
        )
        if new_id:
            ids.append(new_id)
        else:
            # An earlier USB event may have arrived out of order: retarget the
            # existing alert at the now-earliest event (still one alert/user).
            db.execute(
                update(Alert)
                .where(Alert.dedup_key == f"usb_first:{uid}")
                .values(
                    device_id=device_id,
                    window_start=occurred_at,
                    window_end=occurred_at,
                    evidence=evidence,
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
    return ids


def _detect_download_bursts(db, batch, affected_users: set[int]) -> list[int]:
    """Maintain one alert per connected burst cluster per user.

    Half-open sliding window (t - 10min, t]: the current moment is included,
    the exact boundary 10 minutes earlier is excluded. A burst starts at the
    first event whose window count crosses the threshold. The full affected
    region is recomputed every batch, so the result is independent of
    ingestion order. Overlapping runs (including ones whose start first
    appears via late out-of-order events) are merged into the existing
    alert's window rather than creating a duplicate.
    """
    ids: list[int] = []
    window = timedelta(minutes=settings.download_window_minutes)

    per_user_new: dict[int, list] = defaultdict(list)
    for ev in batch:
        if ev["type"] is EventType.file_download:
            per_user_new[ev["_device"].user_id].append(ev["occurred_at"])

    for uid, new_times in per_user_new.items():
        if uid not in affected_users:
            continue
        lo = min(new_times) - window
        hi = max(max(new_times), min(new_times) + window)
        rows = db.execute(
            select(Event.id, Event.device_id, Event.event_id, Event.occurred_at)
            .where(
                Event.user_id == uid,
                Event.type == EventType.file_download,
                Event.occurred_at >= lo,
                Event.occurred_at <= hi,
            )
            .order_by(Event.occurred_at, Event.id)
        ).all()

        # Run starts: i's window contains >threshold events but strictly
        # earlier events in that window are <=threshold.
        run_starts = []
        left = 0
        for i, row in enumerate(rows):
            while rows[left].occurred_at <= row.occurred_at - window:
                left += 1
            count = i - left + 1
            predecessors = i - left
            if count > settings.download_threshold and predecessors <= settings.download_threshold:
                run_starts.append((row, count, row.occurred_at - window))

        if not run_starts:
            continue

        existing = db.scalars(
            select(Alert)
            .where(
                Alert.user_id == uid,
                Alert.rule == AlertRule.download_burst,
                Alert.window_end >= lo,
                Alert.window_start <= hi,
            )
            .order_by(Alert.window_start)
        ).all()

        def count_in(start, end):
            return sum(1 for r in rows if start < r.occurred_at <= end)

        def members_in(start, end):
            return [r.event_id for r in rows if start < r.occurred_at <= end]

        for run_row, run_count, run_start in run_starts:
            run_end = run_row.occurred_at
            overlap = next(
                (a for a in existing
                 if a.window_start is not None and a.window_end is not None
                 and a.window_start < run_end and run_start < a.window_end),
                None,
            )
            if overlap is not None:
                # Same dense activity cluster seen from a newly revealed run:
                # extend the existing alert, never create a duplicate.
                merged_start = min(overlap.window_start, run_start)
                merged_end = max(overlap.window_end, run_end)
                merged_count = count_in(merged_start, merged_end)
                overlap.window_start = merged_start
                overlap.window_end = merged_end
                overlap.evidence = {
                    **(overlap.evidence or {}),
                    "window_start_exclusive": merged_start.isoformat(),
                    "window_end_inclusive": merged_end.isoformat(),
                    "window_minutes": settings.download_window_minutes,
                    "threshold": settings.download_threshold,
                    "download_count": merged_count,
                    "merged_run_starts": sorted(
                        set((overlap.evidence or {}).get("merged_run_starts", []))
                        | {run_row.event_id}
                    ),
                }
                continue

            new_id = insert_alert_if_absent(
                db,
                rule=AlertRule.download_burst,
                # Identity is the run-start event itself, stable across replays.
                dedup_key=f"dl_burst:{run_row.device_id}:{run_row.event_id}",
                title=(
                    f"{run_count} file downloads within "
                    f"{settings.download_window_minutes} minutes"
                ),
                user_id=uid,
                device_id=run_row.device_id,
                severity="high",
                window_start=run_start,
                window_end=run_end,
                evidence={
                    "trigger_event_id": run_row.event_id,
                    "window_start_exclusive": run_start.isoformat(),
                    "window_end_inclusive": run_end.isoformat(),
                    "window_minutes": settings.download_window_minutes,
                    "threshold": settings.download_threshold,
                    "download_count": run_count,
                    "download_event_ids": members_in(run_start, run_end)[: settings.download_threshold + 10],
                    "merged_run_starts": [run_row.event_id],
                },
            )
            if new_id:
                ids.append(new_id)
    return ids


def ingest_batch(db: Session, events_in: list) -> dict:
    """Atomic batch ingestion. Raises HTTPException on any invalid item."""
    # 1. Within-batch duplicate keys are rejected (whole batch).
    seen: set[tuple[str, str]] = set()
    duplicates_in_batch: list[tuple[str, str]] = []
    for ev in events_in:
        key = (ev.device_key, ev.event_id)
        if key in seen:
            duplicates_in_batch.append(key)
        seen.add(key)
    if duplicates_in_batch:
        from ..errors import BatchRejected

        raise BatchRejected(
            {
                "error": "duplicate_keys_within_batch",
                "keys": [
                    {"device_key": d, "event_id": e} for d, e in duplicates_in_batch
                ],
            }
        )

    device_keys = list({ev.device_key for ev in events_in})
    device_map, users, orgs = _load_context(db, device_keys)
    unknown = sorted(set(device_keys) - set(device_map))
    if unknown:
        from ..errors import BatchRejected

        raise BatchRejected({"error": "unknown_devices", "device_keys": unknown})

    batch = []
    for ev in events_in:
        device = device_map[ev.device_key]
        batch.append(
            {
                "device_key": ev.device_key,
                "event_id": ev.event_id,
                "type": ev.type,
                "occurred_at": ev.occurred_at,
                "payload": ev.payload,
                "_device": device,
                "content_hash": content_hash(ev.type.value, ev.occurred_at, ev.payload),
            }
        )

    # 2. Pre-check conflicts with stored rows, then bulk insert ON CONFLICT DO NOTHING.
    keys_by_device: dict[int, list[str]] = defaultdict(list)
    for ev in batch:
        keys_by_device[ev["_device"].id].append(ev["event_id"])
    existing_hashes: dict[tuple[int, str], str] = {}
    for device_id, event_ids in keys_by_device.items():
        rows = db.execute(
            select(Event.device_id, Event.event_id, Event.content_hash).where(
                Event.device_id == device_id, Event.event_id.in_(event_ids)
            )
        ).all()
        for r in rows:
            existing_hashes[(r[0], r[1])] = r[2]

    conflicts = []
    for ev in batch:
        stored = existing_hashes.get((ev["_device"].id, ev["event_id"]))
        if stored is not None and stored != ev["content_hash"]:
            conflicts.append({"device_key": ev["device_key"], "event_id": ev["event_id"]})
    if conflicts:
        from ..errors import ConflictError

        raise ConflictError({"error": "event_content_conflict", "events": conflicts})

    # Bulk insert. Whether each row was truly inserted must be decided by the
    # database: two concurrent transactions cannot see each other's uncommitted
    # rows, so a Python-side count over-counts under concurrent retries.
    # ON CONFLICT DO UPDATE (a no-op update) with RETURNING (xmax = 0) yields
    # true for freshly inserted rows and false for pre-existing ones.
    values = [
        {
            "device_id": ev["_device"].id,
            "user_id": ev["_device"].user_id,
            "event_id": ev["event_id"],
            "type": ev["type"].value,
            "occurred_at": ev["occurred_at"],
            "payload": ev["payload"],
            "content_hash": ev["content_hash"],
        }
        for ev in batch
    ]
    inserted = _bulk_insert_events(db, values)

    # Re-read: closes the race where a concurrent transaction inserts the same
    # key with different content between our check and insert.
    re_rows = []
    for device_id, event_ids in keys_by_device.items():
        re_rows.extend(
            db.execute(
                select(Event.device_id, Event.event_id, Event.content_hash).where(
                    Event.device_id == device_id, Event.event_id.in_(event_ids)
                )
            ).all()
        )
    current_hashes = {(r[0], r[1]): r[2] for r in re_rows}
    race_conflict = any(
        current_hashes.get((ev["_device"].id, ev["event_id"])) != ev["content_hash"]
        for ev in batch
    )
    if race_conflict:
        from ..errors import ConflictError

        raise ConflictError({"error": "event_content_conflict", "events": "concurrent"})

    # 3. Detection (same transaction; idempotent via dedup keys).
    alert_ids: list[int] = []
    alert_ids += _detect_off_hours(db, batch, device_map, users, orgs)

    usb_users = {ev["_device"].user_id for ev in batch if ev["type"] is EventType.usb_connect}
    alert_ids += _detect_usb_first(db, usb_users)

    download_users = {ev["_device"].user_id for ev in batch if ev["type"] is EventType.file_download}
    alert_ids += _detect_download_bursts(db, batch, download_users)

    db.flush()
    return {
        "received": len(batch),
        "inserted": inserted,
        "duplicates": len(batch) - inserted,
        "alerts_created": len(alert_ids),
        "alert_ids": alert_ids,
    }
