"""FastAPI 入口：HMAC 签名校验 + 任务分配路由。

除 GET /healthz 外，所有 /api/* 请求必须携带 HMAC-SHA256 签名头（见 README）。
每个成功响应还会带 X-Response-Signature（对原始响应体的 HMAC），客户端可验真。
本服务不向任何真实机器人下发控制命令；分配结果中的指令一律为 simulated。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import config, crypto, database, services
from .schemas import (
    ChargerFailureIn,
    ChargerIn,
    DispatchRequest,
    RobotIn,
    TaskIn,
    TelemetryIn,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="电量可达任务分配后端",
    version="1.0.0",
    description="合成机器人任务分配：显式能耗公式 + 安全余量 + 最小成本匹配。",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 签名校验

class AuthError(Exception):
    def __init__(self, detail: str):
        self.detail = detail


@app.exception_handler(AuthError)
async def _auth_exc_handler(_request: Request, exc: AuthError):
    return JSONResponse(status_code=401, content={"detail": exc.detail})


@app.exception_handler(services.ServiceError)
async def _service_exc_handler(_request: Request, exc: services.ServiceError):
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}
    )


async def require_signature(
    request: Request,
    x_key_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
) -> None:
    """真实执行 HMAC 校验：时间窗 + nonce 防重放 + 常量时间比较。"""
    headers = {
        "X-Key-Id": x_key_id,
        "X-Timestamp": x_timestamp,
        "X-Nonce": x_nonce,
        "X-Signature": x_signature,
    }
    missing = [k for k, v in headers.items() if not v]
    if missing:
        raise AuthError(f"missing signature headers: {', '.join(missing)}")

    if x_key_id != config.API_KEY_ID:
        raise AuthError(f"unknown key id: {x_key_id}")

    try:
        ts = int(x_timestamp)
    except ValueError:
        raise AuthError("X-Timestamp must be a unix epoch integer")
    now = int(time.time())
    if abs(now - ts) > config.TIMESTAMP_WINDOW_S:
        raise AuthError(
            f"timestamp outside {config.TIMESTAMP_WINDOW_S}s window"
        )

    body = await request.body()
    message = crypto.signing_string(
        x_key_id, x_timestamp, x_nonce,
        request.method, request.url.path, body,
    )
    if not crypto.verify_signature(config.API_SECRET, message, x_signature):
        raise AuthError("signature verification failed")

    conn = database.connect()
    try:
        # 清理过期 nonce 并登记本次 nonce（主键冲突即重放）
        conn.execute(
            "DELETE FROM used_nonces WHERE ts < ?",
            (now - config.TIMESTAMP_WINDOW_S,),
        )
        conn.execute(
            "INSERT INTO used_nonces(nonce, ts) VALUES (?, ?)",
            (x_nonce, now),
        )
    except Exception as exc:  # IntegrityError -> replay
        raise AuthError("nonce already used (replay detected)") from exc
    finally:
        conn.close()


@app.middleware("http")
async def sign_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") and response.status_code < 500:
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        response = JSONResponse(
            status_code=response.status_code,
            content=json.loads(body),
            headers={
                "X-Response-Algorithm": "HMAC-SHA256",
                "X-Response-Signature": crypto.sign_text(
                    config.API_SECRET, body.decode("utf-8")
                ),
            },
        )
    return response


# ---------------------------------------------------------------- 健康检查

@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "battery-reachable-dispatch"}


# ---------------------------------------------------------------- 资源注册

@app.post("/api/robots", dependencies=[Depends(require_signature)])
def create_robot(item: RobotIn, conn=Depends(database.get_conn)):
    with database.immediate_tx(conn):
        try:
            conn.execute(
                "INSERT INTO robots(id, x, y, battery_wh, status, updated_at)"
                " VALUES (?,?,?,?,'idle',?)",
                (item.id, item.x, item.y, item.battery_wh, database.utcnow()),
            )
        except Exception as exc:
            raise services.ServiceError(
                409, f"robot {item.id} already exists"
            ) from exc
    return {"id": item.id, "x": item.x, "y": item.y,
            "battery_wh": item.battery_wh, "status": "idle"}


@app.post("/api/chargers", dependencies=[Depends(require_signature)])
def create_charger(item: ChargerIn, conn=Depends(database.get_conn)):
    with database.immediate_tx(conn):
        try:
            conn.execute(
                "INSERT INTO chargers(id, x, y, status)"
                " VALUES (?,?,?,'available')",
                (item.id, item.x, item.y),
            )
        except Exception as exc:
            raise services.ServiceError(
                409, f"charger {item.id} already exists"
            ) from exc
    return {"id": item.id, "x": item.x, "y": item.y,
            "status": "available"}


@app.post("/api/tasks", dependencies=[Depends(require_signature)])
def create_task(item: TaskIn, conn=Depends(database.get_conn)):
    with database.immediate_tx(conn):
        try:
            conn.execute(
                "INSERT INTO tasks(id, x, y, payload_kg, wait_s, status,"
                " created_at) VALUES (?,?,?,?,?,'pending',?)",
                (item.id, item.x, item.y, item.payload_kg, item.wait_s,
                 database.utcnow()),
            )
        except Exception as exc:
            raise services.ServiceError(
                409, f"task {item.id} already exists"
            ) from exc
    return {"id": item.id, "x": item.x, "y": item.y,
            "payload_kg": item.payload_kg, "wait_s": item.wait_s,
            "status": "pending"}


# ---------------------------------------------------------------- 分配

@app.post("/api/dispatch", dependencies=[Depends(require_signature)])
def dispatch(req: DispatchRequest, conn=Depends(database.get_conn)):
    return services.dispatch(
        conn, req.task_ids,
        margin=req.options.safety_margin_wh,
    )


# ---------------------------------------------------------------- 生命周期

@app.post("/api/tasks/{task_id}/start",
          dependencies=[Depends(require_signature)])
def start_task(task_id: str, conn=Depends(database.get_conn)):
    return services.start_task(conn, task_id)


@app.post("/api/tasks/{task_id}/complete",
          dependencies=[Depends(require_signature)])
def complete_task(task_id: str, payload: dict | None = None,
                  conn=Depends(database.get_conn)):
    measured = None
    if payload and "measured_battery_wh" in payload:
        measured = float(payload["measured_battery_wh"])
        if measured <= 0:
            raise services.ServiceError(
                422, "measured_battery_wh must be > 0"
            )
    return services.complete_task(conn, task_id, measured)


@app.post("/api/tasks/{task_id}/cancel",
          dependencies=[Depends(require_signature)])
def cancel_task(task_id: str, payload: dict | None = None,
                conn=Depends(database.get_conn)):
    reason = (payload or {}).get("reason", "cancelled_by_request")
    return services.cancel_reservation(conn, task_id, str(reason))


@app.post("/api/robots/{robot_id}/telemetry",
          dependencies=[Depends(require_signature)])
def telemetry(robot_id: str, item: TelemetryIn,
              conn=Depends(database.get_conn)):
    return services.report_telemetry(
        conn, robot_id, item.battery_wh, item.x, item.y,
        item.mission_completed_distance_m,
    )


# ---------------------------------------------------------------- 充电点失效

@app.post("/api/chargers/{charger_id}/fail",
          dependencies=[Depends(require_signature)])
def fail_charger(charger_id: str, item: ChargerFailureIn,
                 conn=Depends(database.get_conn)):
    return services.fail_charger(conn, charger_id, item.reason)


# ---------------------------------------------------------------- 查询

@app.get("/api/allocations", dependencies=[Depends(require_signature)])
def list_allocations(conn=Depends(database.get_conn)):
    rows = conn.execute(
        "SELECT * FROM allocations ORDER BY id"
    ).fetchall()
    return [database.row_to_dict(r) for r in rows]


@app.get("/api/alerts", dependencies=[Depends(require_signature)])
def list_alerts(severity: str | None = None,
                conn=Depends(database.get_conn)):
    if severity:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE severity=? ORDER BY id", (severity,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY id"
        ).fetchall()
    return [database.row_to_dict(r) for r in rows]


@app.get("/api/snapshot", dependencies=[Depends(require_signature)])
def snapshot(conn=Depends(database.get_conn)):
    return {
        "robots": [database.row_to_dict(r)
                   for r in conn.execute("SELECT * FROM robots ORDER BY id")],
        "chargers": [database.row_to_dict(r)
                     for r in conn.execute(
                         "SELECT * FROM chargers ORDER BY id")],
        "tasks": [database.row_to_dict(r)
                  for r in conn.execute("SELECT * FROM tasks ORDER BY id")],
        "active_allocations": [
            database.row_to_dict(r)
            for r in conn.execute(
                "SELECT id, task_id, robot_id, charger_id, status,"
                " total_energy_wh, safety_margin_wh, reserved_energy_wh"
                " FROM allocations WHERE status IN ('planned','running')"
                " ORDER BY id")
        ],
    }
