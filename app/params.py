"""Frozen parameter manifest: load, canonicalize, hash, and HMAC-verify.

The parameter set is version-frozen. Integrity is enforced with a real
HMAC-SHA256 signature over the canonical manifest bytes; the service refuses
to start on tampered parameters. The signing key comes from the
``BTE_PARAM_KEY`` environment variable (hex) or, for local development only,
from ``config/param_key.dev.hex``.
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import canonical_bytes, sha256_hex


class ParamIntegrityError(RuntimeError):
    """Raised when the frozen parameter manifest fails integrity checks."""


@dataclass(frozen=True)
class SignedManifest:
    manifest: dict[str, Any]
    sha256: str
    signature: str


def sign_manifest(manifest: dict[str, Any], key: bytes) -> str:
    """Real HMAC-SHA256 signature of the canonical manifest."""
    return hmac.new(key, canonical_bytes(manifest), "sha256").hexdigest()


def load_and_verify(param_dir: str | Path, key: bytes) -> SignedManifest:
    """Load params.json + params.sig and verify the HMAC signature.

    Raises ParamIntegrityError on any mismatch, missing file, or malformed
    content. Never silently falls back to unsigned parameters.
    """
    param_dir = Path(param_dir)
    manifest_path = param_dir / "params.json"
    sig_path = param_dir / "params.sig"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ParamIntegrityError(f"parameter manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ParamIntegrityError(f"parameter manifest is not valid JSON: {exc}") from exc
    try:
        signature = sig_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ParamIntegrityError(f"parameter signature not found: {sig_path}") from exc

    expected = sign_manifest(manifest, key)
    if not hmac.compare_digest(expected, signature):
        raise ParamIntegrityError(
            "parameter manifest HMAC verification failed — manifest was "
            "modified after freezing, or the wrong key is in use"
        )
    return SignedManifest(manifest=manifest, sha256=sha256_hex(manifest), signature=signature)


@dataclass(frozen=True)
class Params:
    """Typed, immutable view of the frozen manifest."""

    version: str
    nominal_capacity_ah: float
    sample_period_s: float
    gap_threshold_s: float
    coulomb_eff_charge: float
    coulomb_eff_discharge: float
    temp_ref_c: float
    capacity_temp_points_c: tuple[float, ...]
    capacity_temp_factor: tuple[float, ...]
    ocv_soc_points: tuple[float, ...]
    ocv_volts: tuple[float, ...]
    ocv_dv_dt_v_per_k: float
    rest_current_a: float
    rest_exit_current_a: float
    rest_min_duration_s: float
    rest_voltage_max_step_v: float
    rest_voltage_max_span_v: float
    temp_out_of_range_invalidates_rest: bool
    sigma0: float
    sigma_rw_per_sqrt_hr: float
    i_bias_bound_a: float
    i_unknown_bound_a: float
    sigma_ocv: float
    replay_horizon_s: float
    soc_min: float
    soc_max: float

    @classmethod
    def from_manifest(cls, m: dict[str, Any]) -> "Params":
        try:
            cap = m["capacity"]
            samp = m["sampling"]
            temp = m["temperature"]
            ocv = m["ocv_table_25c"]
            rest = m["rest"]
            unc = m["uncertainty"]
            replay = m["replay"]
            bounds = m["soc_bounds"]
            params = cls(
                version=str(m["version"]),
                nominal_capacity_ah=float(cap["nominal_ah"]),
                sample_period_s=float(samp["nominal_period_s"]),
                gap_threshold_s=float(samp["gap_threshold_s"]),
                coulomb_eff_charge=float(cap["coulomb_eff_charge"]),
                coulomb_eff_discharge=float(cap["coulomb_eff_discharge"]),
                temp_ref_c=float(temp["ref_c"]),
                capacity_temp_points_c=tuple(float(x) for x in temp["capacity_factor_points_c"]),
                capacity_temp_factor=tuple(float(x) for x in temp["capacity_factor"]),
                ocv_soc_points=tuple(float(x) for x in ocv["soc"]),
                ocv_volts=tuple(float(x) for x in ocv["volts"]),
                ocv_dv_dt_v_per_k=float(temp["ocv_dv_dt_v_per_k"]),
                rest_current_a=float(rest["current_a"]),
                rest_exit_current_a=float(rest["exit_current_a"]),
                rest_min_duration_s=float(rest["min_duration_s"]),
                rest_voltage_max_step_v=float(rest["voltage_max_step_v"]),
                rest_voltage_max_span_v=float(rest["voltage_max_span_v"]),
                temp_out_of_range_invalidates_rest=bool(rest["temp_out_of_range_invalidates"]),
                sigma0=float(unc["sigma0"]),
                sigma_rw_per_sqrt_hr=float(unc["sigma_rw_per_sqrt_hr"]),
                i_bias_bound_a=float(unc["i_bias_bound_a"]),
                i_unknown_bound_a=float(unc["i_unknown_bound_a"]),
                sigma_ocv=float(unc["sigma_ocv"]),
                replay_horizon_s=float(replay["horizon_s"]),
                soc_min=float(bounds["min"]),
                soc_max=float(bounds["max"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ParamIntegrityError(f"parameter manifest is missing/invalid fields: {exc}") from exc
        params._validate()
        return params

    def _validate(self) -> None:
        if not (self.nominal_capacity_ah > 0):
            raise ParamIntegrityError("nominal capacity must be positive")
        if not (self.soc_min < self.soc_max):
            raise ParamIntegrityError("soc_bounds.min must be < soc_bounds.max")
        if len(self.capacity_temp_points_c) != len(self.capacity_temp_factor):
            raise ParamIntegrityError("capacity temperature table length mismatch")
        if list(self.capacity_temp_points_c) != sorted(self.capacity_temp_points_c):
            raise ParamIntegrityError("capacity temperature points must be ascending")
        if len(self.ocv_soc_points) != len(self.ocv_volts):
            raise ParamIntegrityError("OCV table length mismatch")
        if list(self.ocv_volts) != sorted(self.ocv_volts):
            raise ParamIntegrityError("OCV volts must be ascending (monotonic OCV-SOC curve)")
        if not (0.0 <= self.ocv_soc_points[0] and self.ocv_soc_points[-1] <= 1.0):
            raise ParamIntegrityError("OCV soc points must lie in [0, 1]")
