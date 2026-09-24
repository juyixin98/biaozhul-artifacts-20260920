# 线性释放代币托管 (Linear Token Vesting Escrow)

A cliff + linear **token vesting escrow** with revocation support, built on a
**local-only** toolchain: Solidity contracts compiled/tested with
[Foundry](https://getfoundry.sh), and a Python [FastAPI](https://fastapi.tiangolo.com/)
query/drive layer using [web3.py](https://web3py.readthedocs.io/). The chain is
always a local **Anvil** node with its well-known test keys — no public network
is ever contacted.

* **Synthetic token** — a dependency-free ERC-20 (`SyntheticToken`), minted locally.
* **Cliff** — nothing is releasable before the cliff timestamp.
* **Linear release** — after the cliff, tokens vest pro-rata over
  `end - start`; everything is vested at `end`.
* **Revocation** — the owner may freeze the curve at any time. The **vested
  portion is kept for the beneficiary** (still claimable); the unvested
  remainder is refunded to the owner immediately.
* **No overflow** — Solidity 0.8 checked arithmetic guards the
  `amount * elapsed / duration` math; a fuzz test proves vested ≤ total for any
  timestamp.

Conservation identity (asserted by tests):

```
final beneficiary claimed + owner refund == initial locked amount
escrow contract balance after settlement   == 0
```

---

## 1. Repository layout

```
.
├── src/
│   ├── TokenVesting.sol      # escrow: createSchedule / release / revoke
│   └── SyntheticToken.sol    # local ERC-20
├── lib/forge-std/src/        # vendored minimal Test/Vm/DSTest shim (offline build)
├── test/
│   └── TokenVesting.t.sol    # Foundry unit + fuzz acceptance tests
├── app/
│   ├── chain.py              # web3.py connection / artifact / tx helpers
│   └── main.py               # FastAPI HTTP service
├── scripts/
│   ├── deploy.py             # web3 deployer -> deployment/addresses.json
│   └── demo.py               # end-to-end walkthrough + conservation check
├── tests/
│   ├── conftest.py           # boots Anvil, builds, deploys
│   ├── test_vesting_contract.py
│   └── test_api.py
├── foundry.toml
├── remappings.txt
├── pytest.ini
├── requirements.txt          # pinned direct dependencies
├── requirements-lock.txt     # full frozen transitive environment
└── deployment/addresses.json # generated on deploy
```

## 2. Prerequisites

| Tool     | Version used | Install                                  |
|----------|--------------|------------------------------------------|
| Foundry  | forge/anvil 1.8.x | `curl -L https://foundry.paradigm.xyz \| bash && foundryup` |
| Python   | 3.12         | system Python + `venv`                   |
| network  | —            | only `127.0.0.1`; no external RPC        |

Install Python dependencies (pinned):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # direct deps
# or, for the exact frozen transitive environment used here:
# pip install -r requirements-lock.txt
```

> `uvicorn` is the plain build (not `uvicorn[standard]`); the `httptools` extra
> needs a C toolchain and is unnecessary on localhost.

## 3. Build the contracts

```bash
forge build
```

This writes JSON artifacts (ABI + bytecode) to `out/`, which the Python layer
loads directly.

> **Offline test base.** `lib/forge-std` is a small, vendored shim providing
> only the `Test`/`Vm`/`DSTest` surface this repo uses, so `forge test` needs no
> `git` dependency download. (The first `forge build` may still fetch the
> pinned `solc 0.8.24` compiler binary from the Foundry compiler host; it is
> cached under `~/.svm` afterwards.)

## 4. Start a local chain

Terminal 1 — Anvil with deterministic test accounts (default):

```bash
anvil --host 127.0.0.1 --port 8545 --chain-id 31337
```

## 5. Deploy (synthetic token + escrow)

Terminal 2:

```bash
export PATH="$PATH:$HOME/.foundry/bin"
source .venv/bin/activate
export RPC_URL=http://127.0.0.1:8545
# optional; defaults to Anvil account #0
# export PRIVATE_KEY=0xac0974...f2ff80
python scripts/deploy.py
```

Outputs `deployment/addresses.json` with the token/vesting addresses.

## 6. Run the HTTP API

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Interactive docs: <http://127.0.0.1:8000/docs>

### Endpoints

| Method | Path                              | Purpose                                   |
|--------|-----------------------------------|-------------------------------------------|
| GET    | `/health`                         | node + deployment connectivity            |
| POST   | `/schedules`                      | create a vesting schedule (locks tokens)  |
| GET    | `/schedules/{id}`                 | full schedule record                      |
| GET    | `/schedules/{id}/vested?timestamp=` | vested amount at a time (default now)   |
| GET    | `/schedules/{id}/releasable`      | vested but not yet withdrawn              |
| POST   | `/schedules/{id}/release`         | claim vested tokens (to beneficiary)      |
| POST   | `/schedules/{id}/revoke`          | owner: freeze curve, refund remainder     |
| GET    | `/token`                          | synthetic token metadata + deployer bal   |
| POST   | `/token/mint`  `/token/approve`   | local test helpers                        |

`release` is permissionless but funds only ever move to the recorded
beneficiary; `revoke` is owner-only.

### Quick curl example

```bash
# mint/approve are usually not needed (deployer pre-funded), but shown for clarity:
curl -s localhost:8000/health | jq

NOW=$(date +%s)
curl -s -X POST localhost:8000/schedules -H 'content-type: application/json' -d "{
  \"beneficiary\": \"0x70997970C51812dc3A010C7d01b50e0d17dc79C8\",
  \"total_amount\": 1000000000000000000000,
  \"start\": $NOW,
  \"cliff\": $((NOW+10)),
  \"end\": $((NOW+100))
}" | jq
```

## 7. Tests

### Foundry (in-process EVM, no node needed)

```bash
forge test -vvv
```

### Python end-to-end (boots its own Anvil on port 8546)

```bash
pytest
```

`tests/conftest.py` automatically: launches Anvil, runs `forge build`, deploys
via `scripts/deploy.py`, then runs contract-integration and HTTP tests.

### Demo script

With a node + deployment running (steps 4–5):

```bash
python scripts/demo.py
```

It performs claims → revoke → final claim on a non-divisible total and prints
the conservation result.

---

## 8. Vesting math

```
vested(now) =
    0                                   if now < cliff
    total * (now - start) / (end-start) if cliff <= now < end
    total                               if now >= end
```

After revocation the effective clock is frozen at `revokedAt`, so vested never
grows past it. Integer division rounds down; sub-step dust stays in escrow and
settles in full at `end` (or at the frozen vested amount on revoke). On revoke:

```
refund = total - vested(revokedAt)
```

All multiplication uses Solidity 0.8 checked arithmetic (reverts on overflow);
amounts/timestamps are `uint256`.

## 9. Acceptance coverage map

| Requirement                                   | Foundry test                         | Python test |
|-----------------------------------------------|--------------------------------------|-------------|
| start/cliff/end boundaries                    | `test_BeforeCliff…`, `test_LinearProgressionAndFullAtEnd` | `test_before_cliff_zero_then_linear_to_end` |
| repeated claims                               | `test_ReleasesOnlyReleasableDelta`   | `test_repeated_claim_only_pays_new_delta` |
| revoke interleaved with claims, vested kept   | `test_RevokeAfterPartialClaim…`, `test_RevokeBeforeClaim…` | `test_claim_then_revoke_then_claim_keeps_vested` |
| revoke before cliff / after end               | `test_RevokeBeforeCliff…`, `…AfterEnd` | `test_revoke_before_cliff_refunds_all` |
| non-divisible total (dust)                    | `test_NonDivisibleTotalDustSettlesAtEnd` | `test_non_divisible_*` |
| claimed + refund == locked, escrow == 0       | assertion in every revoke test       | `test_conservation_interleaved_lifecycle` |
| overflow / bounded curve                      | `testFuzz_VestedBounded`             | — |

## 10. Local-only / security notes

* The default key is **Anvil's public test key #0**. Never fund these accounts
  on a real network.
* `SyntheticToken.mint` is unrestricted by design (synthetic test asset).
* The API signs with one local key; it has no auth because it binds to
  `127.0.0.1` and is intended solely for local evaluation.

---

## 11. Observed results (this environment)

Toolchain: `forge/anvil 1.8.3`, `solc 0.8.24`, Python `3.12`, `web3 8.0.0`,
`fastapi 0.141.1`, `pytest 9.1.1`.

```text
$ bash scripts/run_all_tests.sh
==> [1/3] forge build
==> [2/3] forge test -vv
Suite result: ok. 16 passed; 0 failed; 0 skipped
   (includes testFuzz_VestedBounded: 256 runs proving vested <= total)
==> [3/3] pytest   (boots its own Anvil on 127.0.0.1:8546)
20 passed, 1 warning in ~10s
```

`scripts/demo.py` against a live node (non-divisible total 12345 SYN + 678 wei,
cliff t+50, end t+500, claims at t+60 / t+300, revoke at t+400):

```text
beneficiary claimed : 9876.000000
owner refund        : 2469.000000
escrow remainder    : 0.000000
claimed + refund    : 12345.000000
initial locked      : 12345.000000
CONSERVATION CHECK: PASS
```

The 678 wei of integer dust is retained by the escrow's floor-division curve and
settles to the beneficiary in full at the end / at the frozen vested amount; the
identity `claimed + refund == locked` holds to the wei with escrow balance 0.

### Notes / limitations
* Only the test surface of `forge-std` (`Test`/`Vm`/`DSTest`) is vendored under
  `lib/forge-std`, because the sandbox network throttled dependency downloads.
  The compiler (`solc 0.8.24`) is fetched once by forge and cached.
* `evm_setTime` on Anvil 1.8 takes **seconds**; the Python test/demo time-travel
  helper reflects that (it is not the older milliseconds convention).
* The HTTP layer intentionally has no authentication and a single local signer.
* No public network, Infura/Alchemy endpoint, or funded key is used anywhere.
