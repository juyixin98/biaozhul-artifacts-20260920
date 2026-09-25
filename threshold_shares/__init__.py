"""Threshold share recovery service (Shamir secret sharing over GF(p)).

Public API:
    split_secret / recover_secret  -- core Shamir scheme
    encode_share / decode_share    -- versioned share (de)serialisation
    split / recover_from_encoded   -- application-level service helpers
    seal_share / unseal_share      -- optional authenticated passphrase wrapping
"""

from .field import FIELD_PRIME, FIELD_NAME, lagrange_interpolate_at_zero
from .scheme import (
    PARAM_VERSION,
    CHUNK_SIZE,
    MAX_TOTAL_SHARES,
    Share,
    split_secret,
    recover_secret,
)
from .encoding import (
    encode_share,
    decode_share,
    share_to_json_object,
    UnsupportedVersionError,
    ShareEncodingError,
)
from .sealing import seal_share, unseal_share, SealingError
from .service import (
    split,
    recover_from_encoded,
    ServiceError,
    ParameterError,
    InsufficientSharesError,
    DuplicateIndexError,
    MixedBatchError,
    InvalidShareEncodingError,
    RecoveredSecretInvalidError,
)

__all__ = [
    "FIELD_PRIME",
    "FIELD_NAME",
    "lagrange_interpolate_at_zero",
    "PARAM_VERSION",
    "CHUNK_SIZE",
    "MAX_TOTAL_SHARES",
    "Share",
    "split_secret",
    "recover_secret",
    "encode_share",
    "decode_share",
    "share_to_json_object",
    "UnsupportedVersionError",
    "ShareEncodingError",
    "seal_share",
    "unseal_share",
    "SealingError",
    "split",
    "recover_from_encoded",
    "ServiceError",
    "ParameterError",
    "InsufficientSharesError",
    "DuplicateIndexError",
    "MixedBatchError",
    "InvalidShareEncodingError",
    "RecoveredSecretInvalidError",
]

__version__ = "1.0.0"
