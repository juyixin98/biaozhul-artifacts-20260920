"""FastAPI HTTP layer for the multisig timelock executor.

Endpoints
---------
GET  /health                       chain + contract connectivity
GET  /state                        signers, threshold, nonce, timelock params
GET  /targets                      deployed target contracts
GET  /operations/digest            op id + signing digest for a payload
GET  /operations/{op_id}           op snapshot
POST /operations/propose           propose + attach signatures
POST /operations/approve           add signatures to a proposed op
POST /operations/execute           execute after timelock (or retry)
POST /admin/signers                replace signer set / threshold
POST /dev/warp                     (Anvil only) fast-forward chain time
POST /dev/flaky                    (Anvil demo) toggle FlakyTarget state
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .chain import ChainError
from .config import Settings
from .timelock import TimelockService, decode_hex


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #


class ProposeIn(BaseModel):
    target: str = Field(description="Checksum-addressable target contract")
    value: int = Field(default=0, ge=0)
    data: str = Field(default="0x", description="Hex-encoded calldata")
    deadline: int = Field(gt=0, description="Absolute unix timestamp")
    # Either sign locally with the service wallet indices...
    signer_indices: Optional[list[int]] = None
    # ...or provide externally-produced 65-byte EIP-712 signatures.
    signatures: Optional[list[str]] = None


class ApproveIn(BaseModel):
    target: str
    value: int = Field(default=0, ge=0)
    data_hash: str = Field(description="keccak256 of the original calldata")
    nonce: int = Field(ge=0)
    deadline: int = Field(gt=0)
    signer_indices: Optional[list[int]] = None
    signatures: Optional[list[str]] = None


class ExecuteIn(BaseModel):
    target: str
    value: int = Field(default=0, ge=0)
    data: str = Field(default="0x")
    nonce: int = Field(ge=0)
    deadline: int = Field(gt=0)


class ChangeSignersIn(BaseModel):
    signers: list[str] = Field(min_length=1)
    threshold: int = Field(ge=1)
    deadline: int = Field(gt=0)
    signer_indices: Optional[list[int]] = None
    signatures: Optional[list[str]] = None


class WarpIn(BaseModel):
    seconds: int = Field(gt=0)


class FlakyIn(BaseModel):
    failing: bool


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="Multisig Timelock Executor",
        version="1.0.0",
        description="Local multi-signature delayed-execution service over Anvil.",
    )
    app.state.settings = settings
    app.state.service = TimelockService(settings)

    def svc() -> TimelockService:
        return app.state.service

    def chain_error(exc: ChainError) -> HTTPException:
        detail = {"message": str(exc)}
        if exc.reason:
            detail["revert"] = svc().decode_error(exc.reason)
        return HTTPException(status_code=400, detail=detail)

    # ---- health / meta -----------------------------------------------------

    @app.get("/health")
    def health():
        try:
            return {"status": "ok", "chain_id": svc().chain.chain_id,
                    "now": svc().chain.now()}
        except ChainError as exc:
            raise chain_error(exc)

    @app.get("/state")
    def get_state():
        try:
            return svc().state()
        except ChainError as exc:
            raise chain_error(exc)

    @app.get("/targets")
    def get_targets():
        return svc().targets

    # ---- operations --------------------------------------------------------

    @app.get("/operations/digest")
    def operation_digest(target: str, nonce: int, deadline: int,
                         value: int = 0, data: str = "0x"):
        try:
            return svc().digest_for(
                target, value, decode_hex(data), nonce, deadline
            )
        except ChainError as exc:
            raise chain_error(exc)

    @app.get("/operations/{op_id}")
    def get_operation(op_id: str):
        try:
            op = svc().get_op(op_id)
            if op.get("state") == "None":
                raise HTTPException(status_code=404, detail={"message": "unknown op"})
            return op
        except ChainError as exc:
            raise chain_error(exc)

    @app.post("/operations/{op_id}/void")
    def void_op(op_id: str):
        try:
            return svc().void_expired(op_id)
        except ChainError as exc:
            raise chain_error(exc)

    @app.post("/operations/propose")
    def propose(body: ProposeIn):
        try:
            return svc().propose(
                body.target,
                body.value,
                decode_hex(body.data),
                body.deadline,
                body.signer_indices,
                body.signatures,
            )
        except ChainError as exc:
            raise chain_error(exc)

    @app.post("/operations/approve")
    def approve(body: ApproveIn):
        try:
            return svc().approve(
                body.target,
                body.value,
                body.data_hash,
                body.nonce,
                body.deadline,
                body.signer_indices,
                body.signatures,
            )
        except ChainError as exc:
            raise chain_error(exc)

    @app.post("/operations/execute")
    def execute(body: ExecuteIn):
        try:
            return svc().execute(
                body.target,
                body.value,
                decode_hex(body.data),
                body.nonce,
                body.deadline,
            )
        except ChainError as exc:
            raise chain_error(exc)

    # ---- administration ----------------------------------------------------

    @app.post("/admin/signers")
    def change_signers(body: ChangeSignersIn):
        try:
            return svc().change_signers(
                body.signers,
                body.threshold,
                body.deadline,
                body.signer_indices,
                body.signatures,
            )
        except ChainError as exc:
            raise chain_error(exc)

    # ---- dev-only (Anvil) --------------------------------------------------

    if settings.enable_dev_endpoints:
        @app.post("/dev/warp")
        def warp(body: WarpIn):
            try:
                return {"status": "ok", "now": svc().chain.increase_time(body.seconds)}
            except ChainError as exc:
                raise chain_error(exc)

        @app.post("/dev/flaky")
        def flip_flaky(body: FlakyIn):
            try:
                return svc().flip_flaky(body.failing)
            except ChainError as exc:
                raise chain_error(exc)

    return app


# Serve with:  uvicorn service.app:create_app --factory
# (no module-level app instance: importing this module must not require a
# running chain or a deployments.json file)

