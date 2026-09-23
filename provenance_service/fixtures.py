"""Offline, dependency-free builder of *structurally faithful* compiler outputs.

The service verifies already-produced compiler output; it never invokes
``solc``.  To exercise the verifier without a Solidity toolchain, this
module synthesizes compiler-output documents that reproduce every property
the verifier checks, with **real** cryptography and **real** dependence of
the emitted bytecode on the sources/settings:

* the metadata document is the exact JSON whose hash the CBOR auxdata tail
  commits to (sha2-256 multihash / keccak256), so a real recomputation can
  detect any tampering;
* runtime body bytes derive from Keccak over the actual source digests and
  compilation settings, so changing a constant, the optimizer config or an
  EVM version changes opcodes in the body — normalization cannot hide it;
* library references use the real solc placeholder scheme
  ``__$<keccak16(fqn)>$__`` (34 ASCII chars, replacing a 20-byte slot).
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from . import cbor
from .crypto import canonical_json, eip55_checksum, keccak256

DEFAULT_VERSION = "0.8.24+commit.e11b9ed9"


# ---------------------------------------------------------------------------
# Example sources
# ---------------------------------------------------------------------------

def example_sources(supply: int = 1_000_000) -> dict[str, str]:
    return {
        "src/SafeMath.sol": (
            "// SPDX-License-Identifier: MIT\n"
            "pragma solidity ^0.8.24;\n\n"
            "library SafeMath {\n"
            "    function add(uint256 a, uint256 b) internal pure returns (uint256) {\n"
            "        return a + b;\n"
            "    }\n"
            "}\n"
        ),
        "src/Token.sol": (
            "// SPDX-License-Identifier: MIT\n"
            "pragma solidity ^0.8.24;\n\n"
            "import \"./SafeMath.sol\";\n\n"
            "contract Token {\n"
            f"    uint256 public constant TOTAL_SUPPLY = {supply};\n"
            "    using SafeMath for uint256;\n\n"
            "    address public immutable mathLib;\n"
            "    mapping(address => uint256) public balanceOf;\n\n"
            "    constructor(address _mathLib) {\n"
            "        mathLib = _mathLib;\n"
            "        balanceOf[msg.sender] = TOTAL_SUPPLY;\n"
            "    }\n\n"
            "    function transfer(address to, uint256 amount) external {\n"
            "        balanceOf[msg.sender] = SafeMath.add(\n"
            "            balanceOf[msg.sender], 0\n"
            "        );\n"
            "        balanceOf[to] += amount;\n"
            "    }\n"
            "}\n"
        ),
    }


SHELL_SCRIPT = (
    "#!/bin/sh\n"
    "# This file ships inside the sample source package.\n"
    "# The provenance service must NEVER execute it. A marker is created\n"
    "# only if *something* wrongly ran it; the test suite asserts its absence.\n"
    'echo "SCRIPT RAN" > /tmp/provenance_script_should_never_run.marker\n'
)


# ---------------------------------------------------------------------------
# Package (zip) construction
# ---------------------------------------------------------------------------

def build_zip(files: dict[str, bytes | str], include_script: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            if isinstance(content, str):
                content = content.encode()
            zi = zipfile.ZipInfo(path)
            # Mark the shell script executable on purpose, to prove that the
            # executable bit never causes execution.
            zi.external_attr = (0o755 << 16) if include_script and path.endswith(".sh") else (0o644 << 16)
            zf.writestr(zi, content)
        if include_script:
            zf.writestr("scripts/build.sh", SHELL_SCRIPT)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Placeholder scheme
# ---------------------------------------------------------------------------

def library_fqn(path: str, name: str) -> str:
    return f"{path}:{name}"


def placeholder_for_fqn(fqn: str) -> str:
    """solc-style 34-char placeholder string committing to the FQN."""
    return "__$" + keccak256(fqn.encode()).hex()[:34] + "$__"


_LINKED_FQN_RE = re.compile(r"__\$[0-9a-f]{34}\$__")


def link_object(object_hex: str, fqn: str, address: str) -> str:
    """Substitute a deployed (linked) address for a library placeholder."""
    token = placeholder_for_fqn(fqn)
    addr = address[2:].lower()
    if token not in object_hex:
        raise ValueError(f"no placeholder for {fqn} in object")
    return object_hex.replace(token, addr)


# ---------------------------------------------------------------------------
# Deterministic "codegen" — body genuinely depends on sources + settings
# ---------------------------------------------------------------------------

def _expand(seed: bytes, nbytes: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < nbytes:
        out += keccak256(seed + counter.to_bytes(4, "big"))
        counter += 1
    return out[:nbytes]


def _version_bytes(version: str) -> bytes:
    core = version.split("+", 1)[0].split("-", 1)[0]
    parts = [int(x) for x in core.split(".")]
    if len(parts) != 3 or not all(0 <= p <= 255 for p in parts):
        raise ValueError(f"unparsable compiler version: {version}")
    return bytes(parts)


def _metadata_document(
    fqn: str,
    path: str,
    name: str,
    sources: dict[str, str],
    version: str,
    optimizer_enabled: bool,
    optimizer_runs: int,
    evm_version: str,
    libraries: dict[str, str],
    bytecode_hash: str,
) -> dict:
    return {
        "compiler": {"version": version},
        "language": "Solidity",
        "output": {
            "abi": [
                {"inputs": [{"internalType": "address", "name": "_mathLib",
                             "type": "address"}],
                 "stateMutability": "nonpayable", "type": "constructor"},
            ],
            "devdoc": {"kind": "dev", "methods": {}, "version": 1},
            "userdoc": {"kind": "user", "methods": {}},
        },
        "settings": {
            "compilationTarget": {path: name},
            "evmVersion": evm_version,
            "libraries": dict(sorted(libraries.items())),
            "metadata": {"bytecodeHash": bytecode_hash},
            "optimizer": {"enabled": optimizer_enabled, "runs": optimizer_runs},
            "remappings": [],
        },
        "sources": {
            sp: {
                "keccak256": "0x" + keccak256(content.encode()).hex(),
                "urls": ["bzzr://0000000000000000000000000000000000000000000000000000000000000000"],
            }
            for sp, content in sorted(sources.items())
        },
        "version": 1,
    }


def build_compiler_output(
    sources: dict[str, str],
    *,
    contracts: list[tuple[str, str, list[tuple[str, str]]]] | None = None,
    version: str = DEFAULT_VERSION,
    optimizer_enabled: bool = True,
    optimizer_runs: int = 200,
    evm_version: str = "paris",
    bytecode_hash: str = "ipfs",
    libraries: dict[str, str] | None = None,
) -> dict:
    """Return a solc standard-json shaped compiler output document.

    ``contracts`` lists ``(source_path, contract_name, linked_libs)`` where
    ``linked_libs`` is a list of ``(lib_path, lib_name)`` references.
    Defaults: ``src/Token.sol:Token`` linked against ``src/SafeMath.sol:SafeMath``.
    """
    libraries = libraries or {}
    if contracts is None:
        contracts = [("src/Token.sol", "Token", [("src/SafeMath.sol", "SafeMath")])]

    out_contracts: dict[str, dict] = {}
    for path, name, libs in contracts:
        fqn = library_fqn(path, name)
        meta = _metadata_document(
            fqn, path, name, sources, version,
            optimizer_enabled, optimizer_runs, evm_version,
            libraries, bytecode_hash,
        )
        meta_bytes = canonical_json(meta)
        import hashlib
        meta_sha = hashlib.sha256(meta_bytes).digest()

        # Real solc semantics: the *code* depends on source contents and
        # compilation settings, but not on the source-file paths (those only
        # enter the metadata document/auxdata). This lets the verifier prove
        # that path normalization cannot hide a real semantic change: change
        # one source byte and this seed changes even if the path is identical.
        settings_seed = canonical_json({
            "source_digests": sorted(
                keccak256(c.encode()).hex() for c in sources.values()
            ),
            "optimizer": {"enabled": optimizer_enabled, "runs": optimizer_runs},
            "evmVersion": evm_version,
            "compiler": version,
        })
        head = bytes.fromhex("6080604052348015600f57600080fd5b50")  # fixed prelude
        body = bytearray(head + _expand(settings_seed, 96))

        link_refs: dict[str, dict[str, list]] = {}
        object_hex = body.hex()
        for lib_path, lib_name in libs:
            lib_fqn = library_fqn(lib_path, lib_name)
            token = placeholder_for_fqn(lib_fqn)
            start_bytes = len(object_hex) // 2
            object_hex += token
            link_refs.setdefault(lib_path, {}).setdefault(lib_name, []).append(
                {"start": start_bytes, "length": 20}
            )
        object_hex += _expand(settings_seed + b":tail", 64).hex()

        cbor_map: dict[str, Any] = {"solc": _version_bytes(version)}
        if bytecode_hash == "ipfs":
            cbor_map = {"ipfs": meta_sha, "solc": _version_bytes(version)}
        elif bytecode_hash == "bzzr1":
            cbor_map = {"bzzr1": meta_sha, "solc": _version_bytes(version)}
        elif bytecode_hash == "keccak256":
            cbor_map = {"keccak256": keccak256(meta_bytes), "solc": _version_bytes(version)}
        elif bytecode_hash == "none":
            pass
        else:
            raise ValueError(bytecode_hash)
        object_hex += cbor.build_metadata_tail(cbor_map).hex()

        deployed = {
            "object": "0x" + object_hex,
            "opcodes": "PUSH1 0x80 ... (offline fixture)",
            "sourceMap": "",
            "linkReferences": link_refs,
            "immutableReferences": {},
        }
        # Creation bytecode: structurally present, not used by the verifier.
        creation = {
            "object": "0x6080604052" + object_hex,
            "opcodes": "",
            "sourceMap": "",
            "linkReferences": link_refs,
        }
        out_contracts.setdefault(path, {})[name] = {
            "abi": meta["output"]["abi"],
            "metadata": meta_bytes.decode("utf-8"),
            "storageLayout": {"storage": [], "types": {}},
            "evm": {
                "bytecode": creation,
                "deployedBytecode": deployed,
            },
        }

    return {
        "contracts": out_contracts,
        "version": version,
        "sources": {
            p: {"id": i, "keccak256": "0x" + keccak256(c.encode()).hex()}
            for i, (p, c) in enumerate(sorted(sources.items()))
        },
    }


def iter_artifacts(compiler_output: dict):
    """Yield ``(fqn, artifact)`` for every contract in a compiler output."""
    for path, contracts in compiler_output["contracts"].items():
        for name, art in contracts.items():
            yield library_fqn(path, name), art


def deployed_artifact(compiler_output: dict, fqn: str | None = None) -> tuple[str, dict]:
    arts = list(iter_artifacts(compiler_output))
    if fqn is None:
        assert len(arts) == 1
        return arts[0]
    for f, a in arts:
        if f == fqn:
            return f, a
    raise KeyError(fqn)


# ---------------------------------------------------------------------------
# Convenience: ready-to-submit job parts
# ---------------------------------------------------------------------------

def relabel_sources(sources: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    """Return the same file contents under different compile paths.

    ``import`` references that mention a moved basename are rewritten so the
    source text remains semantically identical; the byte-for-byte content
    digests are therefore preserved while the metadata paths/URLs differ.
    """
    relabeled = {}
    for old, content in sources.items():
        new = mapping.get(old, old)
        for o, n in mapping.items():
            content = content.replace(o, n)
        relabeled[new] = content
    return relabeled


def sample_job_parts(
    sources: dict[str, str] | None = None,
    *,
    optimizer_enabled: bool = True,
    optimizer_runs: int = 200,
    evm_version: str = "paris",
    version: str = DEFAULT_VERSION,
    library_address: str = "0x8ba1f109551bD432803012645Ac136ddd64DBA72",
) -> dict:
    sources = sources if sources is not None else example_sources()
    output = build_compiler_output(
        sources,
        optimizer_enabled=optimizer_enabled,
        optimizer_runs=optimizer_runs,
        evm_version=evm_version,
        version=version,
    )
    fqn, art = deployed_artifact(output)
    lib_fqn = library_fqn("src/SafeMath.sol", "SafeMath")
    linked = link_object(art["evm"]["deployedBytecode"]["object"], lib_fqn, library_address)
    config = {
        "compiler_version": version,
        "optimizer": {"enabled": optimizer_enabled, "runs": optimizer_runs},
        "evm_version": evm_version,
        "libraries": {lib_fqn: eip55_checksum(library_address)},
    }
    return {
        "sources": sources,
        "package": build_zip(sources),
        "compiler_output": output,
        "config": config,
        "fqn": fqn,
        "expected_runtime_code": {fqn: linked},
        "linked_runtime": linked,
        "lib_fqn": lib_fqn,
    }
