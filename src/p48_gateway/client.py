"""Signed HTTP client used by demo scripts and tests (real HMAC per request)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from .crypto import hmac_sign, request_signing_payload


def auth_headers(
    *, method: str, path: str, tester: str, key: str, body: Any
) -> dict[str, str]:
    ts = time.time()
    nonce = uuid.uuid4().hex
    payload = request_signing_payload(method, path, tester, nonce, ts, body)
    return {
        "X-Tester": tester,
        "X-Timestamp": f"{ts:.6f}",
        "X-Nonce": nonce,
        "X-Signature": hmac_sign(payload, key),
    }


def signed_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    tester: str,
    key: str,
    json_body: Any = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    # The signature binds the *path* only, never the query string.
    path = "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    headers = auth_headers(
        method=method, path=path, tester=tester, key=key, body=json_body
    )
    return client.request(method, url, headers=headers, json=json_body, params=params)


def send_command(
    client: httpx.Client,
    base_url: str,
    *,
    robot_id: str,
    tester: str,
    key: str,
    target: str,
    seq: int,
    command: dict[str, Any],
    ttl_seconds: float | None = None,
) -> httpx.Response:
    path = f"/robots/{robot_id}/commands"
    body: dict[str, Any] = {"target": target, "seq": seq, "command": command}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    return signed_request(
        client, "POST", base_url.rstrip("/") + path,
        tester=tester, key=key, json_body=body,
    )
