# Complete simulated transaction walk-through

This example creates a merchant, an operator, authorizes and captures a $40.00
payment, partially refunds it, settles the day, runs a post-settlement refund,
and reconciles against the simulated acquirer. All commands assume the API is
listening on `localhost:8080` and use `jq` for readability.

## 0. Log in as admin

```bash
ADMIN_TOKEN=$(curl -s -XPOST localhost:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@clearsettle.local","password":"admin12345"}' | jq -r .token)
```

## 1. Create a merchant (returns the API key exactly once)

```bash
curl -s -XPOST localhost:8080/v1/admin/merchants \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Coffee Demo Co","fee_bps":290,"fee_fixed":30}'
```

```json
{
  "merchant": {
    "id": "1d4f…",
    "name": "Coffee Demo Co",
    "fee_bps": 290,
    "fee_fixed": 30,
    "status": "active",
    "api_key_mask": "cs_live_ab12…"
  },
  "api_key": "cs_live_AaBbCc…(save this now)…",
  "warning": "Store this API key now. It is shown only once and cannot be recovered."
}
```

```bash
MID=1d4f…                       # the merchant id
APIKEY='cs_live_AaBbCc…'        # the returned plaintext key
```

## 2. Create an operator user for the merchant

```bash
curl -s -XPOST localhost:8080/v1/admin/users \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"ops@coffee.test\",\"password\":\"operator-password\",\"role\":\"operator\",\"merchant_id\":\"$MID\"}"

OP_TOKEN=$(curl -s -XPOST localhost:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ops@coffee.test","password":"operator-password"}' | jq -r .token)
```

## 3. Authorize $40.00

```bash
curl -s -XPOST localhost:8080/v1/payments/authorize \
  -H "Authorization: Bearer $APIKEY" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: coffee-auth-0001' \
  -d '{"amount":4000,"external_ref":"order-1001"}'
```

```json
{
  "id": "7b0e…",
  "status": "authorized",
  "authorized_amount": 4000,
  "captured_amount": 0,
  "fee_amount": 0,
  "refunded_amount": 0,
  "expires_at": "…+00:00",
  "settled": false
}
```

```bash
PID=7b0e…
```

Re-sending the **same key and body** returns the identical stored response with
the header `Idempotent-Replay: true` and does not create a second payment.
Changing the amount while reusing the key returns `409 idempotency_conflict`.

## 4. Capture (full amount)

```bash
curl -s -XPOST "localhost:8080/v1/payments/$PID/capture" \
  -H "Authorization: Bearer $APIKEY" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: coffee-cap-0001' -d '{}'
```

```json
{
  "id": "7b0e…",
  "status": "captured",
  "captured_amount": 4000,
  "fee_amount": 146,
  "refunded_amount": 0
}
```

The ledger now holds a balanced posting: CASH **+4000**, FEE_REVENUE **−146**,
PAYABLE **−3854** (the merchant is owed $38.54).

## 5. Partial refund of $10.00

```bash
curl -s -XPOST "localhost:8080/v1/payments/$PID/refund" \
  -H "Authorization: Bearer $APIKEY" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: coffee-ref-0001' \
  -d '{"amount":1000,"reason":"customer goodwill"}'
```

```json
{
  "refund": {"amount": 1000, "fee_refund": 37, "payment_id": "7b0e…"},
  "payment": {"status": "partially_refunded", "refunded_amount": 1000, "refunded_fee": 37}
}
```

`fee_refund = round(146 * 1000 / 4000) = 37`. Running totals: CASH **+3000**,
FEE_REVENUE **−109**, PAYABLE **−2891**.

Attempting another $30.01 refund fails with `422 refund_too_large` (only $30.00
remains). Attempting a refund on a separate authorized-but-never-captured
payment fails with `409 illegal_transition`.

## 6. Settle the business day

The worker settles capture-days older than the 24h horizon automatically. To
run it immediately for a specific day:

```bash
DAY=$(date -u -d 'yesterday' +%F)   # the payment's capture day in a demo
curl -s -XPOST localhost:8080/v1/jobs/settle \
  -H "Authorization: Bearer $OP_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"day\":\"$DAY\"}"
```

```json
{
  "BatchID": "…", "Status": "completed",
  "GrossCaptured": 4000, "TotalFees": 146,
  "TotalRefunds": 1000, "RefundedFees": 37,
  "NetAmount": 2891, "PaymentCount": 1
}
```

`net = 4000 − 1000 − 146 + 37 = 2891` cents. The payout posting debits PAYABLE
back to zero and credits CASH −2891 (funds leave the platform to the merchant).
Calling the same endpoint again returns the same batch with `Skipped: true` and
creates nothing new.

## 7. Reconcile against the simulated acquirer

```bash
curl -s -XPOST "localhost:8080/v1/jobs/reconcile/$DAY" \
  -H "Authorization: Bearer $OP_TOKEN"

curl -s "localhost:8080/v1/reconciliations/$DAY" \
  -H "Authorization: Bearer $OP_TOKEN"
```

```json
{
  "status": "completed",
  "items": [
    {"check":"capture_gross","expected":4000,"actual":4000,"difference":0,"severity":"match"},
    {"check":"capture_fees","expected":146,"actual":146,"difference":0,"severity":"match"},
    {"check":"refund_gross","expected":1000,"actual":1000,"difference":0,"severity":"match"},
    {"check":"refund_fees","expected":37,"actual":37,"difference":0,"severity":"match"},
    {"check":"ledger_balance","expected":0,"actual":0,"difference":0,"severity":"match"}
  ]
}
```

If the simulated acquirer were off by more than 5 cents on any check, that item
would carry `"severity":"discrepancy"`, the run summary's `discrepancies` count
would be non-zero, and the worker would log `RECON DISCREPANCY …`.

## 8. Read balances and audit log

```bash
curl -s localhost:8080/v1/accounts/balance -H "Authorization: Bearer $OP_TOKEN"
# {"merchant_id":"…","payable":0,"as_of":"…"}   # zero right after settlement

curl -s "localhost:8080/v1/audit-logs?action=payment.refund" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq '.audit_logs[0]'
```

The operator token is refused for every `/v1/admin/*` route; the auditor token
can GET resources but is rejected (403) on all mutating routes; an operator for
merchant A cannot read or mutate merchant B's payments (queries are always
filtered by the authenticated merchant id).
