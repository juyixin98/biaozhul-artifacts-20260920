"""模板与版本服务。

版本规则：
- create + publish 生成首个已发布版本（version=1）；
- 模板版本发布后不可变（published_at 非空后任何接口都不提供修改入口）；
- 修改模板 = 基于新定义创建新版本；回滚 = 把 current 指针指回旧版本；
- 指针变化只影响之后创建的实例，运行中实例的 version_id 永不改变。
"""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workflow_engine.constants import TemplateStatus
from workflow_engine.errors import ConflictError, NotFoundError, ValidationError
from workflow_engine.models import Instance, Template, TemplateVersion
from workflow_engine.validator import validate_definition


def _checksum(definition: dict) -> str:
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def create_template(session: Session, definition: dict, *, publish: bool) -> tuple[Template, TemplateVersion]:
    key = definition["key"]
    existing = session.scalar(select(Template).where(Template.key == key))
    if existing is not None:
        raise ConflictError(f"模板 key 已存在: {key}", code="template_key_exists")

    if publish:
        _validate_or_raise(definition)

    template = Template(key=key, name=definition["name"])
    session.add(template)
    session.flush()

    version = TemplateVersion(
        template_id=template.id,
        version=1,
        status=TemplateStatus.PUBLISHED if publish else TemplateStatus.DRAFT,
        definition=definition,
        checksum=_checksum(definition),
        published_at=datetime.now(timezone.utc) if publish else None,
    )
    session.add(version)
    session.flush()

    if publish:
        template.current_version_id = version.id
        template.current_version_number = 1
    return template, version


def create_version(
    session: Session, template_key: str, definition: dict, *, publish: bool
) -> TemplateVersion:
    template = session.scalar(select(Template).where(Template.key == template_key))
    if template is None:
        raise NotFoundError(f"模板不存在: {template_key}", code="template_not_found")

    # key/name 沿用同一模板；定义中的 key 必须与模板 key 一致
    if definition["key"] != template.key:
        raise ConflictError("新版本定义的 key 必须与模板 key 一致", code="key_mismatch")

    if publish:
        _validate_or_raise(definition)

    # 锁模板行，并发发布串行化版本号分配
    session.get(Template, template.id, with_for_update=True)
    max_version = session.scalar(
        select(func.max(TemplateVersion.version)).where(TemplateVersion.template_id == template.id)
    )
    version_no = (max_version or 0) + 1
    version = TemplateVersion(
        template_id=template.id,
        version=version_no,
        status=TemplateStatus.PUBLISHED if publish else TemplateStatus.DRAFT,
        definition=definition,
        checksum=_checksum(definition),
        published_at=datetime.now(timezone.utc) if publish else None,
    )
    session.add(version)
    session.flush()

    if publish:
        template.name = definition["name"]
        template.current_version_id = version.id
        template.current_version_number = version_no
    return version


def publish_version(session: Session, template_key: str, version: int) -> TemplateVersion:
    template = session.scalar(select(Template).where(Template.key == template_key))
    if template is None:
        raise NotFoundError(f"模板不存在: {template_key}", code="template_not_found")
    session.get(Template, template.id, with_for_update=True)

    tv = session.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id, TemplateVersion.version == version
        )
    )
    if tv is None:
        raise NotFoundError(f"版本不存在: {template_key}@v{version}", code="version_not_found")
    if tv.status == TemplateStatus.PUBLISHED:
        raise ConflictError("该版本已发布", code="version_already_published")

    _validate_or_raise(tv.definition)
    tv.status = TemplateStatus.PUBLISHED
    tv.published_at = datetime.now(timezone.utc)
    template.name = tv.definition["name"]
    template.current_version_id = tv.id
    template.current_version_number = tv.version
    return tv


def rollback_template(session: Session, template_key: str, target_version: int) -> TemplateVersion:
    """回滚当前指针到任一已发布版本（不改写任何版本定义）。"""
    template = session.scalar(select(Template).where(Template.key == template_key))
    if template is None:
        raise NotFoundError(f"模板不存在: {template_key}", code="template_not_found")
    session.get(Template, template.id, with_for_update=True)

    tv = session.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id,
            TemplateVersion.version == target_version,
            TemplateVersion.status == TemplateStatus.PUBLISHED,
        )
    )
    if tv is None:
        raise NotFoundError(
            f"已发布版本不存在: {template_key}@v{target_version}", code="version_not_found"
        )
    template.current_version_id = tv.id
    template.current_version_number = tv.version
    return tv


def list_versions(session: Session, template_key: str) -> list[TemplateVersion]:
    template = session.scalar(select(Template).where(Template.key == template_key))
    if template is None:
        raise NotFoundError(f"模板不存在: {template_key}", code="template_not_found")
    return list(
        session.scalars(
            select(TemplateVersion)
            .where(TemplateVersion.template_id == template.id)
            .order_by(TemplateVersion.version.desc())
        )
    )


def get_template(session: Session, template_key: str) -> Template:
    template = session.scalar(select(Template).where(Template.key == template_key))
    if template is None:
        raise NotFoundError(f"模板不存在: {template_key}", code="template_not_found")
    return template


def resolve_version_for_start(
    session: Session, template_key: str, version: int | None
) -> tuple[Template, TemplateVersion]:
    template = get_template(session, template_key)
    if version is None:
        if template.current_version_id is None:
            raise ConflictError("模板还没有已发布版本", code="no_published_version")
        tv = session.get(TemplateVersion, template.current_version_id)
    else:
        tv = session.scalar(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template.id, TemplateVersion.version == version
            )
        )
        if tv is None:
            raise NotFoundError(f"版本不存在: {template_key}@v{version}", code="version_not_found")
    if tv.status != TemplateStatus.PUBLISHED:
        raise ConflictError("只能基于已发布版本发起流程", code="version_not_published")
    return template, tv


def get_instance_definition(session: Session, instance: Instance) -> dict:
    """运行时定义加载：只取实例绑定的版本，模板当前指针如何变化都不受影响。"""
    tv = session.get(TemplateVersion, instance.version_id)
    if tv is None:  # 数据库被外部破坏时的防御
        raise NotFoundError("实例绑定的模板版本不存在", code="version_not_found")
    return tv.definition


def _validate_or_raise(definition: dict) -> None:
    issues = validate_definition(definition)
    if issues:
        raise ValidationError(
            f"模板定义校验失败，共 {len(issues)} 个问题",
            extra={"issues": [i.to_dict() for i in issues]},
        )
