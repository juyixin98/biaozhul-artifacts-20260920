"""FastAPI application: offline policy evaluation HTTP endpoints.

Endpoints
---------
``GET  /healthz``                  liveness probe
``POST /v1/evaluate``              one policy -> decision + relevant rules
``POST /v1/evaluate-policy-set``   several policies -> deny-overrides + conflicts
``GET  /v1/trust``                 list trusted signing-key fingerprints

Signed vs unsigned
-------------------
Every request carries either an inline ``policy``/``policies`` document or a
``signed`` Ed25519 bundle. Inline documents are only accepted when the server
was explicitly started with ``POLICY_ALLOW_UNSIGNED=1`` (demo/testing);
production deployments provision ``.pem`` trust anchors instead.
"""

from __future__ import annotations

import json
import os
from typing import Sequence

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .errors import PayloadError, PolicyError
from .evaluator import (
    check_rule_order_invariance,
    check_set_order_invariance,
    evaluate_policy,
    evaluate_policy_set,
)
from .interpreter import AttrBag
from .model import Policy, parse_policy, parse_policy_set
from .schemas import (
    EvaluateRequest,
    SetEvaluateRequest,
    TruthTableRequest,
)
from .signing import TrustStore
from .truth_table import enumerate_truth_table

DEFAULT_MAX_BODY_BYTES = 256 * 1024


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def create_app(
    trust_store: TrustStore | None = None,
    *,
    allow_unsigned: bool | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> FastAPI:
    if trust_store is None:
        trust_store = TrustStore.from_directory(
            os.getenv("POLICY_TRUST_DIR", "examples/keys")
        )
    if allow_unsigned is None:
        allow_unsigned = _env_flag("POLICY_ALLOW_UNSIGNED", "1" if _running_under_test() else "0")

    app = FastAPI(
        title="Offline Policy Evaluation Interpreter",
        version="1.0.0",
        description=(
            "Three-valued attribute policy engine (subject/resource "
            "attributes, set containment, logic composition, deny-overrides) "
            "with Ed25519 signed policy bundles. No policy code is ever "
            "executed: conditions are declarative ASTs interpreted locally."
        ),
    )

    # ---- body size guard (defense-in-depth against oversized payloads) ----
    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_body_bytes:
                    return JSONResponse(
                        {"error": "payload_too_large",
                         "message": f"request body exceeds {max_body_bytes} bytes"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"error": "invalid_request",
                     "message": "invalid content-length"},
                    status_code=400,
                )
        return await call_next(request)

    # ---- uniform error envelope -------------------------------------------
    @app.exception_handler(PolicyError)
    async def policy_error_handler(request: Request, exc: PolicyError):
        return JSONResponse(exc.to_dict(), status_code=exc.http_status)

    @app.exception_handler(ValidationError)
    async def validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(
            {"error": "invalid_request",
             "message": "request body failed schema validation",
             "details": exc.errors(include_url=False)},
            status_code=422,
        )

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "unsigned_allowed": allow_unsigned,
            "trusted_key_count": len(trust_store.trusted_kids),
        }

    @app.get("/v1/trust")
    async def trust():
        return {"trusted_kids": trust_store.trusted_kids}

    def _resolve_policy(body) -> Policy:
        if body.signed is not None:
            document = trust_store.verify_bundle(body.signed)
            if "policies" in document:
                raise PayloadError(
                    "/v1/evaluate expects a single policy; use "
                    "/v1/evaluate-policy-set for policy sets"
                )
            return parse_policy(document)
        if body.policy is None:
            raise PayloadError(
                "provide exactly one of 'policy' (unsigned) or 'signed'"
            )
        if not allow_unsigned:
            from .errors import TrustError

            raise TrustError(
                "unsigned policies are disabled; send a signed bundle or "
                "start the server with POLICY_ALLOW_UNSIGNED=1"
            )
        return parse_policy(body.policy)

    def _resolve_set(body):
        if body.signed is not None:
            document = trust_store.verify_bundle(body.signed)
            if "policies" not in document:
                raise PayloadError(
                    "signed bundle for a policy set must contain 'policies'"
                )
            return parse_policy_set(document)
        if not body.policies:
            raise PayloadError(
                "provide 'policies' (unsigned) or a 'signed' policy-set bundle"
            )
        if not allow_unsigned:
            from .errors import TrustError

            raise TrustError(
                "unsigned policies are disabled; send a signed bundle or "
                "start the server with POLICY_ALLOW_UNSIGNED=1"
            )
        return parse_policy_set({"policies": body.policies})

    @app.post("/v1/evaluate")
    async def evaluate(request: Request):
        body = await _parse(request, EvaluateRequest)
        policy = _resolve_policy(body)
        subject = AttrBag(body.subject)
        resource = AttrBag(body.resource)
        result = evaluate_policy(policy, subject, resource)
        result["order_check"] = check_rule_order_invariance(
            policy, subject, resource
        )
        return result

    @app.post("/v1/evaluate-policy-set")
    async def evaluate_set(request: Request):
        body = await _parse(request, SetEvaluateRequest)
        policies = _resolve_set(body)
        subject = AttrBag(body.subject)
        resource = AttrBag(body.resource)
        result = evaluate_policy_set(policies, subject, resource)
        result["order_check"] = check_set_order_invariance(
            policies, subject, resource
        )
        return result

    @app.post("/v1/truth-table")
    async def truth_table(request: Request):
        body = await _parse(request, TruthTableRequest)
        if body.signed is not None:
            document = trust_store.verify_bundle(body.signed)
            if "policies" in document:
                policies = parse_policy_set(document)
                target: Policy | Sequence[Policy] = policies
            else:
                target = parse_policy(document)
        elif body.policy is not None and not body.policies:
            if not allow_unsigned:
                from .errors import TrustError

                raise TrustError("unsigned policies are disabled")
            target = parse_policy(body.policy)
        elif body.policies and body.policy is None:
            if not allow_unsigned:
                from .errors import TrustError

                raise TrustError("unsigned policies are disabled")
            target = parse_policy_set({"policies": body.policies})
        else:
            raise PayloadError(
                "provide exactly one of 'policy', 'policies' or 'signed'"
            )
        return enumerate_truth_table(
            target,
            body.variables,
            mode=body.mode,
            include_missing=body.include_missing,
        )

    return app


async def _parse(request: Request, model_cls):
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PayloadError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PayloadError("request body must be a JSON object")
    try:
        return model_cls.model_validate(raw)
    except ValidationError:
        # Re-raise so the dedicated handler renders the standard envelope.
        raise


def _running_under_test() -> bool:
    return "PYTEST_VERSION" in os.environ or "PYTEST_CURRENT_TEST" in os.environ


app = create_app()
