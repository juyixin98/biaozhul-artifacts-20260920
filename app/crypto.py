"""Cryptographic binding of quotes to immutable pool snapshots.

Every quote carries:

* ``pool_digest``  - SHA-256 over the canonical serialization of the pool
  snapshot (tokens, fee tier, current tick, every position).  A different
  snapshot always yields a different digest; reserves/positions can never be
  silently changed under a quote.
* ``quote_digest`` - SHA-256 over the pool digest plus every result field and
  every per-segment evidence record, so the quote is tamper-evident.

Canonical serialization is deterministic: ``json.dumps(..., sort_keys=True,
separators=(",", ":"), ensure_ascii=False)`` over plain integers/strings.
These are real SHA-256 computations performed with the Python standard
library ``hashlib`` (OpenSSL-backed at runtime).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Pool, QuoteResult


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_pool(pool: Pool) -> str:
    """SHA-256 digest binding the immutable pool snapshot."""
    return sha256_hex(canonical_json(pool.canonical()))


def digest_quote(pool_digest: str, result: QuoteResult) -> str:
    """SHA-256 digest binding all quote evidence to the snapshot digest."""
    payload = {
        "pool_digest": pool_digest,
        "direction": result.direction,
        "input_token": result.input_token,
        "output_token": result.output_token,
        "amount_in": result.amount_in,
        "amount_out": result.amount_out,
        "unspent_input": result.unspent_input,
        "fee_paid": result.fee_paid,
        "curve_input_total": result.curve_input_total,
        "current_tick_before": result.current_tick_before,
        "current_tick_after": result.current_tick_after,
        "sqrt_price_before": result.sqrt_price_before,
        "sqrt_price_after": result.sqrt_price_after,
        "stop_reason": result.stop_reason,
        "engine_version": result.engine_version,
        "segments": [
            {
                "index": s.index,
                "lower_tick": s.lower_tick,
                "upper_tick": s.upper_tick,
                "liquidity": s.liquidity,
                "start_sqrt_price": s.start_sqrt_price,
                "end_sqrt_price": s.end_sqrt_price,
                "input_token": s.input_token,
                "gross_input": s.gross_input,
                "curve_input": s.curve_input,
                "fee_input": s.fee_input,
                "output_token": s.output_token,
                "output": s.output,
                "crossed": s.crossed,
                "note": s.note,
            }
            for s in result.segments
        ],
    }
    return sha256_hex(canonical_json(payload))


def verify_quote_digest(result: QuoteResult) -> bool:
    """Recompute the quote digest from the evidence and compare."""
    return digest_quote(result.pool_digest, result) == result.quote_digest
