"""Core provenance verification engine.

Given (1) a source package, (2) a compilation configuration and (3) an
already-generated compiler output plus optional deployed runtime code, it
checks — all with real recomputation:

* package containment and source paths;
* per-source Keccak-256 digests (source == what was actually compiled);
* compiler version / optimizer / EVM version recorded in the output;
* the CBOR auxdata commitment (metadata hash recomputed and compared,
  ``solc`` version bytes cross-checked);
* library placeholder scheme, link-reference offsets and linked addresses;
* deployed runtime code split into metadata vs library slots vs real code.

Verdicts: EXACT_MATCH, RULE_MATCH or MISMATCH. No difference is silently
swallowed: every check produces a recorded finding.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from . import bytecode
from .bytecode import PLACEHOLDER_RE
from .cbor import CBORError
from .crypto import (
    canonical_json,
    chain_hash,
    eip55_checksum,
    json_digest,
    keccak256,
    verify_eip55,
)
from .fixtures import iter_artifacts
from .models import ContractReport, Finding, FINDING_CODES, Severity, Verdict
from .package_safety import UnsafePackage, extract_zip_safe, normalize_member_path


@dataclass
class _Accum:
    findings: list[Finding] = field(default_factory=list)

    def add(self, code: str, message: str | None = None, detail: dict | None = None,
            severity_override: Severity | None = None) -> Finding:
        sev, default_msg = FINDING_CODES[code]
        if severity_override is not None:
            sev = severity_override
        f = Finding(code=code, severity=sev,
                    message=message or default_msg, detail=detail or {})
        self.findings.append(f)
        return f

    def has(self, severity: Severity) -> bool:
        return any(f.severity is severity for f in self.findings)

    def codes(self) -> set[str]:
        return {f.code for f in self.findings}


def _verdict(findings: list[Finding]) -> Verdict:
    if any(f.severity is Severity.ERROR for f in findings):
        return Verdict.MISMATCH
    if any(f.severity is Severity.ALLOWED for f in findings):
        return Verdict.RULE
    return Verdict.EXACT


def _norm_version_bytes(version: str) -> bytes:
    core = version.split("+", 1)[0].split("-", 1)[0]
    return bytes(int(x) for x in core.split("."))


def _resolve_source(ref: str, files: dict[str, bytes]) -> str | None:
    norm = normalize_member_path(ref)
    if norm in files:
        return norm
    candidates = [p for p in files if p == norm or p.endswith("/" + norm)]
    if len(candidates) == 1:
        return candidates[0]
    return None


def verify_job(
    package_bytes: bytes,
    config: dict,
    compiler_output: dict,
    expected_runtime: dict[str, str] | None = None,
    only_fqns: list[str] | None = None,
) -> dict:
    """Run a full verification. Always returns a result document; a rejected
    package yields ``status='REJECTED'``."""
    job_id = uuid.uuid4().hex
    started = time.time()
    result: dict = {
        "job_id": job_id,
        "submitted_at": started,
        "config": config,
        "contracts": [],
        "package_digests": {},
        "warnings": [],
    }

    # --- Stage 0: containment ------------------------------------------------
    try:
        files = extract_zip_safe(package_bytes)
    except UnsafePackage as exc:
        result["status"] = "REJECTED"
        result["verdict"] = Verdict.MISMATCH.value
        result["error"] = str(exc)
        result["contracts"] = [{
            "fqn": None,
            "verdict": Verdict.MISMATCH.value,
            "findings": [Finding.from_code("UNSAFE_PATH", str(exc)).model_dump()],
        }]
        return _finalize(result)

    result["package_digests"] = {
        p: {"sha256": __import__("hashlib").sha256(b).hexdigest(),
            "keccak256": "0x" + keccak256(b).hex()}
        for p, b in sorted(files.items())
    }
    script_members = [p for p in files if p.endswith((".sh", ".bat", ".cmd", ".py", ".js"))]

    artifacts = list(iter_artifacts(compiler_output))
    if only_fqns:
        wanted = set(only_fqns)
        artifacts = [(f, a) for f, a in artifacts if f in wanted]

    all_referenced: set[str] = set()
    contract_reports: list[ContractReport] = []
    for fqn, art in artifacts:
        report = _verify_contract(fqn, art, files, config,
                                  (expected_runtime or {}).get(fqn))
        contract_reports.append(report)
        try:
            meta = json.loads(art.get("metadata", ""))
            for p in meta.get("sources", {}):
                resolved = _resolve_source(p, files)
                if resolved:
                    all_referenced.add(resolved)
        except Exception:
            pass

    # Extra files (transparency only; e.g. the deliberately un-run build.sh)
    extra = sorted(set(files) - all_referenced)
    if extra:
        for p in extra:
            f = Finding.from_code(
                "SOURCE_EXTRA",
                message=f"package member not referenced by compiler output: {p}",
                detail={"path": p},
            )
            result["warnings"].append(f.model_dump())
    if script_members:
        note = Finding.from_code(
            "SCRIPT_NOT_EXECUTED",
            message=("executable/script members were treated as inert data; "
                     f"none was executed: {', '.join(sorted(script_members))}"),
            detail={"members": sorted(script_members)},
        )
        result.setdefault("package_findings", []).append(note.model_dump())

    result["contracts"] = [r.model_dump() for r in contract_reports]
    result["status"] = "OK"
    verdicts = [r.verdict for r in contract_reports]
    if any(v is Verdict.MISMATCH for v in verdicts):
        result["verdict"] = Verdict.MISMATCH.value
    elif any(v is Verdict.RULE for v in verdicts):
        result["verdict"] = Verdict.RULE.value
    else:
        result["verdict"] = Verdict.EXACT.value
    return _finalize(result)


def _verify_contract(fqn: str, art: dict, files: dict[str, bytes],
                     config: dict, observed_runtime_hex: str | None) -> ContractReport:
    acc = _Accum()
    deployed = art.get("evm", {}).get("deployedBytecode", {})
    object_hex_raw = deployed.get("object", "")
    link_refs = deployed.get("linkReferences", {})

    # --- metadata document ---------------------------------------------------
    try:
        meta = json.loads(art["metadata"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        acc.add("METADATA_MALFORMED", str(exc))
        return ContractReport(fqn=fqn, verdict=Verdict.MISMATCH, findings=acc.findings)

    # --- sources: path containment + digest equality -------------------------
    referenced: list[str] = []
    meta_sources = meta.get("sources", {})
    for src_path, descriptor in sorted(meta_sources.items()):
        try:
            normalize_member_path(src_path)
        except UnsafePackage as exc:
            acc.add("UNSAFE_PATH", str(exc), {"path": src_path})
            continue
        resolved = _resolve_source(src_path, files)
        if resolved is None:
            acc.add("SOURCE_MISSING", detail={"path": src_path})
            continue
        referenced.append(resolved)
        want = (descriptor.get("keccak256") or "").lower()
        got = "0x" + keccak256(files[resolved]).hex()
        if want != got:
            acc.add("SOURCE_DIGEST_DIFF",
                    f"source {resolved}: digest does not match compiler output",
                    {"path": resolved, "recorded": want, "recomputed": got})
    if not acc.has(Severity.ERROR) and referenced:
        acc.add("SOURCE_DIGEST_OK",
                detail={"paths": referenced,
                        "digests": {p: "0x" + keccak256(files[p]).hex()
                                    for p in referenced}})
        acc.add("PATH_OK", detail={"compilation_target":
                                   meta.get("settings", {}).get("compilationTarget")})

    # --- configuration vs recorded settings ----------------------------------
    _check_config(acc, meta, config)

    # --- auxdata tail parse + hash commitment --------------------------------
    tail_info = {}
    parsed_tail = None
    try:
        clean_obj = object_hex_raw[2:] if object_hex_raw.startswith("0x") else object_hex_raw
        parsed = bytecode.split_tail(clean_obj)[1]
        parsed_tail = parsed
        tail_info = {
            "length_field": parsed["length_field"],
            "fields": {k: bytecode._tail_val(v)
                       for k, v in sorted(parsed["cbor_map"].items())},
        }
        acc.add("METADATA_TAIL_PARSED", detail=tail_info)
    except CBORError as exc:
        acc.add("METADATA_TAIL_MALFORMED", str(exc))
    except bytecode.BytecodeFormat as exc:
        acc.add("RUNTIME_ODD_HEX", str(exc))

    if parsed_tail is not None:
        meta_bytes = canonical_json(meta)
        cmap = parsed_tail["cbor_map"]
        hash_ok = False
        if "ipfs" in cmap:
            import hashlib
            hash_ok = hashlib.sha256(meta_bytes).digest() == cmap["ipfs"]
            want = "0x" + hashlib.sha256(meta_bytes).hexdigest()
            got = "0x" + (cmap["ipfs"].hex() if isinstance(cmap["ipfs"], bytes) else "")
            key = "ipfs"
        elif "bzzr1" in cmap or "bzzr0" in cmap:
            import hashlib
            key = "bzzr1" if "bzzr1" in cmap else "bzzr0"
            hash_ok = hashlib.sha256(meta_bytes).digest() == cmap[key]
            want = "0x" + hashlib.sha256(meta_bytes).hexdigest()
            got = "0x" + cmap[key].hex()
        elif "keccak256" in cmap:
            hash_ok = keccak256(meta_bytes) == cmap["keccak256"]
            want = "0x" + keccak256(meta_bytes).hex()
            got = "0x" + cmap["keccak256"].hex()
            key = "keccak256"
        else:
            key, want, got = "(none)", None, None
        if hash_ok:
            acc.add("METADATA_HASH_OK", detail={"hash_kind": key, "commitment": got})
        else:
            acc.add("METADATA_HASH_DIFF",
                    "auxdata metadata commitment does not match embedded metadata",
                    {"hash_kind": key, "recorded": got, "recomputed": want})
        # solc version bytes cross-check
        recorded_version = meta.get("compiler", {}).get("version", "")
        if "solc" in cmap and isinstance(cmap["solc"], bytes):
            if cmap["solc"] != _norm_version_bytes(recorded_version):
                acc.add("METADATA_TAIL_DIFF_OTHER",
                        "auxdata solc version bytes disagree with metadata compiler.version",
                        {"auxdata": "0x" + cmap["solc"].hex(),
                         "metadata": recorded_version})

    # --- placeholder / link reference integrity (unlinked reference) ---------
    slot_evidence: list[dict] = []
    if parsed_tail is not None:
        try:
            body = parsed_tail["body_hex"]
            problems = bytecode.validate_declared_slots(link_refs, body)
            for p in problems:
                acc.add(p["code"], detail=p)
            if not problems:
                acc.add("PLACEHOLDER_SCHEME_OK",
                        detail={"slots": bytecode.link_slots_from_refs(link_refs)})
        except Exception as exc:  # defensive: malformed structures are reported
            acc.add("LINK_REF_BAD", str(exc))

    # --- configured libraries -------------------------------------------------
    cfg_libs = config.get("libraries", {}) or {}
    for lib_fqn, address in cfg_libs.items():
        if not verify_eip55(address):
            acc.add("ADDRESS_BAD_CHECKSUM",
                    f"library {lib_fqn}: address is not EIP-55 checksummed",
                    {"fqn": lib_fqn, "address": address,
                     "expected_checksum": eip55_checksum(address)
                     if address.lower().startswith("0x") and len(address) == 42 else None})
    if cfg_libs and "ADDRESS_BAD_CHECKSUM" not in acc.codes():
        acc.add("ADDRESS_CHECKSUM_OK", detail={"libraries": cfg_libs})

    # --- deployed runtime comparison -----------------------------------------
    body_digests = {}
    if observed_runtime_hex:
        try:
            comp = bytecode.compare_runtime(object_hex_raw, observed_runtime_hex)
        except bytecode.BytecodeFormat as exc:
            acc.add("RUNTIME_ODD_HEX", str(exc))
            comp = None
        except CBORError as exc:
            acc.add("METADATA_TAIL_MALFORMED",
                    f"deployed runtime auxdata does not parse: {exc}")
            comp = None

        if comp is not None:
            body_digests = {
                "normalized_body_digest_expected": comp.get("expected_body_digest"),
                "normalized_body_digest_observed": comp.get("observed_body_digest"),
            }
            if not comp["length_ok"]:
                ref_clean = object_hex_raw[2:] if object_hex_raw.startswith("0x") else object_hex_raw
                acc.add("RUNTIME_LEN_DIFF", detail={
                    "expected_nibbles": len(ref_clean),
                    "observed_nibbles": len(bytecode.clean_hex(observed_runtime_hex, "observed")),
                    "delta_nibbles": comp.get("length_diff_nibbles"),
                })
            if comp["slot_misalign"]:
                acc.add("LINK_REF_BAD",
                        "library link slots are not at identical offsets",
                        detail=comp["slot_misalign"])
            if comp["observed_unresolved"]:
                acc.add("PLACEHOLDER_UNRESOLVED",
                        "deployed runtime still contains library placeholders",
                        detail={"tokens": comp["observed_unresolved"]})
            for d in comp["tail_diffs"]:
                if d["kind"] == "content-hash":
                    acc.add("METADATA_TAIL_DIFF_HASH",
                            f"auxdata {d['key']} differs (embedded metadata hash)",
                            d)
                else:
                    acc.add("METADATA_TAIL_DIFF_OTHER",
                            f"auxdata field {d['key']} differs", d)
            if comp["body_diffs"]:
                sample = comp["body_diffs"][:20]
                acc.add("BODY_DIFF",
                        f"{len(comp['body_diffs'])} code nibbles differ outside "
                        "metadata and library slots",
                        {"count": len(comp["body_diffs"]),
                         "first_nibble_offsets": sample})

            # Per-slot address accounting
            declared = {k.lower(): v for k, v in cfg_libs.items()}
            for s in comp["slots"]:
                slot_fqn = None
                for ref in bytecode.link_slots_from_refs(link_refs):
                    if ref["start_byte"] == s["start_byte"]:
                        slot_fqn = ref["fqn"]
                observed_addr = "0x" + s["observed"]
                evidence = {
                    "start_byte": s["start_byte"],
                    "library_fqn": slot_fqn,
                    "observed_address": observed_addr,
                    "declared_address": declared.get((slot_fqn or "").lower()),
                }
                if PLACEHOLDER_RE.fullmatch(s["observed"]):
                    pass  # already flagged PLACEHOLDER_UNRESOLVED
                elif slot_fqn and slot_fqn.lower() not in declared:
                    acc.add("ADDRESS_UNDECLARED",
                            f"slot at byte {s['start_byte']} is linked to "
                            f"{observed_addr} but no library mapping declares {slot_fqn}",
                            evidence)
                elif evidence["declared_address"]:
                    if evidence["declared_address"].lower() == observed_addr.lower():
                        acc.add("LINK_REF_OK",
                                "library linked at the declared slot and address",
                                evidence)
                    else:
                        acc.add("LIBRARY_ADDRESS_DIFF",
                                f"library {slot_fqn} linked to a different address",
                                evidence)
                slot_evidence.append(evidence)
    else:
        acc.add("NO_RUNTIME_COMPARISON",
                "integrity verification only; no deployed runtime code submitted",
                detail={"fqn": fqn})

    return ContractReport(
        fqn=fqn,
        verdict=_verdict(acc.findings),
        findings=acc.findings,
        compared_library_slots=slot_evidence,
        metadata_tail=tail_info,
        **body_digests,
    )


def _check_config(acc: _Accum, meta: dict, config: dict) -> None:
    settings = meta.get("settings", {})

    recorded_version = meta.get("compiler", {}).get("version")
    submitted_version = config.get("compiler_version")
    if submitted_version is not None and submitted_version != recorded_version:
        acc.add("VERSION_DIFF",
                f"submitted compiler {submitted_version} != recorded {recorded_version}",
                {"submitted": submitted_version, "recorded": recorded_version})
    elif submitted_version == recorded_version:
        acc.add("VERSION_OK", detail={"version": recorded_version})

    submitted_opt = config.get("optimizer")
    recorded_opt = settings.get("optimizer")
    if submitted_opt is not None:
        se = bool(submitted_opt.get("enabled"))
        sr = submitted_opt.get("runs")
        re_ = bool((recorded_opt or {}).get("enabled"))
        rr = (recorded_opt or {}).get("runs")
        if se != re_ or sr != rr:
            acc.add("SETTING_DIFF",
                    "optimizer configuration differs from recorded compilation settings",
                    {"submitted": {"enabled": se, "runs": sr},
                     "recorded": {"enabled": re_, "runs": rr}})

    submitted_evm = config.get("evm_version")
    recorded_evm = settings.get("evmVersion")
    if submitted_evm is not None and submitted_evm != recorded_evm:
        acc.add("SETTING_DIFF",
                f"evmVersion differs: submitted {submitted_evm} != recorded {recorded_evm}",
                {"setting": "evm_version",
                 "submitted": submitted_evm, "recorded": recorded_evm})

    if "CONFIG_OK" not in acc.codes() and not acc.has(Severity.ERROR):
        acc.add("CONFIG_OK", detail={
            "compiler_version": submitted_version,
            "optimizer": submitted_opt,
            "evm_version": submitted_evm,
        })


# ---------------------------------------------------------------------------
# Evidence chain
# ---------------------------------------------------------------------------

def _finalize(result: dict) -> dict:
    payload_digest = json_digest(result)
    result["evidence"] = {
        "payload_canonical_sha256": __import__("hashlib").sha256(
            canonical_json(result)).hexdigest(),
        "payload_digest_keccak256": payload_digest,
    }
    # Hash chain over contract findings (append-only ordering is the order built)
    prev = None
    links = []
    for c in result.get("contracts", []):
        link = chain_hash(prev, json_digest(c))
        links.append({"fqn": c.get("fqn"), "chain": link})
        prev = link
    result["evidence"]["finding_chain"] = links
    result["evidence"]["chain_head"] = prev
    result["duration_ms"] = int((time.time() - result["submitted_at"]) * 1000)
    return result
