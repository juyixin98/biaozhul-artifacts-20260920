"""Minimal CBOR codec and Solidity metadata-tail parser.

``solc`` appends an auxiliary-deployment ("auxdata") CBOR blob to deployed
runtime bytecode.  Its layout is::

    [cbor-encoded map] [2-byte big-endian cbor length] [0x00 0x33]

Typical map keys (solc >= 0.6): ``ipfs`` (32 bytes), ``solc`` (3-byte
version), ``bzzr0`` / ``bzzr1`` (32 bytes), ``keccak256`` (32 bytes),
``experimental`` (bool).

We implement only the subset of CBOR needed to decode/encode these blobs;
a decode failure means the tail is malformed and is reported as such rather
than guessed at.
"""

from __future__ import annotations


class CBORError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

def _read_header(data: bytes, pos: int) -> tuple[int, int]:
    major = data[pos] >> 5
    info = data[pos] & 0x1F
    pos += 1
    if info < 24:
        value = info
    elif info == 24:
        value = data[pos]
        pos += 1
    elif info == 25:
        value = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2
    elif info == 26:
        value = int.from_bytes(data[pos:pos + 4], "big")
        pos += 4
    elif info == 27:
        value = int.from_bytes(data[pos:pos + 8], "big")
        pos += 8
    else:
        raise CBORError(f"unsupported additional info {info}")
    return major, value, pos  # type: ignore[return-value]


def cbor_decode(data: bytes):
    value, pos = _decode_value(data, 0)
    if pos != len(data):
        raise CBORError(f"{len(data) - pos} trailing CBOR bytes")
    return value


def _decode_value(data: bytes, pos: int):
    major, info, pos = _read_header(data, pos)

    if major == 0:  # uint
        return info, pos
    if major == 1:  # nint
        return -1 - info, pos
    if major == 2:  # byte string
        raw = data[pos:pos + info]
        if len(raw) != info:
            raise CBORError("truncated byte string")
        return raw, pos + info
    if major == 3:  # text string
        raw = data[pos:pos + info]
        if len(raw) != info:
            raise CBORError("truncated text string")
        return raw.decode("utf-8"), pos + info
    if major == 4:  # array
        arr = []
        for _ in range(info):
            v, pos = _decode_value(data, pos)
            arr.append(v)
        return arr, pos
    if major == 5:  # map
        result: dict = {}
        for _ in range(info):
            k, pos = _decode_value(data, pos)
            v, pos = _decode_value(data, pos)
            if not isinstance(k, str):
                raise CBORError("non-text map key")
            result[k] = v
        return result, pos
    if major == 7:
        if info == 20:
            return False, pos
        if info == 21:
            return True, pos
        if info == 22:
            return None, pos
    raise CBORError(f"unsupported CBOR major type {major} info {info}")


# ---------------------------------------------------------------------------
# Encoder (strict subset)
# ---------------------------------------------------------------------------

def _head(major: int, n: int) -> bytes:
    m = major << 5
    if n < 24:
        return bytes([m | n])
    if n < 256:
        return bytes([m | 24, n])
    if n < 65536:
        return bytes([m | 25]) + n.to_bytes(2, "big")
    if n < 2 ** 32:
        return bytes([m | 26]) + n.to_bytes(4, "big")
    return bytes([m | 27]) + n.to_bytes(8, "big")


def cbor_encode(obj) -> bytes:
    if isinstance(obj, bool):
        return b"\xf5" if obj else b"\xf4"
    if obj is None:
        return b"\xf6"
    if isinstance(obj, int) and obj >= 0:
        return _head(0, obj)
    if isinstance(obj, bytes):
        return _head(2, len(obj)) + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return _head(3, len(b)) + b
    if isinstance(obj, dict):
        out = _head(5, len(obj))
        for k in obj:
            if not isinstance(k, str):
                raise CBORError("only text keys supported")
        for k in sorted(obj):  # canonical: sorted keys
            out += cbor_encode(k) + cbor_encode(obj[k])
        return out
    if isinstance(obj, (list, tuple)):
        out = _head(4, len(obj))
        for item in obj:
            out += cbor_encode(item)
        return out
    raise CBORError(f"cannot CBOR-encode {type(obj)!r}")


# ---------------------------------------------------------------------------
# Solidity auxiliary data tail
# ---------------------------------------------------------------------------

_TAIL_SENTINEL = bytes([0x00, 0x33])


def parse_metadata_tail(runtime_hex: str) -> dict:
    """Split a runtime bytecode hex string into body + decoded CBOR metadata.

    Placeholder tokens (``__$...$__``) may occur in the unlinked body, so the
    string is not hex-decoded wholesale; the tail is located via the trailing
    ``0x0033`` sentinel and decoded independently.

    Returns a dict with keys:
      ``body_hex``      – runtime code with the auxdata tail removed
      ``cbor_raw``      – raw CBOR bytes
      ``cbor_map``      – decoded metadata map (bytes values stay bytes)
      ``raw_tail``      – exact trailing bytes (cbor + length + sentinel)
      ``length_field``  – declared CBOR length
    Raises ``CBORError`` when the tail does not parse.
    """
    rt = runtime_hex[2:] if runtime_hex.startswith("0x") else runtime_hex
    if len(rt) % 2:
        raise CBORError("odd-length bytecode")
    if not rt.endswith("0033"):
        raise CBORError("missing 0x0033 auxdata sentinel")
    try:
        length = int(rt[-8:-4], 16)
    except ValueError as exc:
        raise CBORError("invalid auxdata length field") from exc
    # Layout: [cbor payload = length bytes] [length: 2 bytes] [0x0033: 2 bytes]
    payload_nibbles = length * 2
    total_tail_nibbles = payload_nibbles + 8
    if length == 0 or total_tail_nibbles > len(rt):
        raise CBORError("invalid auxdata length field")
    cbor_hex = rt[len(rt) - total_tail_nibbles:len(rt) - 8]
    try:
        cbor_raw = bytes.fromhex(cbor_hex)
        raw_tail = bytes.fromhex(rt[len(rt) - total_tail_nibbles:])
    except ValueError as exc:
        raise CBORError("auxdata tail is not hex") from exc
    meta = cbor_decode(cbor_raw)
    if not isinstance(meta, dict):
        raise CBORError("auxdata CBOR is not a map")
    body_hex = rt[:len(rt) - total_tail_nibbles]
    return {
        "body_hex": body_hex,
        "cbor_raw": cbor_raw,
        "cbor_map": meta,
        "raw_tail": raw_tail,
        "length_field": length,
    }


def build_metadata_tail(meta: dict) -> bytes:
    """Encode a metadata map and append length + sentinel."""
    payload = cbor_encode(meta)
    return payload + len(payload).to_bytes(2, "big") + _TAIL_SENTINEL
