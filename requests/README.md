# Example JSON requests

Each file is one request document for `target/release/ecstripe <command> <file>`.
Paths inside the examples are relative to the working directory you run the
binary from.

| File | Command | Demonstrates |
|---|---|---|
| `encode.json` | `encode` | k=3, m=2, tagged SHA-256 |
| `decode.json` | `decode` | recover with shards 1 and 4 lost, verify hash |
| `decode_too_many_lost.json` | `decode` | 3 lost > m=2 → `not_enough_shards` error |
| `info.json` | `info` | print the validated container header |
| `corrupt.json` | `corrupt` | flip one payload byte (framing kept intact) |

## Typical session

```bash
head -c 5000 /dev/urandom > input.bin

ecstripe encode requests/encode.json
ecstripe info   requests/info.json
ecstripe decode requests/decode.json
cmp input.bin decoded.bin

# Loss beyond the parity budget:
ecstripe decode requests/decode_too_many_lost.json   # exits 1, status=error

# Silent corruption needs an external check — see scripts/acceptance.sh
# steps 5 and 6 for the full corrupt -> decode-without/with-hash sequence.
```

The full automated walkthrough (including the length-mismatch and
silent-corruption cases) is `scripts/acceptance.sh`; the Rust suite is
`tests/integration.rs`.
