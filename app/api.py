"""HTTP API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from . import config, crypto, schemas, services
from .database import connect, transaction

router = APIRouter()


# ---------------------------------------------------------------------------
# auth dependencies
# ---------------------------------------------------------------------------

def require_operator(x_auth_token: str | None = Header(default=None)) -> dict:
    if not x_auth_token:
        raise HTTPException(status_code=401,
                            detail="missing X-Auth-Token header")
    try:
        claims = crypto.verify_token(x_auth_token)
    except crypto.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if claims.get("kind") != "operator":
        raise HTTPException(status_code=403, detail="not an operator token")
    return claims


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

@router.post("/auth/register", tags=["auth"], status_code=201)
def register(body: schemas.RegisterIn):
    conn = connect()
    try:
        with transaction(conn):
            existing = services.get_operator_by_username(conn, body.username)
            if existing:
                raise HTTPException(status_code=409,
                                    detail="username already registered")
            oid = services.create_operator(conn, body.username, body.password)
        return {"id": oid, "username": body.username}
    finally:
        conn.close()


@router.post("/auth/login", tags=["auth"], response_model=schemas.TokenOut)
def login(body: schemas.LoginIn):
    conn = connect()
    try:
        row = services.get_operator_by_username(conn, body.username)
    finally:
        conn.close()
    # verify even when user missing to reduce username enumeration timing
    dummy = ("pbkdf2_sha256$1$00$" + "0" * 64)
    encoded = row["password_hash"] if row else dummy
    if not crypto.verify_password(body.password, encoded) or row is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = crypto.issue_token(
        {"sub": str(row["id"]), "kind": "operator", "username": body.username},
        ttl_seconds=config.TOKEN_TTL_SECONDS,
    )
    return {"access_token": token, "expires_in": config.TOKEN_TTL_SECONDS,
            "operator": body.username}


# ---------------------------------------------------------------------------
# robots / chargers / tasks
# ---------------------------------------------------------------------------

@router.get("/robots", tags=["robots"])
def get_robots():
    return services.list_robots()


@router.post("/robots", tags=["robots"], status_code=201,
             dependencies=[Depends(require_operator)])
def post_robot(body: schemas.RobotIn):
    try:
        return services.create_robot(body)
    except Exception as exc:  # unique constraint etc.
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/robots/{robot_id}", tags=["robots"])
def get_robot(robot_id: str):
    robot = services.get_robot(robot_id)
    if robot is None:
        raise HTTPException(status_code=404, detail="robot not found")
    return robot


@router.get("/chargers", tags=["chargers"])
def get_chargers():
    return services.list_chargers()


@router.post("/chargers", tags=["chargers"], status_code=201,
             dependencies=[Depends(require_operator)])
def post_charger(body: schemas.ChargerIn):
    try:
        return services.create_charger(body)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/tasks", tags=["tasks"])
def get_tasks():
    return services.list_tasks()


@router.post("/tasks", tags=["tasks"], status_code=201,
             dependencies=[Depends(require_operator)])
def post_task(body: schemas.TaskIn):
    try:
        return services.create_task(body)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/tasks/preview-cost", tags=["tasks"])
def preview(body: schemas.CostPreviewIn):
    """Deterministic cost/reachability trial without persisting anything."""
    return services.preview_cost(body)


# ---------------------------------------------------------------------------
# dispatch / assignments
# ---------------------------------------------------------------------------

@router.post("/dispatch/assignments/batch", tags=["dispatch"],
             dependencies=[Depends(require_operator)])
def dispatch_batch(task_ids: list[str] | None = Query(default=None)):
    """Global minimum-cost allocation.

    Pass repeated ``task_ids`` query params to limit the run to specific
    pending tasks; without them every pending task participates.
    """
    return services.batch_allocate(
        only_task_ids=set(task_ids) if task_ids else None)


@router.get("/assignments", tags=["dispatch"])
def get_assignments(status: str | None = Query(default=None)):
    return services.list_assignments(status)


@router.post("/assignments/{assignment_id}/start", tags=["dispatch"],
             dependencies=[Depends(require_operator)])
def start(assignment_id: str,
          x_assignment_token: str | None = Header(default=None)):
    if not x_assignment_token:
        raise HTTPException(status_code=401,
                            detail="missing X-Assignment-Token header")
    return services.start_assignment(assignment_id, x_assignment_token)


@router.post("/assignments/{assignment_id}/complete", tags=["dispatch"],
             dependencies=[Depends(require_operator)])
def complete(assignment_id: str,
             x_assignment_token: str | None = Header(default=None)):
    if not x_assignment_token:
        raise HTTPException(status_code=401,
                            detail="missing X-Assignment-Token header")
    return services.complete_assignment(assignment_id, x_assignment_token)


@router.post("/assignments/{assignment_id}/unplug", tags=["dispatch"],
             dependencies=[Depends(require_operator)])
def unplug(assignment_id: str,
           x_assignment_token: str | None = Header(default=None)):
    if not x_assignment_token:
        raise HTTPException(status_code=401,
                            detail="missing X-Assignment-Token header")
    return services.unplug_assignment(assignment_id, x_assignment_token)


@router.post("/assignments/{assignment_id}/cancel", tags=["dispatch"],
             dependencies=[Depends(require_operator)])
def cancel(assignment_id: str):
    return services.cancel_assignment(assignment_id)


@router.post("/assignments/{assignment_id}/telemetry", tags=["dispatch"],
             dependencies=[Depends(require_operator)])
def telemetry(assignment_id: str, body: schemas.TelemetryIn,
              x_assignment_token: str | None = Header(default=None)):
    if not x_assignment_token:
        raise HTTPException(status_code=401,
                            detail="missing X-Assignment-Token header")
    return services.report_telemetry(
        assignment_id, x_assignment_token,
        body.measured_soc_kwh, body.note)


@router.post("/chargers/{charger_id}/failover", tags=["chargers"],
             dependencies=[Depends(require_operator)])
def failover(charger_id: str):
    return services.charger_failover(charger_id)


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------

@router.get("/alerts", tags=["ops"])
def get_alerts(severity: str | None = Query(default=None)):
    return services.list_alerts(severity)


@router.get("/dispatch/logs", tags=["ops"])
def get_logs():
    return services.list_dispatch_logs()
