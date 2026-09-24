"""FastAPI application: offline DBC management and CAN frame decoding."""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import PlainTextResponse

from . import __version__
from .bitmap import render_message_bitmap
from .decoder import decode_frame
from .errors import DecoderError, DBCParseError, FrameError, NotFoundError
from .schemas import (
    DBCDetail,
    DBCSummary,
    DecodeRequest,
    DecodeResponse,
    LogEntry,
    MessageSummary,
    SignalOut,
    UploadDBCRequest,
)
from .storage import Store

DB_PATH = os.environ.get("CAN_DECODER_DB", os.path.join("data", "can_decoder.db"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tests can inject a pre-built store before the client starts; in that
    # case do not open (and on shutdown, do not close) the production store.
    store = getattr(app.state, "store", None)
    injected = store is not None
    if not injected:
        store = Store(DB_PATH)
        app.state.store = store
    yield
    if not injected:
        store.close()


app = FastAPI(
    title="Offline CAN Signal Decoder",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Parse a deliberately limited DBC subset and decode standard "
        "11-bit CAN frames (Intel/Motorola, signed, scale/offset, "
        "single-layer multiplexing). No vehicle connection is ever made."
    ),
)


def get_store(request: Request) -> Store:
    return request.app.state.store


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


def _error(exc: DecoderError, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@app.post("/dbc", response_model=DBCSummary, status_code=201,
          summary="Create a DBC document")
def create_dbc(body: UploadDBCRequest, request: Request) -> DBCSummary:
    store = get_store(request)
    content_sha = hashlib.sha256(body.content.encode("utf-8")).hexdigest()
    try:
        stored = store.add_dbc(
            body.name, body.content, content_sha, allow_replace=body.replace
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "already_exists", "message": str(exc)}
        )
    except DBCParseError as exc:
        raise _error(exc, 400)
    _, parsed = store.get_parsed(body.name)
    return DBCSummary(
        name=stored.name,
        version=stored.version,
        message_count=len(parsed.messages),
        created_at=stored.created_at,
    )


@app.post("/dbc/upload", response_model=DBCSummary, status_code=201,
          summary="Upload a .dbc file (multipart)")
async def upload_dbc(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    replace: bool = Form(False),
) -> DBCSummary:
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "encoding_error",
                    "message": f"DBC file must be UTF-8 text: {exc}"},
        )
    doc_name = name or os.path.splitext(file.filename or "uploaded")[0]
    store = get_store(request)
    content_sha = hashlib.sha256(raw).hexdigest()
    try:
        stored = store.add_dbc(doc_name, text, content_sha, allow_replace=replace)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "already_exists", "message": str(exc)}
        )
    except DBCParseError as exc:
        raise _error(exc, 400)
    _, parsed = store.get_parsed(doc_name)
    return DBCSummary(
        name=stored.name,
        version=stored.version,
        message_count=len(parsed.messages),
        created_at=stored.created_at,
    )


@app.get("/dbc", response_model=list[DBCSummary], summary="List DBC documents")
def list_dbc(request: Request) -> list[DBCSummary]:
    store = get_store(request)
    out = []
    for s in store.list_dbc():
        _, parsed = store.get_parsed(s.name)
        out.append(DBCSummary(
            name=s.name, version=s.version,
            message_count=len(parsed.messages), created_at=s.created_at,
        ))
    return out


@app.get("/dbc/{name}", response_model=DBCDetail, summary="Get a DBC document")
def get_dbc(name: str, request: Request) -> DBCDetail:
    store = get_store(request)
    try:
        stored = store.get_dbc(name)
        _, parsed = store.get_parsed(name)
    except NotFoundError as exc:
        raise _error(exc, 404)
    return DBCDetail(
        name=stored.name,
        version=stored.version,
        message_count=len(parsed.messages),
        created_at=stored.created_at,
        content=stored.content,
        messages=[
            MessageSummary(
                frame_id=m.frame_id,
                frame_id_hex=f"0x{m.frame_id:03X}",
                name=m.name,
                dlc=m.dlc,
                sender=m.sender,
                signal_count=len(m.signals),
                mux_switch=m.mux_switch,
            )
            for m in parsed.messages
        ],
    )


@app.delete("/dbc/{name}", status_code=204, summary="Delete a DBC document")
def delete_dbc(name: str, request: Request) -> None:
    store = get_store(request)
    try:
        store.get_dbc(name)
    except NotFoundError as exc:
        raise _error(exc, 404)
    store.delete_dbc(name)


@app.get("/dbc/{name}/bitmap/{frame_id}", response_class=PlainTextResponse,
         summary="Hand-verifiable bit map for one frame")
def bitmap(name: str, frame_id: int | str, request: Request) -> str:
    store = get_store(request)
    try:
        _, parsed = store.get_parsed(name)
    except NotFoundError as exc:
        raise _error(exc, 404)
    fid = frame_id if isinstance(frame_id, int) else int(str(frame_id), 0)
    message = next((m for m in parsed.messages if m.frame_id == fid), None)
    if message is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found",
                    "message": f"frame 0x{fid:X} not defined in DBC '{name}'"},
        )
    return render_message_bitmap(message)


@app.post("/dbc/{name}/decode", response_model=DecodeResponse,
          summary="Decode one CAN frame against a stored DBC")
def decode(name: str, body: DecodeRequest, request: Request) -> DecodeResponse:
    store = get_store(request)
    try:
        _, parsed = store.get_parsed(name)
    except NotFoundError as exc:
        raise _error(exc, 404)

    frame_id = body.frame_id
    assert isinstance(frame_id, int)
    if frame_id > 0x7FF:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "frame_error",
                "message": (
                    f"frame id {frame_id} (0x{frame_id:X}) is outside the "
                    "standard 11-bit range (0..0x7FF)"
                ),
            },
        )

    data = body.data
    try:
        result = decode_frame(
            parsed, frame_id, data, strict_dlc=body.strict_dlc
        )
    except FrameError as exc:
        store.log_decode(name, frame_id, len(data), data.hex(), None, False, str(exc))
        raise _error(exc, 422)

    store.log_decode(
        name, frame_id, len(data), data.hex(), result.message_name, True, None
    )
    return DecodeResponse(
        dbc_name=name,
        dbc_version=result.dbc_version,
        frame_id=result.frame_id,
        frame_id_hex=f"0x{result.frame_id:03X}",
        message_name=result.message_name,
        dlc=result.dlc,
        mux_value=result.mux_value,
        signals=[
            SignalOut(
                name=s.name, raw=s.raw, physical=s.physical, unit=s.unit,
                byte_order=s.byte_order, signed=s.signed, present=s.present,
                mux_id=s.mux_id, signal_version=s.signal_version,
            )
            for s in result.signals
        ],
    )


@app.get("/log", response_model=list[LogEntry], summary="Recent decode attempts")
def get_log(request: Request, limit: int = 50) -> list[LogEntry]:
    store = get_store(request)
    return [
        LogEntry(
            dbc_name=r["dbc_name"], frame_id=r["frame_id"], dlc=r["dlc"],
            data_hex=r["data_hex"], message_name=r["message_name"],
            ok=bool(r["ok"]), error=r["error"], created_at=r["created_at"],
        )
        for r in store.recent_log(min(max(limit, 1), 500))
    ]
