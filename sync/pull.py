"""Incremental pulls over the stable ``RecordChange`` stream.

Cursor contract
---------------
* Opaque to clients (base64 of the BIGINT change id).
* Semantics are *id >= cursor* on first call (cursor omitted/0 means "all")
  and *id > last_seen* afterwards, materialized as the next page's filter.
* AUTO_INCREMENT BIGINT ids never repeat and always increase; a write that
  lands while the client is paging simply appears on a later page, so entries
  can be neither skipped nor duplicated by a mid-pull write.
* The cursor survives restarts -- it lives entirely in the client.
"""
import base64

from .models import RecordChange

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500


class InvalidCursor(Exception):
    pass


def encode_cursor(change_id: int) -> str:
    return base64.urlsafe_b64encode(str(int(change_id)).encode("ascii")).decode("ascii")


def decode_cursor(cursor: str):
    if cursor in (None, ""):
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        value = int(raw)
    except (ValueError, UnicodeDecodeError):
        raise InvalidCursor("cursor is malformed")
    if value < 0:
        raise InvalidCursor("cursor is malformed")
    return value


def pull_changes(*, allowed_project_ids, allowed_crew_ids, after_cursor,
                 limit=DEFAULT_PAGE_LIMIT, project_id=None, template_id=None,
                 include_all=False):
    """Return one page of changes visible to the caller.

    ``after_cursor`` is the id of the last change the client already has;
    the page strictly follows it (id > after_cursor). Fetch one extra row to
    decide whether another page exists without a second COUNT query.
    """
    if not include_all:
        queryset = RecordChange.objects.filter(
            project_id__in=allowed_project_ids,
            crew_id__in=allowed_crew_ids,
        )
    else:
        queryset = RecordChange.objects.all()

    queryset = queryset.select_related(
        "revision", "revision__template_version", "record", "template"
    )
    queryset = queryset.filter(id__gt=after_cursor)
    if project_id is not None:
        queryset = queryset.filter(project_id=project_id)
    if template_id is not None:
        queryset = queryset.filter(template_id=template_id)

    queryset = queryset.order_by("id")[: limit + 1]
    rows = list(queryset)

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].id) if page else encode_cursor(after_cursor)

    return {
        "changes": [serialize_change(row) for row in page],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def serialize_change(row: RecordChange) -> dict:
    revision = row.revision
    return {
        "cursor_id": row.id,
        "record_uuid": str(row.record.record_uuid),
        "change_type": row.change_type,
        "record_status": row.record_status,
        "deleted": row.deleted,
        "project_id": row.project_id,
        "crew_id": row.crew_id,
        "template_id": row.template_id,
        "record_version": revision.seq if revision else None,
        "template_version": revision.template_version.version if revision else None,
        "data": revision.data if revision else None,
        "collected_at": revision.collected_at.isoformat() if revision and revision.collected_at else None,
        "changed_at": row.created_at.isoformat(),
    }
