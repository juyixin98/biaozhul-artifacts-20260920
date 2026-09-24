"""Frozen-parameter loading with real Ed25519 signature verification.

The parameter set is version-frozen: the service refuses to start if the
signature over the canonical JSON of the parameter file does not verify
against the pinned public key. This makes silent parameter drift impossible.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PARAMS_VERSION = "soc-estimator-params/v1"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARAMS_PATH = REPO_ROOT / "params" / "params_v1.json"
DEFAULT_SIG_PATH = REPO_ROOT / "params" / "params_v1.sig"
DEFAULT_PUBKEY_PATH = REPO_ROOT / "keys" / "params_signing_public.pem"


class ParamsIntegrityError(RuntimeError):
    """Raised when the frozen parameter set fails signature verification."""


def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON encoding used for signing and hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def params_digest(params: dict) -> str:
    return hashlib.sha256(canonical_bytes(params)).hexdigest()


def sign_params(params: dict, private_key: Ed25519PrivateKey) -> bytes:
    return private_key.sign(canonical_bytes(params))


def verify_params(params: dict, signature: bytes, public_key: Ed25519PublicKey) -> None:
    public_key.verify(signature, canonical_bytes(params))


@dataclass(frozen=True)
class Params:
    """Typed, immutable view over the frozen parameter set."""

    version: str
    raw: dict
    digest: str

    # --- convenience accessors (units documented in params JSON) ---
    @property
    def n_series_cells(self) -> int:
        return int(self.raw["pack"]["n_series_cells"])

    @property
    def nominal_capacity_ah(self) -> float:
        return float(self.raw["pack"]["nominal_capacity_ah"])

    @property
    def eta_charge(self) -> float:
        return float(self.raw["efficiency"]["eta_charge"])

    @property
    def eta_discharge(self) -> float:
        return float(self.raw["efficiency"]["eta_discharge"])

    @property
    def temp_table_c(self) -> list[float]:
        return [float(x) for x in self.raw["temperature"]["capacity_factor_table"]["temp_c"]]

    @property
    def temp_table_factor(self) -> list[float]:
        return [float(x) for x in self.raw["temperature"]["capacity_factor_table"]["factor"]]

    @property
    def ocv_trusted_range_c(self) -> tuple[float, float]:
        lo, hi = self.raw["temperature"]["ocv_trusted_range_c"]
        return float(lo), float(hi)

    @property
    def ocv_soc(self) -> list[float]:
        return [float(x) for x in self.raw["ocv_table"]["soc"]]

    @property
    def ocv_v_cell(self) -> list[float]:
        return [float(x) for x in self.raw["ocv_table"]["v_cell"]]

    @property
    def rest_current_threshold_a(self) -> float:
        return float(self.raw["rest_detection"]["abs_current_threshold_a"])

    @property
    def min_rest_duration_s(self) -> float:
        return float(self.raw["rest_detection"]["min_rest_duration_s"])

    @property
    def max_interval_s(self) -> float:
        return float(self.raw["gap_handling"]["max_interval_s"])

    @property
    def current_noise_sigma_a(self) -> float:
        return float(self.raw["uncertainty"]["current_noise_sigma_a"])

    @property
    def bias_growth_a_per_sqrt_s(self) -> float:
        return float(self.raw["uncertainty"]["bias_growth_a_per_sqrt_s"])

    @property
    def gap_sigma_bound_a(self) -> float:
        return float(self.raw["uncertainty"]["gap_sigma_bound_a"])

    @property
    def initial_soc_sigma(self) -> float:
        return float(self.raw["uncertainty"]["initial_soc_sigma"])

    @property
    def ocv_soc_sigma(self) -> float:
        return float(self.raw["uncertainty"]["ocv_soc_sigma"])

    @property
    def max_abs_current_a(self) -> float:
        return float(self.raw["limits"]["max_abs_current_a"])

    @property
    def max_samples_per_ingest(self) -> int:
        return int(self.raw["limits"]["max_samples_per_ingest"])

    @property
    def default_replay_window_s(self) -> float:
        return float(self.raw["replay"]["default_replay_window_s"])


def load_params(
    params_path: Path = DEFAULT_PARAMS_PATH,
    sig_path: Path = DEFAULT_SIG_PATH,
    pubkey_path: Path = DEFAULT_PUBKEY_PATH,
) -> Params:
    """Load and cryptographically verify the frozen parameter set."""
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))
    if params.get("params_version") != PARAMS_VERSION:
        raise ParamsIntegrityError(
            f"unsupported params_version {params.get('params_version')!r}; "
            f"this build only accepts {PARAMS_VERSION!r}"
        )
    signature = bytes.fromhex(Path(sig_path).read_text(encoding="utf-8").strip())
    public_key = Ed25519PublicKey.from_public_bytes(
        _read_public_key_bytes(Path(pubkey_path))
    )
    try:
        verify_params(params, signature, public_key)
    except InvalidSignature as exc:
        raise ParamsIntegrityError(
            "parameter signature verification FAILED - refusing to run with "
            "unverified parameters"
        ) from exc
    return Params(version=params["params_version"], raw=params, digest=params_digest(params))


def _read_public_key_bytes(path: Path) -> bytes:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    key = load_pem_public_key(path.read_bytes())
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)
