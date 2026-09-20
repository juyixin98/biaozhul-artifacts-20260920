from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.engine import start_instance, withdraw
from app.errors import conflict, not_found
from app.idempotency import Replayed, begin_idempotent, store_idempotent_result
from app.models import Instance
from app.schemas import (
    InstanceDetailOut,
    InstanceOut,
    StartInstanceIn,
    WithdrawIn,
)
from app.serializers import instance_out, load_history
from app.api.deps import get_actor

router = APIRouter(tags=["instances"])

REQUEST_ID_HEADER = "X-Request-Id"


@router.post("/instances", response_model=InstanceDetailOut, status_code=201)
async def create_instance(
    body: StartInstanceIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    submitter = body.submitter or x_user_id or "system"
    request_id = request.headers.get(REQUEST_ID_HEADER)
    method = f"start:{body.template_code}"
    try:
        if request_id:
            await begin_idempotent(session, request_id, method, instance_id=None)
        instance = await start_instance(
            session,
            template_code=body.template_code,
            requested_version=body.template_version,
            business_key=body.business_key,
            variables=body.variables,
            submitter=submitter,
        )
        history = await load_history(session, instance.id)
        out = instance_out(instance, include_history=True, history=history)
        if request_id:
            await store_idempotent_result(
                session, request_id, method, instance.id, 201,
                out.model_dump(mode="json"),
            )
        await session.commit()
        return out
    except Replayed as replay:
        return InstanceDetailOut.model_validate(replay.body)
    except IntegrityError as exc:
        await session.rollback()
        raise conflict(
            "duplicate_business_key",
            "an instance with this template_code and business_key already exists",
        ) from exc


@router.get("/instances/{instance_id}", response_model=InstanceDetailOut)
async def get_instance(
    instance_id: int,
    session: AsyncSession = Depends(get_session),
):
    instance = await session.scalar(
        select(Instance)
        .where(Instance.id == instance_id)
        .options(selectinload(Instance.tasks))
    )
    if instance is None:
        raise not_found(f"instance {instance_id} not found")
    history = await load_history(session, instance_id)
    return instance_out(instance, include_history=True, history=history)


@router.post("/instances/{instance_id}/withdraw", response_model=InstanceOut)
async def withdraw_instance(
    instance_id: int,
    body: WithdrawIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_user_id: str = Header(alias="X-User-Id"),
):
    actor = get_actor(x_user_id)
    request_id = request.headers.get(REQUEST_ID_HEADER)
    try:
        if request_id:
            await begin_idempotent(session, request_id, "withdraw", instance_id)
        instance = await withdraw(session, instance_id, actor, body.comment)
        out = instance_out(instance)
        if request_id:
            await store_idempotent_result(
                session, request_id, "withdraw", instance_id, 200,
                out.model_dump(mode="json"),
            )
        await session.commit()
        return out
    except Replayed as replay:
        return InstanceOut.model_validate(replay.body)
