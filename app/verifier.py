"""Independent verifier.

The verifier takes:

* an export bundle produced by the (possibly compromised) log server,
* the trust anchor (Ed25519 public key, obtained out-of-band),
* optionally an *external* latest checkpoint the verifier previously cached,
* ``expect_tail`` - True when the verifier asked for a tail-inclusive export
  (default); False for bounded interior ranges where checkpoints past the
  slice end are expected, not evidence of truncation.

Verdicts
--------
VALID
    Every record links to the previous one, hashes recompute, and a
    signature-valid checkpoint attests the chain head (or the empty log via a
    seq=0 anchor). Nothing unprovable remains.

TAMPERED
    A hard cryptographic check failed: recomputed record hash mismatch,
    broken/skipped/reordered prev_hash link, bad checkpoint signature
    (forged checkpoint), broken checkpoint chain, or a signed checkpoint
    disagreeing with the record at its seq. This covers deletion,
    modification and re-ordering *that the attacker did not fully rebuild*.

TRUNCATED
    A trusted checkpoint (one validly signed by the anchor key, including the
    verifier-held external one) covers seq N, but records after the verified
    prefix end before N. The latest trusted checkpoint proves records are
    missing.

UNDETERMINED
    All hard checks pass, but a clean tail extends past the latest trusted
    checkpoint. Its content is linked to the anchor (any non-rebuilt edit is
    TAMPERED), yet without a newer anchor the verifier cannot prove the tail
    is complete: deletion of the tail's end or unexported newer records are
    indistinguishable from a genuine short tail.

Trust model: security holds while the Ed25519 signing key stays on the
server and only the public key is distributed. Key theft is out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import chain, keys as keymod
from .canonical import GENESIS_CHECKPOINT_HASH, GENESIS_HASH

VALID = "VALID"
TAMPERED = "TAMPERED"
TRUNCATED = "TRUNCATED"
UNDETERMINED = "UNDETERMINED"


@dataclass
class VerificationResult:
    status: str = VALID
    proven_upto: int = 0
    slice_start: int | None = None
    slice_end: int | None = None
    trusted_checkpoint_seqs: list[int] = field(default_factory=list)
    latest_trusted_seq: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        self.status = TAMPERED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "proven_upto": self.proven_upto,
            "slice_start": self.slice_start,
            "slice_end": self.slice_end,
            "latest_trusted_seq": self.latest_trusted_seq,
            "trusted_checkpoint_seqs": self.trusted_checkpoint_seqs,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _signed_checkpoints(
    checkpoints: list[dict[str, Any]],
    pub,
    result: VerificationResult,
    *,
    origin: str,
) -> list[dict[str, Any]]:
    """Keep only checkpoints with valid signatures; forgery => TAMPERED."""
    valid: list[dict[str, Any]] = []
    for i, cp in enumerate(checkpoints):
        try:
            sig = bytes.fromhex(cp["signature"])
            msg = chain.checkpoint_message(cp)
        except (KeyError, TypeError, ValueError):
            result.fail(f"{origin} checkpoint #{i}: malformed signature/body")
            continue
        if cp.get("version") != keymod.KEY_VERSION:
            result.fail(f"{origin} checkpoint #{i}: unsupported version {cp.get('version')!r}")
            continue
        if keymod.verify_signature(pub, sig, msg):
            valid.append(cp)
        else:
            result.fail(
                f"{origin} checkpoint seq={cp.get('seq')!r}: signature does not "
                "verify against the trust anchor (forged checkpoint)"
            )
    return valid


def _check_checkpoint_chain(
    cps: list[dict[str, Any]], result: VerificationResult, origin: str
) -> None:
    prev_hash = GENESIS_CHECKPOINT_HASH
    prev_seq: int | None = None
    for cp in cps:
        if cp["prev_checkpoint_hash"] != prev_hash:
            result.fail(
                f"{origin} checkpoint seq={cp['seq']}: prev_checkpoint_hash mismatch "
                "(checkpoint deleted or reordered)"
            )
        if prev_seq is not None and cp["seq"] < prev_seq:
            result.fail(
                f"{origin} checkpoint seq={cp['seq']}: checkpoint seq not monotonic "
                "(reordered)"
            )
        prev_hash = chain.checkpoint_hash(cp)
        prev_seq = cp["seq"]


def verify_bundle(
    bundle: dict[str, Any],
    trust_anchor_hex: str,
    external_anchor: dict[str, Any] | None = None,
    expect_tail: bool = True,
) -> VerificationResult:
    """Run all checks and return a structured verdict."""
    result = VerificationResult()
    pub = keymod.load_public_key_hex(trust_anchor_hex)

    records = bundle.get("records", [])
    bundle_cps = bundle.get("checkpoints", [])
    result.slice_start = records[0]["seq"] if records else None
    result.slice_end = records[-1]["seq"] if records else None

    # ----------------------------------------------------------- 1. signatures
    trusted = _signed_checkpoints(bundle_cps, pub, result, origin="bundle")
    external: list[dict[str, Any]] = []
    if external_anchor is not None:
        external = _signed_checkpoints(
            [external_anchor], pub, result, origin="external"
        )

    # Merge by seq; conflicting signed checkpoints at the same seq mean two
    # validly signed but different heads -> cannot both be the real chain.
    by_seq: dict[int, dict[str, Any]] = {}
    for cp in trusted + external:
        s = cp["seq"]
        if s in by_seq and by_seq[s]["record_hash"] != cp["record_hash"]:
            result.fail(
                f"conflicting trusted checkpoints at seq={s}: different record_hash"
            )
        by_seq.setdefault(s, cp)

    # ------------------------------------------------- 2. checkpoint hash chain
    _check_checkpoint_chain(trusted, result, origin="bundle")

    # ------------------------------------------------- 3. record hash chain
    recomputed: dict[int, str] = {}  # seq -> verified hash
    prev_seq: int | None = None
    expected_prev: str | None = None
    slice_start = result.slice_start
    if records is not None and slice_start is not None:
        if slice_start == 1:
            expected_prev = GENESIS_HASH
        else:
            anchor = by_seq.get(slice_start - 1)
            if anchor is None:
                result.warnings.append(
                    f"interior slice starts at seq={slice_start} but no trusted "
                    f"checkpoint at seq={slice_start - 1} anchors its prev_hash; "
                    "start of slice is unanchored"
                )
                expected_prev = records[0].get("prev_hash")
            else:
                expected_prev = anchor["record_hash"]

        for rec in records:
            seq = rec.get("seq")
            if not isinstance(seq, int) or seq < 1:
                result.fail(f"record with invalid seq: {seq!r}")
                break
            if prev_seq is not None:
                if seq == prev_seq:
                    result.fail(f"duplicate seq={seq} (record inserted/reordered)")
                    break
                if seq != prev_seq + 1:
                    result.fail(
                        f"gap in sequence: seq={prev_seq} followed by seq={seq} "
                        "(record deleted)"
                    )
                    break
            if rec.get("prev_hash") != expected_prev:
                result.fail(
                    f"record seq={seq}: prev_hash mismatch (record modified, "
                    "deleted or reordered)"
                )
            recomputed_hash = chain.record_hash(rec)
            if rec.get("hash") != recomputed_hash:
                result.fail(
                    f"record seq={seq}: stored hash != recomputed hash (record modified)"
                )
            recomputed[seq] = recomputed_hash
            expected_prev = recomputed_hash
            prev_seq = seq

    slice_end = result.slice_end

    # --------------------------------------------- 4. checkpoints vs records
    for s, cp in by_seq.items():
        if s == 0:
            if cp["record_hash"] != GENESIS_HASH:
                result.fail("seq=0 checkpoint does not cover the genesis hash")
        elif s in recomputed:
            if cp["record_hash"] != recomputed[s]:
                result.fail(
                    f"trusted checkpoint seq={s} record_hash disagrees with the "
                    "recomputed chain (a covered record was tampered)"
                )
        # s > slice_end handled in step 5 (possible truncation).

    result.trusted_checkpoint_seqs = sorted(by_seq)
    result.latest_trusted_seq = max(by_seq, default=0)

    # Hard failures short-circuit the interpretive verdicts.
    if result.status == TAMPERED:
        return result

    # --------------------------------------------- 5. interpretive verdicts
    end = slice_end if slice_end is not None else 0

    # Where does the cryptographically proven prefix end?  Highest trusted
    # checkpoint whose covered record is present and verified.
    proven = 0
    for s in sorted(by_seq):
        if s == 0 and by_seq[0]["record_hash"] == GENESIS_HASH:
            proven = 0
        elif s in recomputed and by_seq[s]["record_hash"] == recomputed[s]:
            proven = s
    result.proven_upto = proven

    beyond = [s for s in by_seq if s > end]
    if beyond:
        # Trusted coverage extends past what was exported.
        if expect_tail:
            result.status = TRUNCATED
            result.errors.append(
                f"log exports {end} record(s) but a trusted checkpoint covers "
                f"seq={min(beyond)} (latest: seq={max(beyond)}): records were truncated"
            )
            return result
        result.warnings.append(
            f"trusted checkpoints beyond requested slice (up to seq={max(beyond)}) "
            "ignored because expect_tail=False"
        )

    if records and slice_start not in (None, 1) and slice_start - 1 not in by_seq:
        # Interior slice with no anchor at its left boundary: content may be a
        # self-consistent fabrication, so nothing is proven beyond hash shape.
        result.status = UNDETERMINED
        result.warnings.append(
            "cannot prove provenance of this unanchored interior slice"
        )
        return result

    if end > proven:
        result.status = UNDETERMINED
        result.warnings.append(
            f"records seq={proven + 1}..{end} link correctly to the prefix proven "
            f"by the latest trusted checkpoint (seq={proven}), but no newer anchor "
            "exists: their content integrity is checkable, yet completeness is not "
            "decidable - truncation of the tail's end is indistinguishable from a "
            "genuinely short tail"
        )
        return result

    if end == 0 and proven == 0 and 0 not in by_seq:
        result.status = UNDETERMINED
        result.warnings.append(
            "empty export with no genesis anchor: an empty-but-truncated log is "
            "indistinguishable from a genuinely empty log"
        )
        return result

    result.status = VALID
    if end == 0:
        result.warnings.append("empty log attested by a trusted seq=0 checkpoint")
    return result
