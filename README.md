# IFIX — Immutable File Index

A pure-backend, streaming binary codec for a **read-only tree index** with a
**multi-layer offset table** that lets a reader touch only the pages on a
single path to a target node. Written in safe-ish Rust (one hand-rolled
`mmap` FFI module), with the format itself, CRC-32, base64 and all index
algorithms implemented in this crate — the only third-party dependency is
`serde_json`.

Highlights:

- **Documented binary format** — see [`docs/FORMAT.md`](docs/FORMAT.md).
- **Streaming encode/decode** — blob bytes pass through a fixed 64 KiB
  buffer; per-node metadata is the only state proportional to node count and
  is bounded by configurable limits.
- **Multi-layer B-tree offset index** — a lookup reads one index page per
  level plus the target node record; it never scans the node block.
- **Strict validation of untrusted files** — every offset, count and
  interval is checked with checked arithmetic; declared sizes never drive
  allocation; cyclic references, overlapping node/data intervals, mutated
  offsets/lengths and truncation are all rejected with stable error codes.
- **Read vs mmap parity** — the same validation/query code runs over a
  `pread` backend and a private read-only `mmap` backend; tests assert
  identical results and byte-identical file output.

There is deliberately **no frontend**: control is JSON-in / JSON-out.

## Layout

```
src/
  crc.rs       CRC-32 (IEEE) hand-rolled table
  base64.rs    base64 codec for inline blob bytes in JSON
  format.rs    constants + configurable Limits
  model.rs     input tree model, JSON parsing, bounded preparation
  writer.rs    streaming encoder (breadth-first layout, bottom-up TOC)
  storage.rs   Storage trait: FileStorage / MmapStorage / MemStorage
  toc.rs       multi-layer index parsing, validation, paged navigation
  reader.rs    validating open(), path lookup, list, blob streaming
  control.rs   JSON command facade (build/lookup/list/read/verify/stats)
  main.rs      thin CLI
docs/FORMAT.md normative format specification
tests/         integration, mutation/fuzz-rejection, round-trip, CLI tests
samples/       example JSON requests
```

## Build and test

Requires a Rust toolchain (developed against 1.75; edition 2021). Offline
builds are supported (`.cargo/config.toml` sets `net.offline = true`).

```bash
cargo build --release
cargo test
cargo clippy --all-targets
cargo fmt --check
```

## Quick start

Build an index from a JSON tree, then query it:

```bash
# build
./target/release/ifix build --output samples/demo.ifix --request samples/build.json

# verify structure + checksums (pread backend)
./target/release/ifix verify --index samples/demo.ifix --backend read

# verify through the mmap backend
./target/release/ifix verify --index samples/demo.ifix --backend mmap

# resolve a path (touches only its ancestor chain)
./target/release/ifix lookup --index samples/demo.ifix --path /photos/a/1.txt

# list a directory
./target/release/ifix list   --index samples/demo.ifix --path /photos

# stream a file's blob, returned inline as base64 (bounded)
./target/release/ifix read   --index samples/demo.ifix --path /photos/a/1.txt

# one-shot JSON control on stdin
./target/release/ifix json < samples/lookup.json
```

All responses use one envelope:

```json
{ "status": "ok",    "data": { ... } }
{ "status": "error", "code": "NOT_FOUND", "message": "path not found: x" }
```

Exit code is `0` on `ok`, `1` on error.

## JSON control surface

| Command  | Key fields | Meaning |
|----------|-----------|---------|
| `build`  | `output`, `root`, optional `log2_page` (5..=16, default 8), optional `limits` | Encode a tree to a file |
| `verify` | `index`, optional `backend` | Full validation; returns counts/height |
| `lookup` | `index`, `path`, optional `backend` | Resolve a `/`-separated path |
| `list`   | `index`, `path` | Direct children of a directory |
| `read`   | `index`, `path`, optional `max_inline_base64` | Blob bytes as base64 |
| `stats`  | `index` | Region offsets/sizes and TOC height |

A tree node:

```json
{
  "name": "a",
  "type": "dir",
  "children": [
    { "name": "1.txt", "type": "file", "content_b64": "aGVsbG8=" },
    { "name": "2.txt", "type": "file", "content_file": "blobs/2.bin" }
  ]
}
```

- `type` is `"dir"` or `"file"`. The build root must be a directory with an
  empty `name`.
- A file carries **either** `content_b64` (inline bytes) **or**
  `content_file` (a relative path resolved under `--base`, refusing
  absolute paths and `..` traversal). Neither means an empty file.
- Sibling names must be unique; they are sorted on write so the reader can
  binary-search a directory.
- `log2_page` sets TOC fanout `F = 2^log2_page`.

## Safety / validation model

When opening an untrusted file the reader proceeds in this order (see
`docs/FORMAT.md` §6):

1. Parse and sanity-check the 48-byte header (magic, version, flags,
   fanout). No heap allocation yet.
2. Verify the three regions exactly tile the file using checked
   arithmetic; cross-check declared counts/lengths against the measured file
   length and [`Limits`].
3. Parse the TOC tail and fully validate the multi-layer index (level
   counts, key sequences, page-aligned in-range pointers, root pointer)
   before trusting it.
4. Validate every 48-byte node record (per-record CRC, types, child
   intervals/sentinel, name and blob ranges).
5. Verify the parent/child graph: exactly one parent per non-root node
   (rejects sharing/overlaps), root reachability over all nodes (rejects
   orphans/cycles), sorted unique UTF-8 sibling names.
6. Verify all data intervals (names + blobs) are pairwise disjoint.
7. Stream-verify every blob CRC-32, then the whole-file CRC-32, both in
   fixed-size buffers.

Declared counts/offsets never drive allocation. Output collection is capped
by `max_output_bytes`; request input is capped by `max_input_bytes`; lookup
depth is capped by `max_depth`.

## Error codes

Representative stable codes (all returned in the error envelope `code`):

`TRUNCATED`, `BAD_MAGIC`, `BAD_VERSION`, `BAD_FLAGS`, `BAD_FANOUT`,
`BAD_HEADER`, `BAD_CRC`, `REGION_TILING`, `TOC_STRUCTURE`, `TOC_POINTER`,
`NODE_CRC`, `NODE_TYPE`, `NODE_FLAGS`, `NODE_CYCLE`, `CHILD_RANGE`,
`CHILD_OVERLAP`, `ORPHAN_NODE`, `NAME_RANGE`, `NAME_ORDER`, `NAME_INVALID`,
`DATA_RANGE`, `DATA_OVERLAP`, `BLOB_CRC`, `NOT_A_DIR`, `NOT_A_FILE`,
`NOT_FOUND`, and `LIMIT_*` for the configurable caps.

## Test inventory

- `tests/e2e_test.rs` — build/list/lookup/read, depth & output caps, read
  vs mmap parity, byte-identical file/memory output, multi-level TOC
  traversal.
- `tests/mutations_test.rs` — 27 rejection cases: mutated offsets/lengths,
  self cycles, overlapping child and data intervals, orphans, truncation at
  every region boundary, bad magic/version/flags, TOC pointer/key/tail
  corruption, stale per-record and whole-file CRCs.
- `tests/roundtrip_test.rs` — deterministic seeded random trees across
  fanouts 32/64/256/4096 and a 3001-node tree forcing a 4-level TOC.
- `tests/cli_test.rs` — black-box CLI and stdin JSON mode.
