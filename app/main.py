"""FastAPI application: DBC upload, frame decoding, history, HMAC verify."""

from __future__ import annotations

import binascii
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import crypto
from .dbc import (
    STANDARD_ID_MAX,
    DBCError,
    Database,
    parse_dbc,
)
from .decoder import DecodeError, decode_frame
from .storage import Store

app = FastAPI(
    title="CAN Signal Decode API",
    version="1.0.0",
    description=(
        "Offline CAN decoder for a restricted DBC subset: standard 11-bit "
        "frames, Intel/Motorola byte orders, signed signals, scaling/offset "
        "and single-level multiplexing. No vehicle connection."
    ),
)

_store: Store | None = None
_db_cache: dict[str, Database] = {}


def get_store() -> Store:
    """Lazily build the process-wide Store, honoring ``CANDECODE_DB``."""

    global _store
    if _store is None:
        from .storage import DEFAULT_DB_PATH
        _store = Store(os.environ.get("CANDECODE_DB", DEFAULT_DB_PATH))
    return _store


@app.on_event("shutdown")
def _shutdown() -> None:  # pragma: no cover - lifecycle
    global _store
    if _store is not None:
        _store.close()
        _store = None


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
@app.exception_handler(DBCError)
async def _dbc_error_handler(_request: Request, exc: DBCError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": exc.detail()})


@app.exception_handler(DecodeError)
async def _decode_error_handler(
    _request: Request, exc: DecodeError
) -> JSONResponse:
    not_found = {"unknown_message", "no_definition", "unknown_definition"}
    status = 404 if exc.code in not_found else 422
    return JSONResponse(status_code=status, content={"error": exc.detail()})


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #
class DBCUploadRequest(BaseModel):
    dbc: str = Field(..., description="Raw DBC file contents (UTF-8 text)")
    source_uri: str | None = Field(
        default=None, max_length=512,
        description="Optional provenance tag, e.g. an internal file path",
    )


class FrameDecodeRequest(BaseModel):
    frame_id: int = Field(
        ..., ge=0, le=STANDARD_ID_MAX,
        description=f"Standard 11-bit CAN id (0..0x{STANDARD_ID_MAX:X})",
    )
    data: str | list[int] = Field(
        ...,
        description=(
            "Payload bytes: either a hex string ('01 A2 FF' / '01a2ff') "
            "or a list of 0..255 integers, length 0..8"
        ),
    )
    dlc: int | None = Field(
        default=None, ge=0, le=8,
        description="Declared DLC; defaults to the DBC message DLC. "
                    "Must equal the payload length.",
    )
    version_id: str | None = Field(
        default=None,
        description="DBC definition version to decode against; "
                    "defaults to the most recently uploaded one",
    )
    persist: bool = Field(default=True)


class VerifyRequest(BaseModel):
    version_id: str
    signature: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_database(version_id: str | None) -> tuple[str, Database]:
    store = get_store()
    if version_id is None:
        row = store.latest_definition()
        if row is None:
            raise DecodeError(
                "no DBC definition has been uploaded; POST /dbc first",
                code="no_definition",
            )
        version_id = row["version_id"]
        source = row["source"]
    else:
        row = store.get_definition(version_id)
        if row is None:
            raise DecodeError(
                f"unknown definition version {version_id}",
                code="unknown_definition",
            )
        source = row["source"]

    if version_id not in _db_cache:
        database = parse_dbc(source)
        if crypto.fingerprint(database) != version_id:
            # Stored content has been tampered with and no longer matches
            # its advertised version; refuse rather than silently decode.
            raise DecodeError(
                "stored DBC content does not match its version fingerprint",
                code="fingerprint_mismatch",
            )
        _db_cache[version_id] = database
    return version_id, _db_cache[version_id]


def _parse_data(data: str | list[int]) -> list[int]:
    if isinstance(data, list):
        bytes_out = data
    else:
        cleaned = data.strip().replace(" ", "").replace("0x", "").replace("-", "")
        if len(cleaned) % 2:
            raise DecodeError(
                "hex data must contain whole bytes "
                "(an even number of hex digits)",
                code="bad_hex",
            )
        try:
            raw = binascii.unhexlify(cleaned)
        except binascii.Error as exc:
            raise DecodeError(f"invalid hex data: {exc}", code="bad_hex") from exc
        bytes_out = list(raw)
    if not isinstance(bytes_out, list) or not all(
        isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 255
        for b in bytes_out
    ):
        raise DecodeError(
            "data must be a list of byte integers (0..255)",
            code="bad_hex",
        )
    if len(bytes_out) > 8:
        raise DecodeError(
            f"CAN payload is {len(bytes_out)} bytes; maximum is 8",
            code="data_too_long",
        )
    return bytes_out


def _signal_to_json(signal) -> dict:
    return {
        "name": signal.name,
        "raw": signal.raw,
        "value": signal.value,
        "unit": signal.unit,
        "length": signal.length,
        "byte_order": signal.byte_order,
        "signed": signal.signed,
        "mux_kind": signal.mux_kind,
        "mux_value": signal.mux_value,
        "definition_version": signal.definition_version,
    }


def _frame_to_json(frame) -> dict:
    return {
        "frame_id": frame.frame_id,
        "frame_id_hex": f"0x{frame.frame_id:03X}",
        "message_name": frame.message_name,
        "dlc": frame.dlc,
        "data_hex": frame.data_hex,
        "mux": frame.mux,
        "definition_version": frame.definition_version,
        "signals": [_signal_to_json(s) for s in frame.signals],
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "can-signal-decode", "version": "1.0.0"}


@app.post("/dbc")
def upload_dbc(body: DBCUploadRequest) -> dict:
    # Parsing + all validations happen before anything is written.
    database = parse_dbc(body.dbc)
    version_id, signature, created = get_store().upsert_definition(
        database, body.dbc
    )
    _db_cache[version_id] = database
    return {
        "version_id": version_id,
        "source_sha256": crypto.sha256_text(body.dbc),
        "signature_sha256": signature,
        "signature_algorithm": "HMAC-SHA256",
        "created": created,
        "dbc_version": database.version,
        "nodes": list(database.nodes),
        "message_count": len(database.messages),
        "messages": [
            {
                "frame_id": m.frame_id,
                "frame_id_hex": f"0x{m.frame_id:03X}",
                "name": m.name,
                "dlc": m.dlc,
                "multiplexed": m.mux_switch is not None,
                "signal_count": len(m.signals),
            }
            for m in sorted(database.messages, key=lambda m: m.frame_id)
        ],
    }


@app.get("/definitions/{version_id}")
def get_definition(version_id: str) -> dict:
    row = get_store().get_definition(version_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": {
                "code": "unknown_definition",
                "message": f"unknown definition version {version_id}",
            }},
        )
    return {
        "version_id": row["version_id"],
        "source_sha256": row["source_hash"],
        "signature_sha256": row["signature"],
        "created_at": row["created_at"],
        "dbc_version": row["db_version"],
        "nodes": json.loads(row["nodes"]),
        "message_count": row["message_count"],
    }


@app.post("/verify")
def verify_definition(body: VerifyRequest) -> dict:
    row = get_store().get_definition(body.version_id)
    if row is None:
        raise DecodeError(
            f"unknown definition version {body.version_id}",
            code="unknown_definition",
        )
    valid = crypto.verify(
        body.version_id, body.signature, get_store().hmac_key()
    )
    return {
        "version_id": body.version_id,
        "valid": valid,
        "algorithm": "HMAC-SHA256",
    }


@app.post("/decode")
def decode(body: FrameDecodeRequest) -> dict:
    version_id, database = _load_database(body.version_id)
    message = database.message_by_id(body.frame_id)
    if message is None:
        raise DecodeError(
            f"frame id 0x{body.frame_id:X} is not defined in DBC version "
            f"{version_id[:12]}",
            code="unknown_message",
        )
    payload = _parse_data(body.data)
    frame = decode_frame(
        message, payload, version_id, dlc=body.dlc
    )

    frame_pk = None
    if body.persist:
        frame_pk = get_store().record_frame(
            version_id,
            {
                "frame_id": frame.frame_id,
                "message_name": frame.message_name,
                "dlc": frame.dlc,
                "data_hex": frame.data_hex,
                "mux": frame.mux,
            },
            [_signal_to_json(s) for s in frame.signals],
        )

    result = _frame_to_json(frame)
    result["stored_frame_id"] = frame_pk
    return result


@app.get("/frames")
def get_frames(
    version_id: str | None = None,
    frame_id: int | None = None,
    limit: int = 50,
) -> dict:
    limit = max(1, min(limit, 500))
    rows = get_store().list_frames(version_id, frame_id, limit)
    frames = []
    for row in rows:
        signals = [
            {
                "name": s["name"],
                "raw": s["raw"],
                "value": s["value"],
                "unit": s["unit"],
                "mux_kind": s["mux_kind"],
                "mux_value": s["mux_value"],
                "definition_version": row["version_id"],
            }
            for s in get_store().signals_for(row["id"])
        ]
        frames.append({
            "stored_frame_id": row["id"],
            "frame_id": row["frame_id"],
            "frame_id_hex": f"0x{row['frame_id']:03X}",
            "message_name": row["message_name"],
            "dlc": row["dlc"],
            "data_hex": row["data_hex"],
            "mux": row["mux"],
            "received_at": row["received_at"],
            "definition_version": row["version_id"],
            "signals": signals,
        })
    return {"count": len(frames), "frames": frames}
