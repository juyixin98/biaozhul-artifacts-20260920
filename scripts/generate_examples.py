"""Generate example inputs under examples/ and (optionally) run curl demos.

Usage:
    python scripts/generate_examples.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provenance_service.crypto import eip55_checksum  # noqa: E402
from provenance_service.fixtures import (  # noqa: E402
    DEFAULT_VERSION,
    build_compiler_output,
    build_zip,
    example_sources,
    library_fqn,
    link_object,
    relabel_sources,
)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "examples", "packages")

ADDR_A = "0x8ba1f109551bD432803012645Ac136ddd64DBA72"
ADDR_B = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"


def config(addr: str, lib_fqn: str, *, enabled=True, runs=200,
           evm="paris", version=DEFAULT_VERSION) -> dict:
    return {
        "compiler_version": version,
        "optimizer": {"enabled": enabled, "runs": runs},
        "evm_version": evm,
        "libraries": {lib_fqn: eip55_checksum(addr)},
    }


def write_case(name: str, *, zip_bytes: bytes, cfg: dict, output: dict,
               expected: dict | None) -> None:
    d = os.path.join(OUT, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "sources.zip"), "wb") as f:
        f.write(zip_bytes)
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    with open(os.path.join(d, "compiler_output.json"), "w") as f:
        json.dump(output, f, indent=2)
    with open(os.path.join(d, "expected_runtime_code.json"), "w") as f:
        json.dump(expected or {}, f, indent=2)
    print(f"  wrote {name}/")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    print(f"generating examples in {OUT}")

    sources = example_sources()
    lib = library_fqn("src/SafeMath.sol", "SafeMath")
    fqn = "src/Token.sol:Token"

    # 1. happy path ----------------------------------------------------------
    out = build_compiler_output(sources)
    art = out["contracts"]["src/Token.sol"]["Token"]["evm"]["deployedBytecode"]["object"]
    deployed = link_object(art, lib, ADDR_A)
    write_case("01_exact",
               zip_bytes=build_zip(sources),
               cfg=config(ADDR_A, lib),
               output=out,
               expected={fqn: deployed})

    # 2. same artifact, library deployed at a second address -----------------
    deployed_b = link_object(art, lib, ADDR_B)
    write_case("02_library_address_rule",
               zip_bytes=build_zip(sources),
               cfg=config(ADDR_A, lib),          # original compilation address
               output=out,
               expected={fqn: deployed_b})      # chain deployment at address B

    # 3. optimizer configuration mismatch ------------------------------------
    cfg_off = config(ADDR_A, lib, enabled=False, runs=200)
    write_case("03_optimizer_mismatch",
               zip_bytes=build_zip(sources),
               cfg=cfg_off,                      # disagrees with artifact metadata
               output=out,
               expected={fqn: deployed})

    # 4. missing source -------------------------------------------------------
    write_case("04_missing_source",
               zip_bytes=build_zip({"src/Token.sol": sources["src/Token.sol"]},
                                   include_script=False),
               cfg=config(ADDR_A, lib),
               output=out,
               expected={fqn: deployed})

    # 5. malicious path traversal --------------------------------------------
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/cron.d/evil", "* * * * * root id\n")
        zf.writestr("src/Token.sol", sources["src/Token.sol"])
        zf.writestr("src/SafeMath.sol", sources["src/SafeMath.sol"])
    write_case("05_malicious_path",
               zip_bytes=buf.getvalue(),
               cfg=config(ADDR_A, lib),
               output=out,
               expected={fqn: deployed})

    # 6. semantic change hidden behind identical paths ------------------------
    tweaked = example_sources(supply=2_000_000)
    write_case("06_semantic_change",
               zip_bytes=build_zip(tweaked),
               cfg=config(ADDR_A, lib),
               output=out,                       # artifact claims the original sources
               expected={fqn: deployed})

    # 7. path relocation (rule match: only metadata hash differs) -------------
    # The deployed code comes from an identical build whose sources lived at
    # different paths. Code body is byte-identical; only the embedded metadata
    # hash (and the linked library address) differ -> RULE_MATCH.
    mapping = {"src/SafeMath.sol": "contracts/lib/SafeMath.sol",
               "src/Token.sol": "contracts/Token.sol"}
    moved = relabel_sources(sources, mapping)
    out_moved = build_compiler_output(
        moved,
        contracts=[("contracts/Token.sol", "Token",
                    [("contracts/lib/SafeMath.sol", "SafeMath")])])
    lib_moved = library_fqn("contracts/lib/SafeMath.sol", "SafeMath")
    art_moved = out_moved["contracts"]["contracts/Token.sol"]["Token"][
        "evm"]["deployedBytecode"]["object"]
    deployed_moved = link_object(art_moved, lib_moved, ADDR_B)
    write_case("07_path_relocation_rule",
               zip_bytes=build_zip(sources),    # original bundle supplied
               cfg=config(ADDR_A, lib),         # original compilation config
               output=out,                      # original artifact is the reference
               expected={fqn: deployed_moved})  # deployed code from relocated build

    # 8. no deployed runtime (integrity-only) ---------------------------------
    write_case("08_no_runtime_code",
               zip_bytes=build_zip(sources),
               cfg=config(ADDR_A, lib),
               output=out,
               expected=None)

    print("done.")


if __name__ == "__main__":
    main()
