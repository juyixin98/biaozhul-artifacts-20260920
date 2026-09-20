from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_actor
from app.db import get_session
from app.engine import decide
from app.errors import bad_request
from app.idempotency import Replayed, begin_idempotent, store_idempotent_result
from app.schemas import DecisionIn, InstanceDetailOut
from app.serializers import instance_out, load_history

router = APIRouter(tags=["decisions"])

REQUEST_ID_HEADER = "X-Request-Id"


@router.post(
    "/instances/{instance_id}/decision",
    response_model=InstanceDetailOut,
)
async def make_decision(
    instance_id: int,
    body: DecisionIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    """Approve or reject a pending task.

    Requires ``X-Request-Id`` (idempotency) and ``X-User-Id`` (acting approver;
    body.actor is accepted when the header is absent).  ``expected_version``
    defends against stale clients and produces 409 without writing history.
    """
    actor = get_actor(x_user_id or body.actor)

    request_id = request.headers.get(REQUEST_ID_HEADER)
    if not request_id:
        raise bad_request(
            "missing_request_id",
            "X-Request-Id header is required for approve/reject",
        )

    try:
        await begin_idempotent(session, request_id, body.action, instance_id)
        instance, _tasks = await decide(
            session,
            instance_id=instance_id,
            expected_version=body.expected_version,
            actor=actor,
            action=body.action,
            comment=body.comment,
        )
        history = await load_history(session, instance.id)
        out = instance_out(instance, include_history=True, history=history)
        await store_idempotent_result(
            session, request_id, body.action, instance_id, 200,
            out.model_dump(mode="json"),
        )
        await session.commit()
        return out
    except Replayed as replay:
        return InstanceDetailOut.model_validate(replay.body)
