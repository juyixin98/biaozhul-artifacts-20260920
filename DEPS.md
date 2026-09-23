# Locked dependencies

The build is fully offline after clone: the only dependency is **vendored**
under `lib/` and the toolchain version is pinned below.

## forge-std (test framework)

| | |
|---|---|
| package | foundry-rs/forge-std |
| vendored path | `lib/forge-std` |
| pinned commit | `7239323e35487ba4339c93fe591065a63ce122aa` |
| package version | 1.16.2 (`lib/forge-std/package.json`) |
| upstream | https://github.com/foundry-rs/forge-std |

Re-verify that the vendored tree is byte-identical to the pinned upstream
commit (requires network; not needed to build/test):

```bash
bash script/verify_deps.sh
```

## Foundry toolchain

Pinned to the release used to produce the artifacts:

| tool | version | commit |
|---|---|---|
| forge / cast / anvil | 1.8.3 | `cae51ad458f6abb64852b7709eb784352429825d` |

Install reproducibly with foundryup:

```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup --version 1.8.3
```

## Solidity compiler

`solc = "0.8.26"` in `foundry.toml` (first install fetches it automatically),
EVM target `cancun`, optimizer enabled (200 runs), `via_ir = true` (required
for the nested-struct JSON decoding in the reference-vector tests).

## Python reference model

Standard library only (`decimal`, `math`, `random`, `hashlib`, `json`) —
Python ≥ 3.10. No pip packages, no virtualenv needed.
