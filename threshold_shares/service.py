"""Application service layer: orchestrates encoding + scheme with strict checks.

Recovery is fail-closed. The service decodes and validates EVERY supplied
share before interpolation, in this order:

  1. every item must decode (corrupt/truncated/non-canonical -> error);
  2. all shares must carry identical parameters (threshold, total, block
     count) and version -- a "mixed batch" is rejected, not partially used;
  3. x coordinates must be distinct (duplicate abscissa -> error);
  4. at least `threshold` valid shares must remain, else recovery is refused.

No secret material is ever placed in an exception message. The one integrity
signal available without an authenticated scheme is the optional
``expected_fingerprint`` (sha256 of the original secret, produced at split
time): if the recomputed fingerprint differs the recovered bytes are
reported as invalid. That detects tampering but cannot say *which* share is
malicious -- the scheme is unauthenticated.
"""

import hashlib

from . import field
from .encoding import decode_share, encode_share, share_to_json_object, ShareEncodingError, UnsupportedVersionError
from .scheme import PARAM_VERSION, split_secret

FINGERPRINT_ALGO = "sha256"


class ServiceError(Exception):
    code = "service_error"
    http_status = 400

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or {}


class ParameterError(ServiceError):
    code = "invalid_parameters"


class InsufficientSharesError(ServiceError):
    code = "insufficient_shares"


class DuplicateIndexError(ServiceError):
    code = "duplicate_share_index"


class MixedBatchError(ServiceError):
    code = "mixed_batch"


class InvalidShareEncodingError(ServiceError):
    code = "invalid_share_encoding"


class RecoveredSecretInvalidError(ServiceError):
    code = "recovered_secret_invalid"
    http_status = 422


def fingerprint(secret: bytes) -> str:
    return f"{FINGERPRINT_ALGO}:{hashlib.sha256(secret).hexdigest()}"


def split(secret: bytes, threshold: int, total: int) -> dict:
    """Split a secret and return versioned shares plus reference metadata."""
    if isinstance(secret, str):
        raise ParameterError("secret must be supplied as bytes (HTTP layer encodes text/base64)")
    if not isinstance(secret, (bytes, bytearray)):
        raise ParameterError("secret must be bytes")
    if not secret:
        raise ParameterError("secret must not be empty")
    try:
        shares = split_secret(bytes(secret), threshold, total)
    except ValueError as exc:
        raise ParameterError(str(exc)) from exc

    return {
        "version": PARAM_VERSION,
        "field": field.FIELD_NAME,
        "threshold": threshold,
        "total": total,
        "block_count": len(shares[0].ys),
        "shares": [encode_share(s, threshold, total) for s in shares],
        "shares_json": [share_to_json_object(s, threshold, total) for s in shares],
        "secret_fingerprint": fingerprint(bytes(secret)),
    }


def _decode_all(encoded_shares):
    decoded = []
    errors = []
    for position, item in enumerate(encoded_shares):
        try:
            share, threshold, total = decode_share(item)
        except UnsupportedVersionError as exc:
            errors.append({"position": position, "reason": f"unsupported version: {exc}"})
        except ShareEncodingError as exc:
            errors.append({"position": position, "reason": str(exc)})
        else:
            decoded.append({"position": position, "share": share, "threshold": threshold, "total": total})
    if errors:
        # Never feed a batch we could not fully parse into interpolation.
        raise InvalidShareEncodingError(
            f"{len(errors)} of {len(encoded_shares)} share(s) failed to decode; recovery refused",
            details={"errors": errors},
        )
    return decoded


def recover_from_encoded(encoded_shares, *, expected_fingerprint=None) -> dict:
    """Validate and recover; raises a ServiceError subclass on every refusal."""
    if not isinstance(encoded_shares, (list, tuple)):
        raise ParameterError("'shares' must be a list")
    if not encoded_shares:
        raise ParameterError("'shares' must not be empty")

    decoded = _decode_all(encoded_shares)

    # --- mixed-batch detection -------------------------------------------------
    reference = decoded[0]
    reference_key = (reference["threshold"], reference["total"], len(reference["share"].ys))
    mismatches = []
    for item in decoded[1:]:
        key = (item["threshold"], item["total"], len(item["share"].ys))
        if key != reference_key:
            mismatches.append(
                {
                    "position": item["position"],
                    "threshold": item["threshold"],
                    "total": item["total"],
                    "block_count": len(item["share"].ys),
                }
            )
    if mismatches:
        raise MixedBatchError(
            "shares belong to different parameter sets (threshold/total/size); "
            "cannot combine them in one recovery",
            details={
                "expected": {
                    "threshold": reference["threshold"],
                    "total": reference["total"],
                    "block_count": reference_key[2],
                },
                "mismatches": mismatches,
            },
        )

    threshold = reference["threshold"]
    total = reference["total"]
    block_count = reference_key[2]

    # --- duplicate x detection -------------------------------------------------
    seen = {}
    duplicates = []
    for item in decoded:
        x = item["share"].x
        if x in seen:
            duplicates.append({"x": x, "positions": [seen[x], item["position"]]})
        else:
            seen[x] = item["position"]
    if duplicates:
        raise DuplicateIndexError(
            "duplicate share index (x coordinate) in batch; holders must submit distinct shares",
            details={"duplicates": duplicates},
        )

    # --- threshold enforcement -------------------------------------------------
    if len(decoded) < threshold:
        raise InsufficientSharesError(
            f"recovery requires at least {threshold} distinct shares, received {len(decoded)}",
            details={
                "required": threshold,
                "received": len(decoded),
                "missing": threshold - len(decoded),
                "total": total,
            },
        )

    # Exactly threshold shares participate; surplus valid shares are ignored but
    # reported back for transparency.
    chosen = decoded[:threshold]
    unused_positions = [item["position"] for item in decoded[threshold:]]

    from .scheme import recover_secret

    try:
        secret = recover_secret([item["share"] for item in chosen], block_count)
    except ValueError as exc:
        # Structural inconsistency (length header / padding / block space)
        # reached the scheme. With unauthenticated shares this flags that
        # something is wrong but cannot name the participant responsible.
        details = {"used_positions": [item["position"] for item in chosen]}
        if expected_fingerprint is not None:
            details["expected_fingerprint"] = expected_fingerprint
        raise RecoveredSecretInvalidError(
            f"reconstruction produced an invalid secret ({exc}); one or more shares are wrong "
            "or modified -- the unauthenticated scheme cannot identify which participant is malicious",
            details=details,
        ) from exc

    recovered_fp = fingerprint(secret)
    result = {
        "version": PARAM_VERSION,
        "field": field.FIELD_NAME,
        "threshold": threshold,
        "total": total,
        "used_positions": [item["position"] for item in chosen],
        "used_indices": [item["share"].x for item in chosen],
        "unused_positions": unused_positions,
        "valid_share_count": len(decoded),
        "secret_b64": _b64encode(secret),
        "secret_length": len(secret),
        "secret_fingerprint": recovered_fp,
    }
    if expected_fingerprint is not None:
        result["fingerprint_matches"] = expected_fingerprint == recovered_fp
        if not result["fingerprint_matches"]:
            # The secret is returned only explicitly never here; surface positions
            # so the caller can see which inputs were used.
            raise RecoveredSecretInvalidError(
                "recovered secret fingerprint does not match the expected one; "
                "one or more shares are wrong or modified (the unauthenticated "
                "scheme cannot identify which participant is malicious)",
                details={
                    "expected_fingerprint": expected_fingerprint,
                    "recovered_fingerprint": recovered_fp,
                    "used_positions": result["used_positions"],
                },
            )
    return result


def _b64encode(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")
