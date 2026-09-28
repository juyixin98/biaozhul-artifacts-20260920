"""FastAPI 应用工厂与路由。

权限矩阵：
| 操作                         | investigator | auditor | admin |
|------------------------------|:---:|:---:|:---:|
| 创建研究                     | ✓ | – | ✓ |
| 登记 / 重复登记（回原分配）  | ✓ | – | – |
| 查询某对象的臂（揭盲）       | ✓ | – | ✓ |
| 均衡表 / 分布诊断            | ✓ | ✓ | ✓ |
| 随机流复核                   | – | ✓ | ✓ |
| 审计事件读取                 | – | ✓ | ✓ |
| 封尾组 / 封研究              | – | – | ✓ |
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import __version__, SCHEMA_VERSION
from ..core.seed import SecretSeed
from ..evidence.audit_log import AuditFileLogger
from ..evidence.balance import balance_report
from ..evidence.distribution import (
    arm_frequency_by_position, enumerate_permutation_distribution,
)
from ..evidence.stream_verify import verify_study
from ..evidence.trace import Trace
from ..errors import AppError, ErrorCategory
from ..config import Settings, load_settings
from ..service import AllocationService
from ..storage.database import Database
from ..core.vault import SeedVault
from ..storage.repository import Repository
from .schemas import (
    AllocateIn, DistributionProbeIn, OpenBlockIn, StudyCreateIn,
)
from .security import Principal, authenticate

REPO = Repository()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="分层区组随机分配服务（合成实验）",
        version=__version__,
        description="统计契约 / 估计内核 / 证据诊断 / 复现实验 四模块后端",
    )
    app.state.settings = settings
    app.state.db = Database(settings.database_path)
    app.state.vault = SeedVault(settings.seeds_path)
    app.state.audit_log = AuditFileLogger(settings.log_path)
    app.state.service = AllocationService(app.state.db, app.state.vault, REPO)

    _register_middleware(app)
    _register_exception_handlers(app)
    _register_routes(app)
    return app


# ---------------------------------------------------------------- 中间件
def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _file_log(request, request_id, 500, outcome="error",
                      category="UNHANDLED_EXCEPTION")
            raise
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Spec-Version"] = "contract/v1"
        response.headers["X-Service-Version"] = __version__
        if response.status_code >= 400:
            # 细节由异常处理器记录；这里记一行请求级摘要
            _file_log(request, request_id, response.status_code,
                      outcome="http_error")
        else:
            _file_log(request, request_id, response.status_code,
                      elapsed_ms=round((time.perf_counter() - start) * 1000, 2))
        return response

    def _file_log(request: Request, request_id: str, status: int, **extra) -> None:
        logger: AuditFileLogger = request.app.state.audit_log
        principal = getattr(request.state, "principal", None)
        logger.log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_id": request_id,
            "actor_role": principal.role if principal else "anonymous",
            "method": request.method,
            "path": request.url.path,
            "http_status": status,
            "spec_version": "contract/v1",
            "service_version": __version__,
            **extra,
        })


# ---------------------------------------------------------------- 异常
def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request,
                                 exc: RequestValidationError):
        """请求体/参数校验失败也使用统一错误信封与稳定类别。"""
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=422,
            content={"error": {
                "category": ErrorCategory.REQUEST_VALIDATION_FAILED,
                "message": "请求体或参数未通过校验",
                "details": jsonable_encoder(exc.errors()),
                "request_id": request_id,
            }},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        request_id = getattr(request.state, "request_id", None)
        body = exc.to_body(request_id)
        body["error"]["service_version"] = __version__
        # 失败事件进 JSONL：失败原因单列，带稳定类别
        app.state.audit_log.log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_id": request_id,
            "actor_role": getattr(request.state, "principal_role", "anonymous"),
            "action": f"{request.method} {request.url.path}",
            "outcome": "rejected",
            "failure": {
                "category": exc.category,
                "message": exc.message,
                "http_status": exc.http_status,
                "detail": exc.details,
            },
        })
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", None)
        app.state.audit_log.log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "request_id": request_id,
            "outcome": "uncertain",
            "failure": {"category": "UNHANDLED_EXCEPTION",
                        "message": "服务内部错误，分配结果未知；请求可安全重试（幂等）"},
        })
        return JSONResponse(
            status_code=500,
            content={"error": {
                "category": "UNHANDLED_EXCEPTION",
                "message": "内部错误（已记录 request_id）；登记请求携带 "
                           "Idempotency-Key 可安全重试",
                "request_id": request_id,
            }},
        )


# ---------------------------------------------------------------- 路由
def _register_routes(app: FastAPI) -> None:

    def new_trace(request: Request, principal: Principal) -> Trace:
        return Trace(
            request_id=request.state.request_id,
            actor_role=principal.role,
            versions={"service": __version__, "schema": str(SCHEMA_VERSION),
                      "contract_spec": "contract/v1"},
        )

    def envelope(trace: Trace, data: dict) -> dict:
        return {"data": data, "trace": trace.to_dict()}

    @app.get("/healthz")
    async def healthz(request: Request):
        return {"status": "ok", "service_version": __version__,
                "request_id": request.state.request_id}

    # ---- 创建研究 ----
    @app.post("/v1/studies", status_code=201)
    def create_study(
        body: StudyCreateIn,
        request: Request,
        principal: Principal = Depends(authenticate),
    ):
        _assert_role(principal, ("investigator", "admin"))
        request.state.principal = principal
        request.state.principal_role = principal.role
        trace = new_trace(request, principal)
        trace.study_id = body.study_id
        trace.step("parse_request", handler="app.api.routes:create_study")
        seed = SecretSeed.from_base64(body.seed_base64)
        trace.step("freeze_seed_commitment", handler="app.core.seed:SecretSeed.commit",
                   detail={"seed_fingerprint_prefix": seed.fingerprint()[:12]})
        contract_kwargs = dict(
            study_id=body.study_id,
            arm_specs=[{"arm_id": a.arm_id, "ratio": a.ratio} for a in body.arms],
            factor_specs=[{"name": f.name, "levels": f.levels}
                          for f in body.factors],
            block_multiple=body.block_multiple,
            tail_policy=body.tail_policy,
        )
        trace.step("validate_statistical_contract",
                   handler="app.contracts:build_contract")
        data = app.state.service.create_study(
            contract_kwargs=contract_kwargs, seed=seed,
            actor_role=principal.role, request_id=trace.request_id,
        )
        trace.step("persist_contract_and_audit",
                   handler="app.storage.repository:insert_study")
        trace.random_source({
            "seed_fingerprint": data["contract_fingerprint"],
            "note": "种子明文仅写入本地保险库（0600），任何接口不回读",
        })
        return envelope(trace, data)

    # ---- 登记分配 ----
    @app.post("/v1/studies/{study_id}/allocations", status_code=200)
    def allocate(
        study_id: str,
        body: AllocateIn,
        request: Request,
        idempotency_key: str | None = Header(default=None,
                                             alias="Idempotency-Key"),
        principal: Principal = Depends(authenticate),
    ):
        _assert_role(principal, ("investigator",))
        request.state.principal = principal
        request.state.principal_role = principal.role
        trace = new_trace(request, principal)
        trace.study_id = study_id
        trace.subject_id = body.subject_id
        trace.step("load_contract", handler="app.service:AllocationService.allocate")
        trace.step("freeze_identity_and_features",
                   handler="app.core.identity:build_identity")
        trace.step("enforce_idempotency_and_tail_policy",
                   handler="app.service:AllocationService.allocate")
        trace.step("claim_block_slot_in_serial_tx",
                   handler="app.storage.repository:claim_next_slot")
        data = app.state.service.allocate(
            study_id=study_id, subject_id=body.subject_id, features=body.features,
            idempotency_key=idempotency_key, actor_role=principal.role,
            request_id=trace.request_id,
        )
        trace.step("derive_arm_from_frozen_seed",
                   handler="app.core.allocator:assign_at_position",
                   detail={"outcome": data["outcome"], "arm": data["arm"],
                           "block_index": data["block_index"],
                           "position": data["position"]})
        trace.random_source({
            "prf": "HMAC-SHA256", "shuffle": "Fisher-Yates",
            "sampling": "unbiased rejection",
            "domain": "rct/v1/block-permutation",
            "contract_fingerprint": data["contract_fingerprint"],
        })
        if data["outcome"] == "replayed":
            trace.uncertain(
                "IDEMPOTENT_REPLAY",
                "本次为重复请求，返回的是首次分配；未消耗随机数，结果可重复",
            )
        if data.get("block_became_full"):
            trace.step("archive_completed_block_permutation",
                       handler="app.storage.repository:record_block_permutation")
        app.state.audit_log.log({
            "request_id": trace.request_id,
            "study_id": study_id,
            "subject_id": body.subject_id,
            "actor_role": principal.role,
            "action": "allocate",
            "outcome": data["outcome"],
            "arm": data["arm"],
            "spec_version": "contract/v1",
            "service_version": __version__,
            "contract_fingerprint": data["contract_fingerprint"],
            "versions": trace.versions,
            "steps": [s["step"] for s in trace.steps],
        })
        return envelope(trace, data)

    # ---- 揭盲查询 ----
    @app.get("/v1/studies/{study_id}/assignments/{subject_id}")
    def get_assignment(study_id: str, subject_id: str, request: Request,
                             principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("investigator", "admin"))
        request.state.principal = principal
        request.state.principal_role = principal.role
        trace = new_trace(request, principal)
        trace.study_id = study_id
        trace.subject_id = subject_id
        trace.step("read_persisted_assignment",
                   handler="app.service:AllocationService.get_assignment")
        data = app.state.service.get_assignment(
            study_id=study_id, subject_id=subject_id,
            actor_role=principal.role, request_id=trace.request_id,
        )
        return envelope(trace, data)

    # ---- 封尾组 ----
    @app.post("/v1/studies/{study_id}/actions/seal-tails")
    def seal_tails(study_id: str, request: Request,
                         principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("admin",))
        request.state.principal = principal
        request.state.principal_role = principal.role
        trace = new_trace(request, principal)
        trace.study_id = study_id
        data = app.state.service.seal_tails(
            study_id=study_id, actor_role=principal.role,
            request_id=trace.request_id,
        )
        for item in data["sealed_tails"]:
            trace.uncertain(
                "TAIL_SEALED_INCOMPLETE",
                f"层 {item['stratum_key']} 区组 {item['block_index']} 尾组"
                f"未满（缺 {item['missing']}）已按策略封闭，比例不补齐",
                item,
            )
        return envelope(trace, data)

    # ---- 封研究（整体停止入组） ----
    @app.post("/v1/studies/{study_id}/actions/seal")
    def seal_study(study_id: str, request: Request,
                         principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("admin",))
        request.state.principal = principal
        request.state.principal_role = principal.role
        trace = new_trace(request, principal)
        trace.study_id = study_id
        data = app.state.service.seal_study(
            study_id=study_id, actor_role=principal.role,
            request_id=trace.request_id)
        return envelope(trace, data)

    # ---- 显式开下一个区组（seal_early） ----
    @app.post("/v1/studies/{study_id}/blocks")
    def open_block(study_id: str, body: OpenBlockIn, request: Request,
                         principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("admin",))
        request.state.principal = principal
        request.state.principal_role = principal.role
        trace = new_trace(request, principal)
        trace.study_id = study_id
        data = app.state.service.open_next_block(
            study_id=study_id, stratum_key=body.stratum_key,
            actor_role=principal.role, request_id=trace.request_id)
        return envelope(trace, data)

    # ---- 均衡表 ----
    @app.get("/v1/studies/{study_id}/evidence/balance")
    def get_balance(study_id: str, request: Request,
                          principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("investigator", "auditor", "admin"))
        request.state.principal = principal
        trace = new_trace(request, principal)
        trace.study_id = study_id
        with app.state.db.connection() as conn:
            loaded = app.state.service.load_contract(conn, study_id)
            contract = loaded["contract"]
            blocks = REPO.list_blocks(conn, study_id)
            allocations = REPO.list_allocations(conn, study_id)
        data = balance_report(contract, blocks, allocations)
        for u in data["uncertainties"]:
            trace.uncertain(u["category"], u["reason"], u)
        return envelope(trace, data)

    # ---- 随机流复核 ----
    @app.post("/v1/studies/{study_id}/evidence/verify")
    def post_verify(study_id: str, request: Request,
                          principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("auditor", "admin"))
        request.state.principal = principal
        trace = new_trace(request, principal)
        trace.study_id = study_id
        trace.step("recompute_all_decisions_from_seed",
                   handler="app.evidence.stream_verify:verify_study")
        data = verify_study(app.state.db, app.state.vault, study_id, REPO)
        if data["verdict"] == "UNCERTAIN":
            trace.uncertain(data["category"], data["message"])
        elif data["verdict"] == "FAIL":
            trace.fail("VERIFICATION_FAILED",
                       "随机流复核发现不一致，详见 failures", 200,
                       {"failures": data["failures"]})
        return envelope(trace, data)

    # ---- 审计事件 ----
    @app.get("/v1/studies/{study_id}/audit")
    def get_audit(study_id: str, request: Request, limit: int = 200,
                        principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("auditor", "admin"))
        request.state.principal = principal
        limit = max(1, min(limit, 1000))
        with app.state.db.connection() as conn:
            REPO.get_study_row(conn, study_id)
            rows = REPO.query_audit(conn, study_id, limit)
            events = []
            for r in rows:
                events.append({
                    "ts": r["ts"], "request_id": r["request_id"],
                    "subject_id": r["subject_id"], "actor_role": r["actor_role"],
                    "action": r["action"], "outcome": r["outcome"],
                    "category": r["category"],
                    "contract_fingerprint": r["contract_fingerprint"],
                    "stream_locators": (
                        json.loads(r["stream_locators_json"])
                        if r["stream_locators_json"] else None),
                    "detail": json.loads(r["detail_json"] or "{}"),
                    "spec_version": r["spec_version"],
                })
        return {"data": {"study_id": study_id, "events": events,
                         "audit_log_file": app.state.settings.log_path}}

    # ---- 分布诊断（不依赖具体研究数据） ----
    @app.post("/v1/diagnostics/distribution")
    def distribution_diagnostic(body: DistributionProbeIn, request: Request,
                                      principal: Principal = Depends(authenticate)):
        _assert_role(principal, ("investigator", "auditor", "admin"))
        request.state.principal = principal
        trace = new_trace(request, principal)
        ratio_sum = sum(a.ratio for a in body.arms)
        if body.block_size % ratio_sum != 0:
            raise AppError(
                ErrorCategory.REQUEST_VALIDATION_FAILED, 422,
                f"block_size={body.block_size} 必须是比例和 {ratio_sum} 的整数倍",
                {"block_size": body.block_size, "ratio_sum": ratio_sum},
            )
        multiple = body.block_size // ratio_sum
        block_size = body.block_size
        arm_ids = tuple(a.arm_id for a in body.arms)
        slot_arms = tuple(
            arm for a in body.arms
            for arm in [a.arm_id] * (a.ratio * multiple)
        )
        trace.step("enumerate_small_blocks",
                   handler="app.evidence.distribution:enumerate_permutation_distribution")
        results: dict[str, Any] = {}
        if 2 <= block_size <= 5:
            results["permutation_enumeration"] = (
                enumerate_permutation_distribution(
                    block_size, blocks_per_seed=body.blocks_per_seed))
        else:
            trace.uncertain(
                "ENUMERATION_SKIPPED",
                f"区组长度 {block_size} > 5，全排列枚举组合爆炸，跳过；"
                "改用逐位置比例检验",
            )
        trace.step("position_arm_frequency",
                   handler="app.evidence.distribution:arm_frequency_by_position")
        results["arm_frequency_by_position"] = arm_frequency_by_position(
            arm_ids, slot_arms, block_size, blocks_per_seed=body.blocks_per_seed)
        trace.uncertain(
            "STATISTICAL_NON_PROOF",
            "卡方/游程检验不显著仅表示本批固定种子样本未发现反例，"
            "不构成随机性或分配正确性的证明；硬证据是逐数复算与区组枚举。",
        )
        return envelope(trace, {"block_size": block_size, "results": results})


def _assert_role(principal: Principal, allowed: tuple[str, ...]) -> None:
    if principal.role not in allowed:
        raise AppError(
            ErrorCategory.FORBIDDEN_ROLE, 403,
            f"角色 {principal.role!r} 无权执行此操作；需要 {sorted(allowed)} 之一",
            {"actual_role": principal.role, "required_roles": list(allowed)},
        )
