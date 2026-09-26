# Run log

Honest record of the commands actually run, their results, problems found
and how they were resolved. Times are 2026-09-25 (local).

## Environment

- Host: Linux 6.8.0-90-generic, x86_64.
- Two Rust toolchains were present:
  - a rustup-managed `stable` (1.98.1), reached through the
    `/home/admin/.cargo/bin/cargo` shim;
  - a system toolchain **`/usr/bin/cargo` / `/usr/bin/rustc`, Rust 1.75.0**
    (sysroot `/usr`).
- The project builds **offline**: `.cargo/config.toml` sets
  `net.offline = true`. The only third-party crate is `serde_json`
  (1.0.151), available in the local cargo cache.

### Toolchain incident (resolved)

Invoking the rustup shim (`/home/admin/.cargo/bin/cargo`) triggered an
automatic channel sync for 1.98.1. The sync failed
(`bad checksum for cached download`, then rename/remove errors) and left
`~/.rustup/toolchains/stable-.../bin` deleted — i.e. it broke the 1.98
toolchain. All subsequent builds use the untouched system toolchain
explicitly:

```bash
PATH=/usr/bin:$PATH /usr/bin/cargo <cmd> --offline
```

The code stays compatible with Rust 1.75 (edition 2021; e.g. `div_ceil`,
stabilized in 1.73, is the newest API relied upon).

## Build / lint / test commands and results

```bash
PATH=/usr/bin:$PATH cargo build --offline            # debug build: OK
PATH=/usr/bin:$PATH cargo build --release --offline  # LTO release: OK
PATH=/usr/bin:$PATH cargo fmt --check                # clean
PATH=/usr/bin:$PATH cargo clippy --offline --all-targets   # 0 warnings
PATH=/usr/bin:$PATH cargo test --offline             # all green (50 tests)
```

Final test totals:

| Binary | Tests | Result |
|---|---:|---|
| lib unit tests (crc, base64) | 4 | ok |
| `tests/cli_test.rs` | 3 | ok |
| `tests/e2e_test.rs` | 8 | ok |
| `tests/mutations_test.rs` | 28 | ok |
| `tests/roundtrip_test.rs` | 3 | ok |
| `tests/streaming_test.rs` | 4 | ok |
| **Total** | **50** | **all pass** |

## Real defects found by running (and fixed)

These were genuine code/spec mistakes surfaced by actually building and
running, not test-only issues:

1. **Writer panic on file CRC write.** The header stored `file_crc32` as a
   u32 but the writer did
   `header[40..48].copy_from_slice(&file_crc.to_le_bytes())`, copying 4
   bytes into an 8-byte slice. First end-to-end CLI build aborted with
   `source slice length (4) does not match destination slice length (8)`.
   Fixed to write `header[40..44]` and leave `44..48` as reserved zero; the
   reader now also rejects nonzero reserved bytes there (`BAD_HEADER`), and
   `docs/FORMAT.md` documents the field as a 4-byte u32 plus 4 reserved
   bytes.
2. **Empty inline blob produced an invalid record.**
   `content_b64: ""` went through the `Blob::Inline` branch, which set a
   non-zero `data_offset` with `data_len = 0`; the encoder's own invariant
   check then rejected the file (`empty file node N has nonzero
   offset/hash`). Found by the seeded random round-trip test. Fixed so an
   empty inline blob encodes exactly like an empty file `(0,0,0)`.
3. **Magic length mismatch (caught at compile time).** `MAGIC` was declared
   `[u8; 6]` but initialized from the 7-byte `b"IFIX1\0\0"`. Redefined as
   the 6-byte `IFIX1\0` and the spec updated.
4. **Root-type gap.** A crafted single-node file whose root was a `file`
   type could otherwise be accepted. Added an explicit
   "root node (id 0) must be a directory" check (`NODE_TYPE`) plus a
   mutation test.
5. **Spec drift.** The format table listed `blob_hash` as 8 bytes and
   omitted the `reserved0` field at node-record offset 20; corrected to the
   implemented 4-byte u32 + 4 reserved bytes.

Test-only corrections (assertions that were wrong, not the implementation):

- A parity test compared a file build at `log2_page=8` to a memory build at
  the default `log2_page=5`; differing TOC/CRC is expected. Aligned the
  fanouts.
- A wide-tree test expected a 3-level TOC for 3001 nodes at fanout 32; the
  true height is 4 (3001 → 94 → 3 → 1). Corrected.
- A "two parents overlap" mutation made a node claim *itself*, which is
  correctly caught earlier as `NODE_CYCLE`; changed it to claim a sibling id
  so it exercises `CHILD_OVERLAP`.

## Acceptance criteria — observed behavior

- **Mutated offsets / lengths:** e.g. flipping `data_bytes` or
  `nodes_offset` yields `REGION_TILING`; a blob `data_len` overflow yields
  `DATA_RANGE`; a name offset out of range yields `NAME_RANGE`.
  Verified by unit tests and the live CLI.
- **Cyclic references:** a node listing itself → `NODE_CYCLE`; any sharing
  is also ruled out structurally by exact-one-parent counting.
- **Node overlap:** two parents claiming one id → `CHILD_OVERLAP`.
- **Data overlap:** two blobs/names sharing bytes → `DATA_OVERLAP`.
- **Truncation:** cuts at every region boundary and the header are rejected
  (`TRUNCATED` / `REGION_TILING`); a tiny file declaring `node_count =
  1_000_000` is rejected before any proportional allocation.
- **Valid files agree between backends:** the same index opened with the
  `pread` backend and the private `mmap` backend returns identical ids,
  names, sizes and blob bytes (`tests/e2e_test.rs`), and file-vs-memory
  builds of the same tree + fanout are byte-identical.

Live CLI evidence (release binary):

```text
$ ifix verify --index samples/demo.ifix --backend read   -> ok, 11 nodes
$ ifix verify --index samples/demo.ifix --backend mmap   -> ok, identical
$ ifix lookup --index samples/demo.ifix --path /photos/a/1.txt -> id 8, size 5
$ truncate -s 400 demo.ifix; ifix verify ... -> REGION_TILING, exit 1
$ printf X | dd ... seek=0;       ifix verify ... -> BAD_MAGIC, exit 1
$ mutate data_bytes;              ifix verify ... -> REGION_TILING, exit 1
```

## Known limitations / non-goals

- No frontend, by request; control is JSON only (CLI flags or stdin).
- Inline blob responses are base64 and capped by `max_inline_base64` /
  `max_output_bytes`; very large blobs are expected to be served via the
  streaming library API (`read_blob` into a writer) rather than inline JSON.
- `mmap` is implemented for Unix only (hand-rolled FFI); `read`/memory
  backends are portable.
