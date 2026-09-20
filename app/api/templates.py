from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.engine import publish_template, rollback_template
from app.errors import bad_request, not_found
from app.idempotency import Replayed, begin_idempotent, store_idempotent_result
from app.models import TemplateVersion
from app.schemas import PublishTemplateIn, TemplateVersionOut
from app.serializers import template_out
from app.validation import validate_definition

router = APIRouter(prefix="/templates", tags=["templates"])

REQUEST_ID_HEADER = "X-Request-Id"


@router.post("/{template_code}/versions", response_model=TemplateVersionOut,
             status_code=201)
async def publish(
    template_code: str,
    body: PublishTemplateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_user_id: str = Header(default="system", alias="X-User-Id"),
):
    # Validate before taking the idempotency path: malformed definitions are
    # rejected without consuming a request id.
    issues = validate_definition(body.definition)
    if issues:
        raise bad_request(
            "invalid_template",
            "; ".join(f"[{i.code}] {i.message}" for i in issues),
        )

    request_id = request.headers.get(REQUEST_ID_HEADER)
    method = f"publish:{template_code}"
    try:
        if request_id:
            await begin_idempotent(session, request_id, method, instance_id=None)
        record = await publish_template(
            session, template_code, body.name, body.definition, x_user_id
        )
        out = template_out(record)
        if request_id:
            await store_idempotent_result(
                session, request_id, method, None, 201, out.model_dump(
                    mode="json"
                )
            )
        await session.commit()
        return out
    except Replayed as replay:
        return TemplateVersionOut.model_validate(replay.body)


@router.get("/{template_code}/versions", response_model=list[TemplateVersionOut])
async def list_versions(
    template_code: str,
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(TemplateVersion)
        .where(TemplateVersion.template_code == template_code)
        .order_by(TemplateVersion.version)
    )
    return [template_out(r) for r in rows]


@router.get("/{template_code}/versions/{version}",
            response_model=TemplateVersionOut)
async def get_version(
    template_code: str,
    version: int,
    session: AsyncSession = Depends(get_session),
):
    record = await session.get(TemplateVersion, (template_code, version))
    if record is None:
        raise not_found(f"template {template_code} version {version} not found")
    return template_out(record)


@router.post("/{template_code}/rollback/{target_version}",
             response_model=TemplateVersionOut, status_code=201)
async def rollback(
    template_code: str,
    target_version: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_user_id: str = Header(default="system", alias="X-User-Id"),
):
    request_id = request.headers.get(REQUEST_ID_HEADER)
    method = f"rollback:{template_code}"
    try:
        if request_id:
            await begin_idempotent(session, request_id, method, instance_id=None)
        record = await rollback_template(
            session, template_code, target_version, x_user_id
        )
        out = template_out(record)
        if request_id:
            await store_idempotent_result(
                session, request_id, method, None, 201,
                out.model_dump(mode="json"),
            )
        await session.commit()
        return out
    except Replayed as replay:
        return TemplateVersionOut.model_validate(replay.body)
