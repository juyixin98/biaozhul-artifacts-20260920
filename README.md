# Bounded Order Signature Settlement (local test chain)

A maker signs an order **off-chain**; a taker settles it **on-chain** by
submitting the signature. The order is *bounded*: it supports partial fills,
maker nonce cancellation, an expiry, and a capped fee. Everything runs against
a **local Anvil** node using the well-known, funded **test keys** — it is a
demo/test artifact and is not safe for real funds.

Stack: **Solidity 0.8.26 + Foundry (forge/anvil)**, **Python 3.12 + FastAPI +
web3.py**, signatures are **EIP-712** with the domain separator bound to the
chain id and the settlement contract address.

---

## What it does

The maker signs an `Order`:

| field | meaning |
|---|---|
| `maker` | signs and sells `makerToken` |
| `taker` | the only allowed counterparty, or `address(0)` = anyone |
| `makerToken` / `takerToken` | the two ERC-20s (two pre-deployed mock tokens) |
| `makerAmount` | total maker token on offer (**gross** of fee) |
| `takerAmount` | total taker token owed for a full fill |
| `nonce` | maker-chosen; cancellable up-front via `cancelNonce` |
| `deadline` | last valid block timestamp (`timestamp <= deadline`) |
| `feeRecipient` | paid in maker token; zero address means fee must be 0 |
| `maxFeeAmount` | **cumulative** fee cap over the order's whole life |

Guarantees enforced by `BoundedOrderSettlement`:

- **Partial fills**, repeatable until `makerAmount` is exhausted. Cumulative
  maker spend can never exceed the signed `makerAmount`.
- **Rounding favors the maker.** The taker amount due for a slice is
  `ceil(proceeds · takerAmount / makerAmount)`; the final fill that exhausts
  the order pays exactly the remaining signed taker amount, so no dust is left.
- **Fee cap.** `fee` per fill plus all earlier fees must stay ≤ `maxFeeAmount`,
  and a fee is impossible unless a recipient was signed.
- **Nonce cancellation.** `cancelNonce(nonce)` blocks every still-unfilled
  order using `(maker, nonce)`. Cancellation is keyed by the *maker's* address
  plus nonce; a third party cancelling the same number has no effect.
- **Expiry** via `deadline` (boundary: a block with `timestamp == deadline`
  still fills).
- **Replay safety.** Fill accounting is per order hash; once an order is
  exhausted, the same signature cannot move more than the signed amounts.
- **Domain binding.** The EIP-712 domain includes `chainId` and this contract's
  address, so a signature is invalid on another chain, another deployment, or
  after any field is tampered with. Signatures must be low-s (EIP-2).
- **Atomic settlement.** Maker→taker, maker→feeRecipient and
  taker→maker transfers all happen in one transaction; any failed token
  transfer reverts the entire fill and its bookkeeping.

---

## Repository layout

```
src/MockERC20.sol                dependency-free mock ERC-20 (anyone can mint)
src/BoundedOrderSettlement.sol   the settlement contract
test/BoundedOrderSettlement.t.sol  Foundry unit tests (24)
app/                             FastAPI + web3.py service
  config.py                      test keys, env settings, deployment loader
  abi.py                         loads ABI JSON from out/
  signing.py                     raw eth_abi EIP-712 digest + ECDSA signing
  chain.py                       web3.py client, txs, revert decoding
  main.py                        HTTP API
scripts/deploy.py                deploys contract + 2 tokens, mints, approves
scripts/example.py               end-to-end HTTP walkthrough
tests/                           pytest integration tests vs live Anvil (16)
requirements.in                  direct deps
requirements-lock.txt            fully resolved, tested versions
```

---

## Prerequisites

- [Foundry](https://book.getfoundry.sh/getting-started/installation)
  (`forge`, `anvil`, `cast` on `PATH`)
- Python 3.12 (a `.venv` is created below)

The suite was run with `forge 1.8.3` and `solc 0.8.26`.

---

## Setup

```bash
# 1. build contracts
forge build

# 2. Python virtualenv + pinned deps
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # exact tested set
# (or: pip install -r requirements.in for direct deps only)
```

---

## Running the system (manual, 3 terminals)

```bash
# terminal 1 — local chain (chain id 31337, the Anvil default)
anvil --chain-id 31337

# terminal 2 — deploy contracts + mint/approve, writes deployment.json
RPC_URL=http://127.0.0.1:8545 CHAIN_ID=31337 \
  .venv/bin/python -m scripts.deploy

# terminal 3 — HTTP API
RPC_URL=http://127.0.0.1:8545 CHAIN_ID=31337 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then run the end-to-end HTTP example:

```bash
.venv/bin/python -m scripts.example
```

Interactive API docs are at <http://127.0.0.1:8000/docs>.

### Environment variables

| var | default | purpose |
|---|---|---|
| `RPC_URL` | `http://127.0.0.1:8545` | Anvil JSON-RPC endpoint |
| `CHAIN_ID` | `31337` | expected chain id (must match the node) |
| `DEPLOYMENT_FILE` | `./deployment.json` | output/input of the deploy step |
| `DEPLOYER_KEY` / `MAKER_KEY` / `TAKER_KEY` / `FEE_RECIPIENT_KEY` | Anvil keys 0–3 | demo role keys |

---

## HTTP API

| method & path | body / notes |
|---|---|
| `GET /health` | chain id, block, settlement address |
| `GET /config` | token addresses and demo account addresses |
| `POST /orders` | create **and sign** an order with the configured maker key; returns `order`, `orderHash`, `signature` |
| `POST /orders/fill` | `{order(+signature), spent, fee}`; simulates, settles, returns `takerDue` and cumulative fill status |
| `POST /orders/cancel` | `{nonce}` signed by the maker |
| `GET /orders/{orderHash}` | cumulative `filledMakerAmount` / `cumulativeFee` / `filledTakerAmount` |
| `GET /tokens/{token}/balance/{holder}` | ERC-20 balance |

`POST /orders` is a demo convenience that signs with the single configured
maker key. In a real deployment the maker would sign elsewhere and only the
signature would travel; `scripts.example.py` and the integration tests show
the order+signature JSON shape used by `/orders/fill`.

### Quick `curl`

```bash
BASE=http://127.0.0.1:8000
# create + sign
O=$(curl -s $BASE/orders -H 'content-type: application/json' \
  -d '{"makerAmount":100,"takerAmount":3,"nonce":1,"deadline":9999999999}')
# add signature into the order object and fill 33
…
```

(The Python example in `scripts/example.py` is the easier path than crafting
the nested JSON by hand.)

---

## Tests

### Foundry unit tests (in-process EVM, no node needed)

```bash
forge test -vv
```

Covers full/partial fills, the two partial-fill rounding cases, fee caps
(per-fill and cumulative), cancellation and the wrong-address cancel race,
replay/over-fill, EIP-712 domain binding across a second deployment, wrong
signer / tampered order / high-`s` malleability, expiry boundary, designated
taker, and transfer-failure rollback.

### Python integration tests (live Anvil over HTTP-RPC)

`tests/conftest.py` starts an **isolated Anvil on a free port**, deploys the
contracts with `scripts.deploy`, and runs both direct web3.py scenarios and
the FastAPI app through an in-process test client:

```bash
PATH="$HOME/.foundry/bin:$PATH" .venv/bin/python -m pytest
```

Acceptance scenarios are explicitly covered:

- **two partial fills with rounding** (`tests/test_chain.py
  ::test_two_partial_fills_rounding_and_conservation`, plus the small-number
  100→3 case in both suites) — verifies the ceil rule and final dust clearing;
- **cancel race** (`::test_cancel_and_fill_race_only_one_wins`) fires cancel
  and fill concurrently and asserts exactly one outcome with no half-state;
- **replay** (`::test_replay_same_signature_after_full_fill`,
  `test_replay_rejected_via_api`);
- **transfer failure** (`::test_failed_transfer_rolls_back_entire_fill`) —
  a taker with no B allowance fails the last transfer and the earlier
  maker-token transfer and all bookkeeping roll back;
- **asset conservation + volume bounds** — the conservation test checks both
  tokens across maker/taker/fee-recipient, and asserts the maker gives exactly
  the signed amount and total volume never exceeds the signed allowance.

### Run everything

```bash
forge test
PATH="$HOME/.foundry/bin:$PATH" .venv/bin/python -m pytest
```

---

## Design notes

- **Why gross `makerAmount`?** `spent` on a fill is gross: the taker receives
  `spent - fee` of maker token and `fee` goes to the recipient. Pro-rata taker
  payment is computed on the *proceeds* (`spent - fee`), so fees never buy
  taker token.
- **Three cumulative counters** per order hash — maker spent, fees paid, taker
  received — make all bounds exact and the conservation checks straightforward.
- **No proxy / no allowlist manager**; the contract is intentionally small and
  reads no price from anywhere. Pricing is purely the signed ratio.
- **Signatures are verified with `ecrecover` plus a low-s check**; EIP-1271
  contract wallets are not supported in this demo.

## Security / scope

Local demo only. The mock token lets anyone mint and carries no value; the
default keys are publicly known. Do not point this at a public network or real
assets.

## Known limitations / honest caveats

- **No EIP-1271.** Signatures are validated with `ecrecover`, so makers must be
  EOAs; contract-wallet signatures are not supported.
- **Dust on adversarial ratios.** Maker-favorable ceil rounding means a maker
  can be slightly *over*-paid in taker token relative to a strict pro-rata
  slice (this is the intended protection). On pathological ratios with many
  tiny partial fills, cumulative ceil error could make the final
  dust-clearing subtraction (`takerAmount - alreadyPaid`) underflow, in which
  case the last sliver cannot be filled. The signed principal is never
  exceeded; practical ratios (e.g. 18-decimal tokens) cannot hit this. A
  production system would use a `mulDiv` library and cap per-order fill count.
- **Single demo maker key.** `POST /orders` signs with the one configured
  maker key for convenience; it is not a multi-user signer service.
- **Synchronous settlement only.** There is no order book, mempool, matching
  engine, gasless/meta-transaction relayer, or ERC-20 fee-on-transfer token
  support; the mock token is a plain fixed-supply ERC-20.
- **No formal audit.** Tests are extensive for the stated acceptance scenarios
  but do not constitute an audit.

## Test results (as run for this delivery)

- `forge test`: **24 passed / 0 failed** (forge 1.8.3, solc 0.8.26, via-IR).
- `pytest` (live Anvil over HTTP-RPC): **16 passed / 0 failed**
  (Python 3.12, web3.py 6.20.3, FastAPI 0.115.0).
- Manual live run of `scripts.example.py` against a fresh Anvil: two partial
  fills + final fill settle 100 A for 3 B, a replay is rejected with
  `FillExceedsOrder`, and a post-cancel fill is rejected with
  `NonceCancelled`. A separate fee-bearing run verified conservation
  (1000 A = 960 to taker + 40 fee; 400 B taker→maker) and the cumulative fee
  cap.

