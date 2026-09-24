"""FastAPI envelope-encryption service.

Endpoints:
  POST /objects            upload raw bytes, returns the object id
  GET  /objects/{id}       download decrypted plaintext (never partial output)
  GET  /objects/{id}/info  parsed header metadata (no plaintext)
  GET  /keys               master-key version listing
  POST /keys/rotate        create a new master-key version (old ones retained)
  POST /rewrap             re-wrap every object's DEK under the current key
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request, Response

from .crypto import EnvelopeError, decrypt, encrypt, parse_header, rewrap
from .keystore import KeyStore
from .storage import ObjectStore

DATA_DIR = os.environ.get("ENVELOPE_DATA_DIR", "data")

keys = KeyStore(os.path.join(DATA_DIR, "keys.json"))
objects = ObjectStore(os.path.join(DATA_DIR, "objects"))

app = FastAPI(title="Envelope Encryption Service", version="1.0.0")


@app.post("/objects")
async def create_object(request: Request) -> dict:
    plaintext = await request.body()
    blob = encrypt(keys.current_key(), keys.current_version(), plaintext)
    object_id = objects.new_id()
    objects.put(object_id, blob)
    return {
        "id": object_id,
        "kek_version": keys.current_version(),
        "stored_bytes": len(blob),
    }


@app.get("/objects/{object_id}")
async def read_object(object_id: str) -> Response:
    if not objects.exists(object_id):
        raise HTTPException(status_code=404, detail="object not found")
    try:
        plaintext = decrypt(keys.get, objects.get(object_id))
    except EnvelopeError as exc:
        # Authentication/parsing failed: return an error only, never bytes
        # that could be mistaken for (partial) plaintext.
        raise HTTPException(status_code=422, detail=str(exc))
    return Response(content=plaintext, media_type="application/octet-stream")


@app.get("/objects/{object_id}/info")
async def object_info(object_id: str) -> dict:
    if not objects.exists(object_id):
        raise HTTPException(status_code=404, detail="object not found")
    blob = objects.get(object_id)
    try:
        kek_version, _, _, _ = parse_header(blob)
    except EnvelopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "id": object_id,
        "kek_version": kek_version,
        "stored_bytes": len(blob),
        "current_kek_version": keys.current_version(),
    }


@app.get("/keys")
async def list_keys() -> dict:
    return {"current": keys.current_version(), "versions": keys.versions()}


@app.post("/keys/rotate")
async def rotate_keys() -> dict:
    new_version = keys.rotate()
    return {
        "current": new_version,
        "versions": keys.versions(),
        "note": "existing objects keep their old wrapping until /rewrap; "
        "old key versions are retained so they stay decryptable",
    }


@app.post("/rewrap")
async def rewrap_all() -> dict:
    """Re-wrap every object's DEK under the current master key.

    Each object is rewritten atomically and independently, so an interruption
    leaves a mix of old- and new-version objects — all still decryptable,
    because old key versions are retained.
    """
    current_key = keys.current_key()
    current_version = keys.current_version()
    rewrapped, already_current, failed = 0, 0, {}
    for object_id in objects.list_ids():
        blob = objects.get(object_id)
        try:
            new_blob = rewrap(keys.get, current_key, current_version, blob)
        except EnvelopeError as exc:
            failed[object_id] = str(exc)
            continue
        if new_blob is blob:
            already_current += 1
        else:
            objects.put(object_id, new_blob)
            rewrapped += 1
    status = {
        "current_kek_version": current_version,
        "rewrapped": rewrapped,
        "already_current": already_current,
        "failed": failed,
    }
    if failed:
        raise HTTPException(status_code=500, detail=status)
    return status
