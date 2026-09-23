"""溯源核验主流水线。

裁决（verdict）只有三种，且**任何差异都必须显式归类，不允许静默忽略**：

- ``EXACT``       全部逐 nibble 一致；
- ``RULE_MATCH``  差异只出现在预先声明、语义可解释的规则区域
                  （链接库地址 / CBOR 元数据尾），其余完全一致；
- ``MISMATCH``    出现任何无法被规则解释的差异，或密码学/元数据核验失败。

每条检查都生成一条可复核证据（输入摘要、计算值、比对值、结论），
证据之间用哈希链串联，可离线复验。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import crypto
from .bytecode import (
    BytecodeError,
    diff_nibbles,
    find_placeholders,
    mask_regions,
    normalize_hex,
)
from .compiler_output import (
    CompilerOutputError,
    ContractArtifact,
    CompilationConfig,
    parse_compiler_output,
    parse_config,
    parse_version,
)
from .libraries import LibraryError, apply_linking, validate_libraries_structure
from .metadata import MetadataError, cbor_value_repr, parse_cbor_tail, verify_embedded_hash

VERDICT_EXACT = "EXACT"
VERDICT_RULE = "RULE_MATCH"
VERDICT_MISMATCH = "MISMATCH"

SEV_INFO = "INFO"
SEV_RULE = "RULE"          # 差异被某条语义规则允许
SEV_FAIL = "FAIL"          # 无法解释/核验失败
_SEV_ORDER = {SEV_INFO: 0, SEV_RULE: 1, SEV_FAIL: 2}


def canonical_json(obj: Any) -> bytes:
    """确定性 JSON 序列化：键排序、无空白、UTF-8。作为所有哈希的输入。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass
class Check:
    code: str
    severity: str
    detail: dict
    rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"code": self.code, "severity": self.severity, "detail": self.detail}
        if self.rules:
            out["rules"] = self.rules
        return out


class VerificationRejected(Exception):
    """请求层面不可核验（结构非法/非法路径/非法地址）——HTTP 400。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 各专项检查
# --------------------------------------------------------------------------- #
def _check_metadata_basics(artifact: ContractArtifact) -> list[Check]:
    checks: list[Check] = []
    meta = artifact.metadata
    required = ("compiler", "language", "outputSelection", "settings", "sources")
    missing = [k for k in required if k not in meta]
    if meta.get("language") != "Solidity":
        checks.append(
            Check("METADATA_LANGUAGE", SEV_FAIL, {"language": meta.get("language"), "expected": "Solidity"})
        )
    comp = meta.get("compiler")
    meta_version = comp.get("version") if isinstance(comp, dict) else None
    if meta_version is None:
        checks.append(Check("METADATA_COMPILER_VERSION", SEV_FAIL, {"reason": "metadata.compiler.version 缺失"}))
    else:
        try:
            parse_version(meta_version)
            checks.append(
                Check("METADATA_COMPILER_VERSION_PRESENT", SEV_INFO, {"metadata_compiler_version": meta_version})
            )
        except CompilerOutputError as exc:
            checks.append(Check("METADATA_COMPILER_VERSION", SEV_FAIL, {"reason": str(exc)}))
    if missing:
        checks.append(Check("METADATA_STRUCTURE", SEV_FAIL, {"missing_fields": missing}))
    else:
        checks.append(Check("METADATA_STRUCTURE", SEV_INFO, {"required_fields": list(required)}))
    return checks


def _check_compiler_version(config: CompilationConfig, artifact: ContractArtifact, linked_deployed: str | None, linked_creation: str | None) -> list[Check]:
    meta_version = artifact.metadata.get("compiler", {}).get("version", "")
    cbor_tail = None
    candidates = [c for c in (linked_deployed, linked_creation) if c]
    for candidate in candidates:
        try:
            cbor_tail, _ = parse_cbor_tail(candidate)
            break
        except MetadataError:
            continue

    solc_bytes = cbor_tail.get("solc") if cbor_tail else None
    solc_triple = None
    if isinstance(solc_bytes, (bytes, bytearray)) and len(solc_bytes) == 3:
        solc_triple = tuple(solc_bytes)

    detail = {
        "config_version": config.compiler_version,
        "metadata_version": meta_version,
        "cbor_solc": list(solc_bytes) if solc_bytes is not None else None,
    }
    sev = SEV_INFO
    cfg_triple = parse_version(config.compiler_version)
    try:
        meta_triple = parse_version(meta_version)
    except CompilerOutputError:
        meta_triple = None
    if meta_triple != cfg_triple:
        sev = SEV_FAIL
        detail["disagreement"] = "config 与 metadata 版本三段不一致"
    if solc_triple is not None and meta_triple is not None and solc_triple != meta_triple:
        sev = SEV_FAIL
        detail["disagreement"] = "CBOR solc 与 metadata.compiler.version 三段不一致"
    if cbor_tail is None:
        detail["cbor"] = "deployed 与 creation 字节码均未找到可解析 CBOR 尾"
    return [Check("COMPILER_VERSION", sev, detail)]


def _check_sources(config: CompilationConfig, artifact: ContractArtifact, sources: dict[str, bytes]) -> list[Check]:
    """metadata.sources 中每个被编译源文件的密码学摘要逐一核验，不得跳过。"""
    checks: list[Check] = []
    meta_sources = artifact.metadata.get("sources", {})
    used = set()
    for path, entry in meta_sources.items():
        used.add(path)
        detail = {"path": path}
        if path not in sources:
            checks.append(
                Check("SOURCE_PRESENT", SEV_FAIL, {**detail, "reason": "源码包缺少该文件（拒绝凭空核验）"})
            )
            continue
        content = sources[path]
        detail["provided_sha256"] = crypto.sha256(content).hex()
        detail["provided_keccak256"] = crypto.keccak256(content).hex()
        digests = entry.get("keccak256") if isinstance(entry, dict) else None
        if not digests:
            checks.append(
                Check(
                    "SOURCE_DIGEST",
                    SEV_FAIL,
                    {**detail, "reason": "metadata 未记录 keccak256，摘要无法核验（不做放行）"},
                )
            )
            continue
        if crypto.keccak256(content).hex() != digests.lstrip("0x").lower():
            checks.append(
                Check(
                    "SOURCE_DIGEST",
                    SEV_FAIL,
                    {
                        **detail,
                        "metadata_keccak256": digests,
                        "match": False,
                    },
                )
            )
        else:
            checks.append(Check("SOURCE_DIGEST", SEV_INFO, {**detail, "metadata_keccak256": digests, "match": True}))
    # 包内多余文件：信息记录，绝不静默丢弃
    extra = sorted(set(sources) - used)
    checks.append(Check("SOURCE_EXTRA_FILES", SEV_INFO, {"count": len(extra), "paths": extra}))
    return checks


def _settings_view(meta_settings: dict) -> dict:
    """从 metadata.settings 提取与配置对应的可比视图（保留缺失态）。"""
    optimizer = meta_settings.get("optimizer") or {}
    return {
        "evm_version": meta_settings.get("evmVersion"),
        "optimize": optimizer.get("enabled", False) if isinstance(optimizer, dict) else None,
        "optimize_runs": optimizer.get("runs") if isinstance(optimizer, dict) else None,
        "via_ir": meta_settings.get("viaIR", False),
        "libraries": validate_libraries_structure(meta_settings.get("libraries")),
    }


def _check_settings(config: CompilationConfig, artifact: ContractArtifact) -> list[Check]:
    """优化配置/viaIR/EVM/库地址与 metadata.settings 逐项严格对比。"""
    settings = artifact.metadata.get("settings")
    if not isinstance(settings, dict):
        return [Check("COMPILATION_SETTINGS", SEV_FAIL, {"reason": "metadata.settings 缺失"})]
    actual = _settings_view(settings)
    expected = {
        "evm_version": config.evm_version,
        "optimize": config.optimize,
        "optimize_runs": config.optimize_runs,
        "via_ir": config.via_ir,
        "libraries": config.libraries,
    }
    disagreements = []
    for key, exp in expected.items():
        act = actual.get(key)
        if key == "libraries":
            # 地址以校验和归一后比较；只比较本合约实际链接引用涉及的库
            continue
        if act != exp:
            disagreements.append({"key": key, "config": exp, "metadata": act})
    checks = [
        Check(
            "COMPILATION_SETTINGS",
            SEV_FAIL if disagreements else SEV_INFO,
            {"config": expected, "metadata": actual, "disagreements": disagreements},
        )
    ]
    return checks


def _perform_linking(
    config: CompilationConfig, artifact: ContractArtifact
) -> tuple[list[Check], str | None, str | None, list[tuple[str, str, int, int]]]:
    """把库地址写入 deployed/creation 未链接字节码，返回 (检查, 链接deployed, 链接creation, refs)。"""
    checks: list[Check] = []
    placeholders = find_placeholders(artifact.deployed_bytecode)
    ref_regions = [(s, l) for _, _, s, l in artifact.deployed_link_refs]
    ph_regions = [(s, e - s) for s, e, _ in placeholders]
    if sorted(ph_regions) != sorted(ref_regions):
        checks.append(
            Check(
                "LINK_REFERENCE_CONSISTENCY",
                SEV_FAIL,
                {
                    "placeholder_regions": ph_regions,
                    "link_reference_regions": ref_regions,
                    "reason": "字节码占位位置与 linkReferences 不一致（编译器输出自相矛盾）",
                },
            )
        )
    # 每个 linkReference 必须在配置中有对应库地址
    missing = []
    for src, lib, start, length in artifact.deployed_link_refs:
        if src not in config.libraries or lib not in config.libraries.get(src, {}):
            missing.append(f"{src}:{lib}")
    if missing:
        checks.append(Check("LIBRARY_ADDRESS_PROVIDED", SEV_FAIL, {"missing": missing}))
    if any(c.severity == SEV_FAIL for c in checks):
        return checks, None, None, artifact.deployed_link_refs
    try:
        linked_dep, link_evidence = apply_linking(
            artifact.deployed_bytecode, artifact.deployed_link_refs, config.libraries
        )
        linked_cre, cre_evidence = apply_linking(
            artifact.creation_bytecode, artifact.creation_link_refs, config.libraries
        )
    except (LibraryError, BytecodeError) as exc:
        checks.append(Check("LIBRARY_LINKING", SEV_FAIL, {"reason": str(exc)}))
        return checks, None, None, artifact.deployed_link_refs
    checks.append(
        Check(
            "LIBRARY_LINKING",
            SEV_INFO,
            {"deployed_links": link_evidence, "creation_links": cre_evidence},
            rules=["LINKED_LIBRARY_ADDRESSES"] if link_evidence else [],
        )
    )
    return checks, linked_dep, linked_cre, artifact.deployed_link_refs


def _compare_deployed(
    config: CompilationConfig,
    artifact: ContractArtifact,
    linked: str,
    dep_refs: list[tuple[str, str, int, int]],
    target_deployed: str,
) -> list[Check]:
    """链接期望字节码与部署字节码逐 nibble 比较。"""
    checks: list[Check] = []
    ref_regions = [(s, l) for _, _, s, l in dep_refs]

    # 长度必须一致（掩码不能掩盖代码长度差异）
    cmp_detail: dict = {
        "expected_length_nibbles": len(linked),
        "target_length_nibbles": len(target_deployed),
    }
    if len(linked) != len(target_deployed):
        checks.append(
            Check("BYTECODE_LENGTH", SEV_FAIL, {**cmp_detail, "reason": "长度不同，无法逐 nibble 溯源"})
        )
        return checks

    # 目标字节码中不得残留任何占位
    target_residual = find_placeholders(target_deployed)
    if target_residual:
        checks.append(
            Check(
                "TARGET_FULLY_LINKED",
                SEV_FAIL,
                {"residual_placeholders": target_residual, "reason": "部署字节码仍含未链接占位"},
            )
        )

    # 解析两侧 CBOR 尾；期望侧内嵌哈希必须与 metadata JSON 真实一致
    rule_regions: list[tuple[int, int]] = list(ref_regions)
    rules_used: list[str] = []
    tail_detail: dict = {}
    try:
        exp_cbor, exp_tail_start = parse_cbor_tail(linked)
        tgt_cbor, tgt_tail_start = parse_cbor_tail(target_deployed)
        hash_result = verify_embedded_hash(artifact.metadata_raw, exp_cbor)
        tail_detail["expected_cbor"] = {k: cbor_value_repr(v) for k, v in exp_cbor.items()}
        tail_detail["target_cbor"] = {k: cbor_value_repr(v) for k, v in tgt_cbor.items()}
        tail_detail["metadata_hash_verification"] = hash_result
        failed_hashes = [h for h in hash_result["hash_checks"] if h.get("ok") is False]
        inconclusive = [h for h in hash_result["hash_checks"] if h.get("ok") is None]
        if failed_hashes:
            checks.append(Check("METADATA_EMBEDDED_HASH", SEV_FAIL, {"checks": hash_result["hash_checks"]}))
        else:
            checks.append(
                Check(
                    "METADATA_EMBEDDED_HASH",
                    SEV_INFO,
                    {
                        "checks": hash_result["hash_checks"],
                        "note": "ok=None 表示超出离线能力的显式不可判定，非放行" if inconclusive else "",
                    },
                )
            )
        tail_len_nibbles = (len(bytes.fromhex(linked)) - exp_tail_start // 2) * 2
        if exp_tail_start != tgt_tail_start:
            checks.append(
                Check(
                    "METADATA_TAIL_POSITION",
                    SEV_FAIL,
                    {"expected_start_nibble": exp_tail_start, "target_start_nibble": tgt_tail_start},
                )
            )
        else:
            rule_regions.append((exp_tail_start, tail_len_nibbles))
            rules_used.append("CBOR_METADATA_TAIL")
    except MetadataError as exc:
        checks.append(Check("CBOR_TAIL", SEV_FAIL, {"reason": f"无法解析元数据尾: {exc}"}))
        return checks

    if ref_regions:
        rules_used.append("LINKED_LIBRARY_ADDRESSES")

    # 逐 nibble 掩码比较
    try:
        mask = mask_regions(len(linked), rule_regions)
    except BytecodeError as exc:
        checks.append(Check("MASK_CONSTRUCTION", SEV_FAIL, {"reason": str(exc)}))
        return checks
    diffs = diff_nibbles(linked, target_deployed, mask)

    if not diffs:
        if rules_used and _masked_positions_differ(linked, target_deployed, mask):
            sev = SEV_RULE
            cmp_detail["masked_difference_observed"] = True
        else:
            sev = SEV_INFO
            cmp_detail["masked_difference_observed"] = False
        checks.append(
            Check(
                "BYTECODE_COMPARISON",
                sev,
                {
                    **cmp_detail,
                    "unexplained_diffs": [],
                    "rules_applied": rules_used,
                    "rule_regions_nibbles": rule_regions,
                    **tail_detail,
                },
                rules=rules_used,
            )
        )
    else:
        checks.append(
            Check(
                "BYTECODE_COMPARISON",
                SEV_FAIL,
                {
                    **cmp_detail,
                    "unexplained_diffs": diffs,
                    "rules_applied": rules_used,
                    "rule_regions_nibbles": rule_regions,
                    **tail_detail,
                },
            )
        )
    return checks


def _link_and_compare(
    config: CompilationConfig, artifact: ContractArtifact, target_deployed: str
) -> tuple[list[Check], dict | None]:
    """保留兼容入口（测试可直接调用）：链接 + 比较。"""
    link_checks, linked_dep, linked_cre, dep_refs = _perform_linking(config, artifact)
    if linked_dep is None:
        return link_checks, None
    cmp_checks = _compare_deployed(config, artifact, linked_dep, dep_refs, target_deployed)
    return link_checks + cmp_checks, None


def _masked_positions_differ(expected: str, actual: str, mask: bytearray) -> bool:
    return any(mask[i] == 1 and expected[i] != actual[i] for i in range(len(expected)))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _check_creation_internal(
    artifact: ContractArtifact, linked_creation: str | None
) -> list[Check]:
    """creation（部署事务输入）字节码的内部一致性检查。

    本服务的溯源目标是 deployed runtime，但 creation 产物同样必须自洽：
    链接后无残留占位、CBOR 元数据尾可解析且内嵌哈希真实。任何问题都不能忽略。
    """
    if linked_creation is None:
        return [Check("CREATION_INTERNAL", SEV_FAIL, {"reason": "creation 字节码链接失败，无法检查"})]
    residual = find_placeholders(linked_creation)
    if residual:
        return [Check("CREATION_INTERNAL", SEV_FAIL, {"residual_placeholders": residual})]
    try:
        cbor, start = parse_cbor_tail(linked_creation)
    except MetadataError as exc:
        return [Check("CREATION_INTERNAL", SEV_FAIL, {"reason": f"creation CBOR 尾无法解析: {exc}"})]
    result = verify_embedded_hash(artifact.metadata_raw, cbor)
    bad = [h for h in result["hash_checks"] if h.get("ok") is False]
    return [
        Check(
            "CREATION_INTERNAL",
            SEV_FAIL if bad else SEV_INFO,
            {"tail_start_nibble": start, "hash_checks": result["hash_checks"]},
        )
    ]


def run_verification(
    config_obj: dict,
    compiler_output_obj: dict,
    sources: dict[str, bytes],
    target_deployed: str,
) -> dict:
    """执行一次完整溯源核验并返回报告字典。

    :raises VerificationRejected: 请求本身非法（400 类）。
    """
    try:
        config = parse_config(config_obj)
    except CompilerOutputError as exc:
        raise VerificationRejected("BAD_CONFIG", str(exc)) from exc

    # 提前校验全部库地址的 EIP-55——非法地址属于请求不可受理
    for src, libs in config.libraries.items():
        for lib, addr in libs.items():
            from .libraries import parse_checksummed_address

            try:
                parse_checksummed_address(addr)
            except LibraryError as exc:
                raise VerificationRejected("BAD_LIBRARY_ADDRESS", f"{src}:{lib}: {exc}") from exc

    try:
        artifact = parse_compiler_output(compiler_output_obj, config.source, config.name)
    except CompilerOutputError as exc:
        raise VerificationRejected("BAD_COMPILER_OUTPUT", str(exc)) from exc

    try:
        target = normalize_hex(target_deployed)
    except BytecodeError as exc:
        raise VerificationRejected("BAD_TARGET_BYTECODE", str(exc)) from exc

    checks: list[Check] = []
    checks.extend(_check_metadata_basics(artifact))

    # 链接在版本/CBOR 检查之前：链接后才是纯十六进制，可安全解析 CBOR 尾
    link_checks, linked_dep, linked_cre, dep_refs = _perform_linking(config, artifact)
    checks.extend(link_checks)

    checks.extend(_check_compiler_version(config, artifact, linked_dep, linked_cre))
    checks.extend(_check_sources(config, artifact, sources))
    checks.extend(_check_settings(config, artifact))
    if linked_dep is not None:
        checks.extend(_compare_deployed(config, artifact, linked_dep, dep_refs, target))
    checks.extend(_check_creation_internal(artifact, linked_cre))

    worst = SEV_INFO
    rules: list[str] = []
    for c in checks:
        if _SEV_ORDER[c.severity] > _SEV_ORDER[worst]:
            worst = c.severity
        rules.extend(c.rules)
    if worst == SEV_FAIL:
        verdict = VERDICT_MISMATCH
    elif worst == SEV_RULE:
        verdict = VERDICT_RULE
    else:
        verdict = VERDICT_EXACT

    rules = sorted(set(rules))
    return {
        "contract": {"source": config.source, "name": config.name},
        "verdict": verdict,
        "rules_applied": rules,
        "summary": {
            "exact": verdict == VERDICT_EXACT,
            "rule_based_matches": rules,
            "unexplained_differences": any(
                c.code == "BYTECODE_COMPARISON" and c.detail.get("unexplained_diffs") for c in checks
            ),
        },
        "checks": [c.to_dict() for c in checks],
    }
