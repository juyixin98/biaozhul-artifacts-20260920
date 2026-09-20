"""模板与版本接口。"""

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from workflow_engine.database import get_session
from workflow_engine.models import Template, TemplateVersion
from workflow_engine.schemas import (
    CreateTemplateRequest,
    CreateVersionRequest,
    Message,
    PublishRequest,
    TemplateSummary,
    VersionDetail,
    VersionOut,
)
from workflow_engine.services import templates as tpl_service

router = APIRouter(prefix="/templates", tags=["templates"])


def _version_out(tv: TemplateVersion, *, with_definition: bool = False) -> dict:
    data = {
        "id": tv.id,
        "template_id": tv.template_id,
        "version": tv.version,
        "status": tv.status,
        "checksum": tv.checksum,
        "published_at": tv.published_at,
        "created_at": tv.created_at,
    }
    if with_definition:
        data["definition"] = tv.definition
    return data


def _template_out(t: Template) -> dict:
    return {
        "id": t.id,
        "key": t.key,
        "name": t.name,
        "current_version_id": t.current_version_id,
        "current_version_number": t.current_version_number,
        "created_at": t.created_at,
    }


@router.get("", response_model=list[TemplateSummary])
def list_templates(session: Session = Depends(get_session)):
    rows = session.scalars(select(Template).order_by(Template.id)).all()
    return [_template_out(t) for t in rows]


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
def create_template(body: CreateTemplateRequest, session: Session = Depends(get_session)):
    definition = body.definition.model_dump()
    with session.begin():
        template, tv = tpl_service.create_template(session, definition, publish=body.publish)
    return {"template": _template_out(template), "version": _version_out(tv, with_definition=False)}


@router.get("/{key}", response_model=TemplateSummary)
def get_template(key: str, session: Session = Depends(get_session)):
    return _template_out(tpl_service.get_template(session, key))


@router.get("/{key}/versions", response_model=list[VersionOut])
def list_versions(key: str, session: Session = Depends(get_session)):
    return [_version_out(tv) for tv in tpl_service.list_versions(session, key)]


@router.get("/{key}/versions/{version}", response_model=VersionDetail)
def get_version(key: str, version: int, session: Session = Depends(get_session)):
    template = tpl_service.get_template(session, key)
    tv = session.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id, TemplateVersion.version == version
        )
    )
    if tv is None:
        from workflow_engine.errors import NotFoundError

        raise NotFoundError(f"版本不存在: {key}@v{version}", code="version_not_found")
    return _version_out(tv, with_definition=True)


@router.post("/{key}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED)
def create_version(key: str, body: CreateVersionRequest, session: Session = Depends(get_session)):
    definition = body.definition.model_dump()
    with session.begin():
        tv = tpl_service.create_version(session, key, definition, publish=body.publish)
        data = _version_out(tv)
    return data


@router.post("/{key}/versions/{version}/publish", response_model=VersionOut)
def publish_version(key: str, version: int, body: PublishRequest, session: Session = Depends(get_session)):
    with session.begin():
        tv = tpl_service.publish_version(session, key, version)
        data = _version_out(tv)
    return data


@router.post("/{key}/rollback/{version}", response_model=VersionOut)
def rollback(key: str, version: int, session: Session = Depends(get_session)):
    """把当前版本指针回滚到任一已发布版本；只影响之后发起的实例。"""
    with session.begin():
        tv = tpl_service.rollback_template(session, key, version)
        data = _version_out(tv)
    return data
