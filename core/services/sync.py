"""同步领域服务：批量推送（幂等/冲突）与稳定游标增量拉取。

所有条目级结果通过 SyncBatch/SyncItem 持久化，客户端断网重试整批时
原样返回首次结果，保证"同 UUID 同内容重试返回原结果"。
"""
import datetime
import hashlib
import json

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import (
    ChangeLog,
    Conflict,
    FormRecord,
    FormTemplate,
    RecordVersion,
    SyncBatch,
    SyncItem,
    SyncState,
)
from ..template_schema import SubmissionError, validate_submission

# 条目结果码（与 SyncItem.Status 对应）
ITEM_CREATED = "created"
ITEM_UPDATED = "updated"
ITEM_IDEMPOTENT = "idempotent"
ITEM_CONFLICT = "conflict"
ITEM_ERROR = "error"


def content_hash(data):
    """对提交内容做稳定哈希（key 排序、分隔符防注入）。"""
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _render_payload(kind, record, version=None, conflict=None):
    """事件发生时渲染快照；历史读取永远按当时版本。"""
    base = {
        "record_uuid": str(record.uuid),
        "project": record.project_id,
        "template_code": record.template.code,
        "template_version": record.template.version,
        "status": record.status,
    }
    if kind == ChangeLog.Kind.RECORD_DELETED:
        base["deleted"] = True
        return base
    if version is not None:
        base.update(
            {
                "template_version": version.template.version,
                "template_code": version.template.code,
                "data": version.content,
                "client_record_version": version.client_record_version,
                "collected_at": version.collected_at.isoformat(),
                "device_id": version.device_id,
                "record_version_id": version.id,
            }
        )
    if kind == ChangeLog.Kind.CONFLICT_DETECTED:
        base["conflict_id"] = conflict.id if conflict else None
        base["awaiting_resolution"] = True
    if kind == ChangeLog.Kind.CONFLICT_RESOLVED:
        base["conflict_id"] = conflict.id if conflict else None
        base["resolved"] = True
    return base


def emit_change(kind, project, record, *, version=None, conflict=None, payload=None):
    rendered = payload if payload is not None else _render_payload(
        kind, record, version=version, conflict=conflict
    )
    return ChangeLog.objects.create(
        kind=kind,
        project=project,
        record=record,
        record_version=version,
        conflict=conflict,
        payload=rendered,
    )


# --------------------------------------------------------------------------- #
# 推送
# --------------------------------------------------------------------------- #

class ItemResult(dict):
    """单条处理结果（同时被持久化到 SyncItem）。"""


def _result(status, code, message, *, version=None, conflict=None):
    r = ItemResult(status=status, code=code, message=message)
    if version is not None:
        r["record_version_id"] = version.id
    if conflict is not None:
        r["conflict_id"] = conflict.id
    return r


def process_item(user, project, item, now=None):
    """处理批次中的一个条目。每个条目独立事务，互不影响。

    必须在外层调用方持有 SyncBatch 行锁后调用。
    两台设备并发首次提交同一 UUID 时，后到事务会撞
    (project, uuid) 唯一约束，捕获后按"记录已存在"路径重放一次，
    正确落入幂等或冲突分支。
    """
    now = now or timezone.now()
    uuid = item.get("uuid")
    data = item.get("data")
    client_rv = item.get("record_version")
    collected_at = item.get("collected_at")
    device_id = str(item.get("device_id", "") or "")[:128]

    # ---- 基本形态校验（不查库的快速失败） ----
    import uuid as uuidlib

    try:
        parsed_uuid = uuidlib.UUID(str(uuid))
    except (ValueError, AttributeError, TypeError):
        return _result(ITEM_ERROR, "invalid_uuid", "uuid 必须是合法 UUID")
    if not isinstance(data, dict):
        return _result(ITEM_ERROR, "invalid_data", "data 必须是对象")
    if not isinstance(client_rv, int) or isinstance(client_rv, bool) or client_rv < 1:
        return _result(
            ITEM_ERROR, "invalid_record_version", "record_version 必须是 >=1 的整数"
        )
    if collected_at is None:
        return _result(
            ITEM_ERROR, "invalid_collected_at", "必须携带采集时间 collected_at"
        )
    if isinstance(collected_at, str):
        from django.utils.dateparse import parse_datetime

        parsed_dt = parse_datetime(collected_at)
        if parsed_dt is None:
            return _result(
                ITEM_ERROR,
                "invalid_collected_at",
                "collected_at 必须是 ISO 8601 时间字符串",
            )
        collected_at = parsed_dt
    if not isinstance(collected_at, datetime.datetime):
        return _result(
            ITEM_ERROR, "invalid_collected_at", "collected_at 类型非法"
        )
    if timezone.is_naive(collected_at):
        collected_at = timezone.make_aware(collected_at, timezone.utc)

    tpl_code = item.get("template_code")
    tpl_version = item.get("template_version")
    if not tpl_code:
        return _result(
            ITEM_ERROR, "invalid_template_code", "必须携带模板 template_code"
        )
    if (
        not isinstance(tpl_version, int)
        or isinstance(tpl_version, bool)
        or tpl_version < 1
    ):
        return _result(
            ITEM_ERROR, "invalid_template_version", "必须携带 >=1 的整数 template_version"
        )

    for attempt in range(2):
        try:
            with transaction.atomic():
                # 锁定记录行，串行化两台设备对同一 UUID 的并发提交。
                record = (
                    FormRecord.objects.select_for_update()
                    .filter(project=project, uuid=parsed_uuid)
                    .first()
                )

                # ---- 定位模板：允许已弃用版本（旧设备合法提交不能丢） ----
                template = (
                    FormTemplate.objects.select_related("project")
                    .filter(project=project, code=tpl_code, version=tpl_version)
                    .first()
                )
                if template is None:
                    return _result(
                        ITEM_ERROR,
                        "template_not_found",
                        f"模板 {tpl_code} v{tpl_version} 不存在",
                    )
                if template.status == FormTemplate.Status.DRAFT:
                    return _result(
                        ITEM_ERROR,
                        "template_not_published",
                        f"模板 {tpl_code} v{tpl_version} 尚未发布",
                    )

                # ---- 按提交当时的模板版本校验 ----
                try:
                    validate_submission(template, data)
                except SubmissionError as exc:
                    return _validation_error(exc)

                h = content_hash(data)

                if record is None:
                    return _create_record(
                        user, project, template, parsed_uuid, data, h,
                        client_rv, collected_at, device_id,
                    )

                return _process_existing(
                    user, record, template, data, h, client_rv,
                    collected_at, device_id,
                )
        except IntegrityError:
            # 并发首次插入同 UUID：对方已先提交，整轮回放走"已存在"分支。
            if attempt == 0:
                continue
            raise


def _validation_error(exc):
    r = ItemResult(
        status=ITEM_ERROR,
        code="validation_failed",
        message="未通过模板校验",
    )
    r["detail"] = exc.detail
    return r


def _create_record(user, project, template, uuid, data, h, client_rv,
                   collected_at, device_id):
    record = FormRecord.objects.create(
        uuid=uuid,
        project=project,
        template=template,
        status=FormRecord.Status.ACTIVE,
    )
    version = RecordVersion.objects.create(
        record=record,
        template=template,
        content=data,
        content_hash=h,
        client_record_version=client_rv,
        collected_at=collected_at,
        created_by=user,
        device_id=device_id,
        status=RecordVersion.Status.ACCEPTED,
    )
    record.current_version = version
    record.save(update_fields=["current_version", "updated_at"])
    emit_change(
        ChangeLog.Kind.RECORD_UPSERTED, project, record, version=version
    )
    return _result(ITEM_CREATED, "created", "记录已创建", version=version)


def _process_existing(user, record, template, data, h, client_rv,
                      collected_at, device_id):
    """记录已存在：删除拦截 / 幂等重放 / 内容冲突。"""
    if record.status == FormRecord.Status.DELETED:
        return _result(
            ITEM_ERROR,
            "record_deleted",
            "记录已被删除，不能重新提交；如需恢复请联系主管",
        )

    current = record.current_version

    # ---- 幂等：同 UUID 同内容（即使 record_version 标签不同也视为重放） ----
    duplicate = (
        RecordVersion.objects.filter(record=record, content_hash=h)
        .order_by("-id")
        .first()
    )
    if duplicate is not None:
        return _result(
            ITEM_IDEMPOTENT,
            "idempotent_replay",
            "内容未变化，返回首次处理结果",
            version=duplicate,
        )

    # ---- 不同内容：新增候选版本，绝不覆盖已有记录 ----
    candidate = RecordVersion.objects.create(
        record=record,
        template=template,
        content=data,
        content_hash=h,
        client_record_version=client_rv,
        collected_at=collected_at,
        created_by=user,
        device_id=device_id,
        status=RecordVersion.Status.CANDIDATE,
    )

    conflict, created = Conflict.objects.get_or_create(
        record=record,
        status=Conflict.Status.OPEN,
        defaults={"project": record.project},
    )
    conflict.versions.add(candidate)
    if current is not None:
        conflict.versions.add(current)

    record.status = FormRecord.Status.IN_CONFLICT
    record.template = template
    record.save(update_fields=["status", "template_id", "updated_at"])

    emit_change(
        ChangeLog.Kind.CONFLICT_DETECTED,
        record.project,
        record,
        version=candidate,
        conflict=conflict,
    )
    return _result(
        ITEM_CONFLICT,
        "content_conflict",
        "检测到同 UUID 的不同内容：双方版本均已保留，等待主管解决",
        version=candidate,
        conflict=conflict,
    )


def process_batch(user, project, client_batch_id, items, device_id_default=""):
    """处理整批（<=50 条）。整批幂等：同 (user, client_batch_id) 返回原结果。"""
    max_batch = getattr(settings, "SYNC_MAX_BATCH_SIZE", 50)
    if not items:
        return None, {"detail": "items 不能为空"}
    if len(items) > max_batch:
        return None, {"detail": f"每批最多 {max_batch} 条，当前 {len(items)} 条"}

    with transaction.atomic():
        # 锁批处理行，保证同 client_batch_id 的并发重试只有一个真正执行。
        batch = (
            SyncBatch.objects.select_for_update()
            .filter(user=user, client_batch_id=client_batch_id)
            .first()
        )
        if batch is not None:
            if batch.status == SyncBatch.Status.PROCESSING:
                return batch, {
                    "detail": "该批次上次处理中断，请稍后重试或通过 GET 对账"
                }
            return batch, None  # 由调用方回放持久化结果

        batch = SyncBatch.objects.create(
            user=user,
            project=project,
            client_batch_id=client_batch_id,
            status=SyncBatch.Status.PROCESSING,
            item_count=len(items),
        )
        results = []
        for idx, item in enumerate(items):
            if device_id_default and not item.get("device_id"):
                item = {**item, "device_id": device_id_default}
            result = process_item(user, project, item)
            results.append(result)
            rv_id = result.get("record_version_id")
            conflict_id = result.get("conflict_id")
            SyncItem.objects.create(
                batch=batch,
                index=idx,
                uuid=_safe_uuid(item.get("uuid")),
                raw_uuid=str(item.get("uuid") or "")[:64],
                status=result["status"],
                code=result["code"],
                message=result["message"],
                record_version_id=rv_id,
                conflict_id=conflict_id,
            )
        batch.status = SyncBatch.Status.COMPLETED
        batch.save(update_fields=["status"])
        return batch, results


def _safe_uuid(value):
    import uuid as uuidlib

    try:
        return uuidlib.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def batch_results(batch):
    """从持久化的 SyncItem 重建首次处理结果（断网重试/对账）。"""
    out = []
    for item in batch.items.select_related("record_version", "conflict").order_by(
        "index"
    ):
        r = ItemResult(status=item.status, code=item.code, message=item.message)
        if item.raw_uuid:
            r["uuid"] = item.raw_uuid
        elif item.uuid:
            r["uuid"] = str(item.uuid)
        if item.record_version_id:
            r["record_version_id"] = item.record_version_id
        if item.conflict_id:
            r["conflict_id"] = item.conflict_id
        out.append(r)
    return out


# --------------------------------------------------------------------------- #
# 冲突解决（主管显式选择保留版本 + 解决依据）
# --------------------------------------------------------------------------- #

def resolve_conflict(conflict, winning_version, note, user):
    with transaction.atomic():
        conflict = (
            Conflict.objects.select_for_update()
            .filter(id=conflict.id)
            .prefetch_related("versions")
            .get()
        )
        if conflict.status == Conflict.Status.RESOLVED:
            return conflict, {"detail": "冲突已解决，不能重复操作"}
        version_ids = set(conflict.versions.values_list("id", flat=True))
        current = conflict.record.current_version
        if current:
            version_ids.add(current.id)
        if winning_version.id not in version_ids:
            return conflict, {"detail": "只能选择冲突中保留的记录版本"}

        winning_version = RecordVersion.objects.select_for_update().get(
            id=winning_version.id
        )
        record = conflict.record

        # 落选版本标记为 superseded（内容仍保留可审计）。
        RecordVersion.objects.filter(id__in=version_ids).exclude(
            id=winning_version.id
        ).update(status=RecordVersion.Status.SUPERSEDED)
        winning_version.status = RecordVersion.Status.ACCEPTED
        winning_version.save(update_fields=["status"])

        conflict.status = Conflict.Status.RESOLVED
        conflict.winning_version = winning_version
        conflict.resolution_note = note
        conflict.resolved_by = user
        conflict.resolved_at = timezone.now()
        conflict.save()

        record.status = FormRecord.Status.RESOLVED
        record.current_version = winning_version
        record.template = winning_version.template
        record.save(
            update_fields=["status", "current_version", "template_id", "updated_at"]
        )

        emit_change(
            ChangeLog.Kind.CONFLICT_RESOLVED,
            record.project,
            record,
            version=winning_version,
            conflict=conflict,
        )
        return conflict, None


# --------------------------------------------------------------------------- #
# 增量拉取（稳定游标 + 快照高水位，翻页期间新写入不漏）
# --------------------------------------------------------------------------- #

def _current_max_id():
    return ChangeLog.objects.order_by("-id").values_list("id", flat=True).first() or 0


def pull_changes(user, project, *, cursor=None, limit=100, hwm=None, save_state=True):
    """返回某项目的增量变更（基于单调 BIGINT 主键的稳定游标）。

    游标/快照语义
    =============
    * 第一页不传 cursor 时，从该用户保存的 SyncState.cursor 续传
      （服务重启后可继续同步）。
    * 每轮第一页计算快照高水位 hwm = max(ChangeLog.id) - 安全余量。
      余量用于吸收"低 id 事务晚于高 id 事务提交"造成的 id 空洞
      （MySQL InnoDB 自增 ID 在事务开始时分配）。余量大小通过
      SYNC_HWM_ID_GRACE 配置（默认 100，约一批同步的两倍）。
    * 翻页必须携带首页返回的 hwm，整轮读取固定快照 [cursor+1, hwm]：
      翻页期间发生的新写入一律落在 hwm 之后，下一轮同步读取，绝不漏数据。
    * 最后一页 has_more=false 后，可不带 hwm 开启新一轮同步。
    * 测试或确认写入已全部落盘时可传 hwm=max 或设置余量为 0。
    """
    limit = max(1, min(int(limit), 500))

    state = SyncState.objects.filter(user=user, project=project).first()
    if cursor is None:
        cursor = state.cursor if state else 0
    cursor = max(0, int(cursor))

    max_id = _current_max_id()
    grace = int(getattr(settings, "SYNC_HWM_ID_GRACE", 100))
    computed_hwm = max(0, max_id - grace)

    if hwm is None:
        hwm = state.hwm if (state and state.hwm) else computed_hwm
    hwm = max(0, int(hwm))

    qs = ChangeLog.objects.filter(
        project=project, id__gt=cursor, id__lte=hwm
    ).order_by("id")
    total_in_window = qs.count()
    page = list(qs[:limit])

    next_cursor = page[-1].id if page else cursor
    has_more = total_in_window > len(page)

    if save_state:
        SyncState.objects.update_or_create(
            user=user, project=project,
            defaults={
                "cursor": next_cursor,
                "hwm": hwm if has_more else None,
            },
        )

    return {
        "changes": [
            {
                "id": ch.id,
                "kind": ch.kind,
                "record_uuid": str(ch.record.uuid),
                "created_at": ch.created_at.isoformat(),
                "payload": ch.payload,
            }
            for ch in page
        ],
        "cursor": cursor,
        "next_cursor": next_cursor,
        "hwm": hwm,
        "has_more": has_more,
        "window_max_id": max_id,
    }


def mark_record_deleted(user, record):
    """主管对记录打删除标记（软删除），并写墓碑事件供增量拉取。"""
    with transaction.atomic():
        record = FormRecord.objects.select_for_update().get(id=record.id)
        if record.status == FormRecord.Status.DELETED:
            return record, False
        if record.status == FormRecord.Status.IN_CONFLICT:
            return record, "conflict_open"
        record.status = FormRecord.Status.DELETED
        record.deleted_at = timezone.now()
        record.save(update_fields=["status", "deleted_at", "updated_at"])
        emit_change(
            ChangeLog.Kind.RECORD_DELETED,
            record.project,
            record,
            payload=_render_payload(ChangeLog.Kind.RECORD_DELETED, record),
        )
        return record, True
