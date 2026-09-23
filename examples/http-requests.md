# HTTP request examples

All requests target one local server. Start it with:

```bash
cargo run --release -- init   /tmp/repo --block-size 4
cargo run --release -- serve  /tmp/repo --addr 127.0.0.1:8080
```

Request bodies for `PUT /block` and `POST /build` are **raw bytes**
(`Content-Type: application/octet-stream`), not JSON. All responses are JSON.

## Health / introspection

```bash
curl -s http://127.0.0.1:8080/
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/root
```

## Build a whole file (journaled full rebuild)

```bash
printf 'abcdefghijk' > payload.bin
curl -s -X POST --data-binary @payload.bin http://127.0.0.1:8080/build
# -> {"ok":true,"data_len":11,"n":3,"root":"…","root_hex":"…"}
```

## Range proofs (verifier never reads the rest of the file)

```bash
# first block
curl -s "http://127.0.0.1:8080/range?which=first"
# last block (here the 3-byte short block)
curl -s "http://127.0.0.1:8080/range?which=last"
# explicit half-open block interval [start,end)
curl -s "http://127.0.0.1:8080/range?start=1&end=3"
```

Each response carries everything verification needs:
`block_size, data_len, n, start, end, root, blocks[], proof[]`.

## Stateless verification

`POST /verify` is self-contained: it hashes only the supplied blocks and
climbs the supplied proof. It never opens the repository the response came
from. The body of a `/range` response is already a valid `/verify` request.

```bash
curl -s "http://127.0.0.1:8080/range?which=first" > req.json
# (verify on a different process/repo, or the same one — result is identical)
curl -s -X POST --data-binary @req.json http://127.0.0.1:8080/verify
# -> {"valid": true}
```

Offline equivalent (no server at all):

```bash
cargo run --release -- verify req.json
```

## Incremental single-block writes

```bash
# overwrite block 2 with a full block
printf 'ijkl' | curl -s -X PUT --data-binary @- "http://127.0.0.1:8080/block?index=2"
# then append a new block (only legal once the last block is full-size)
printf 'mn'   | curl -s -X PUT --data-binary @- "http://127.0.0.1:8080/block?index=3"
```

Appending while the current last block is short returns HTTP 400:

```bash
# after the 11-byte build, last block "ijk" is short:
printf 'zzzz' | curl -s -X PUT --data-binary @- "http://127.0.0.1:8080/block?index=3"
# -> HTTP 400 {"error":"cannot append block 3: current last block is short …"}
```

## Empty file

```bash
curl -s -X POST --data '' http://127.0.0.1:8080/build
curl -s "http://127.0.0.1:8080/range?which=first" > empty.json
curl -s -X POST --data-binary @empty.json http://127.0.0.1:8080/verify
# -> {"valid": true}, root equals SHA256("merkle-store:empty-root-v1")
```

## Expected rejection responses

| Attack | Request variant | `valid` | `error` |
|--------|-----------------|---------|---------|
| flip a block byte | `examples/verify-tampered-block.json` | false | leaf N hash mismatch with supplied block bytes |
| claim a different file length | `examples/verify-forged-length.json` | false | declared byte length disagrees with leaf count and block size |
| swap proof siblings | `examples/verify-misaligned-proof.json` | false | misaligned proof node: expected node …, got … |
| empty tree, wrong root | any root ≠ empty sentinel | false | zero-leaf tree root does not match empty-root sentinel |
| truncated/extended proof | remove/append a proof node | false | proof truncated before reaching the root / proof contains unconsumed nodes |
