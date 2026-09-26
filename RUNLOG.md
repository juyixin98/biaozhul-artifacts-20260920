# Run log

Commands actually executed during development/acceptance and their observed
results. The toolchain used was a standalone Rust 1.98.1 extracted under
`/home/admin/rust-local` (the host `rustup` installation was repeatedly
corrupted by an interrupted download; see "Environment" below). All commands
were run from the repository root.

## Environment

```text
$ rustc --version   (via /home/admin/rust-local/bin)
rustc 1.98.1 (48a229cea 2026-09-01)
$ cargo --version
cargo 1.98.1 (797e8a9bc 2026-08-05)
$ uname -srm
Linux 6.8.0-90-generic x86_64
```

Zero external crates; `cargo build --offline` works with no registry access.

## Build

```text
$ cargo build --release --offline
Finished `release` profile [optimized] target(s) in 8.86 s
$ cargo fmt
$ cargo clippy --offline --all-targets
(no warnings, no errors)
```

## Tests

```text
$ cargo test --offline
src/lib unit tests ............ 13 passed, 0 failed
tests/crosscheck.rs ...........  3 passed, 0 failed
tests/cycles.rs ...............  6 passed, 0 failed
tests/mutation.rs ............. 10 passed, 0 failed
                          total 32 passed, 0 failed
```

Coverage by acceptance item:

- mutated offsets/lengths — `tests/mutation.rs::mutate_header_offsets`,
  `mutate_node_offsets_and_lengths`, `allocation_bomb_declared_giant_counts`;
- index cycles — `mutate_block_child_to_self_creates_cycle` (self and
  backward edges), plus symlink cycles in `tests/cycles.rs`;
- node overlap — `node_interval_overlap` (names),
  `cycles.rs::blob_interval_overlap_is_rejected` (blobs);
- truncation — `truncation_variants`,
  `readers_reject_truncated_file_at_all_cut_points`;
- legal files agree with the mmap reference — every test in
  `tests/crosscheck.rs`, and the `crosscheck` CLI op below.

## End-to-end CLI run (examples/)

Build, validate (stream), lookup (mmap), list, ranged read, and stream/mmap
crosscheck against `examples/build.json` all returned `"ok": true`:

```text
build     -> bytes 1110, nodes 6, blocks 2, dirs 2, file_bytes 35
validate  -> nodes 6, blocks 2, dirs 2, file_bytes 35 (backend file)
lookup    -> /etc/hostname node_id 3, type file, size 13 (backend mmap)
list /    -> 3 entries in ascending key order
read      -> /etc/hostname offset 0 len 5 => base64 "ZXhhbXA="
crosscheck-> paths 5, payload_bytes 35, equal true
```

Symlink behavior on `/etc/motd -> /README.md`:

- following: resolves to node 1 `README.md` (file);
- `--no-follow`: returns node 4 `motd` (symlink) with literal target.

## Large tree (selective-access demonstration)

Built a 50,000-file tree plus a nested directory/symlink:

```text
build      -> bytes 9,392,958 (9.0 MiB), nodes 50,003, blocks 3,337
             build wall time ~0.84 s
crosscheck -> paths 50,002, payload_bytes 4,975,000, equal true
             (streaming reader vs mmap, every path and payload)
             wall time ~11.2 s (debug binary doing 50k paired lookups)
lookup /f049999 (streaming) -> wall time 0.002 s
```

I/O actually performed for that single targeted lookup (strace):

```text
file size:         9,392,958 bytes
read syscalls:           66   (includes process/loader startup)
seeks:                   61
total bytes read:     6,123
fraction of file:     0.065%
```

The lookup descends a 4-level internal index plus one leaf block and reads
one node record; it never touches sibling subtrees.

## Memory safety on a hostile file

A 256-byte file with its NODES-end field overwritten to `u64::MAX`:

```text
$ ifix validate --file /tmp/bomb.ifix
{"ok": false, "error": {"kind": "truncated",
 "message": "section nodes declares end 18446744073709551615 but file is 256 bytes"}}
Maximum resident set size (kbytes): 2304
```

The declared size never drives an allocation.

## Corruption rejections observed (whole-file validation)

| Mutation | Result |
|----------|--------|
| magic byte 0 -> `X` | `corrupt: bad magic: not an IFIX file` |
| last byte removed | `truncated: section blobs declares end ... but file is ...` |
| root internal block first child -> self block id | `corrupt: block 0 references child block 0 which is not greater (index cycle)` |
| same -> root-1 | same non-forward-reference rejection |
| root dir name_len -> 4000 | `corrupt: name interval [0,+4000) exceeds section bound 33` |
| file node data_len -> u64::MAX | `corrupt: blob interval [0,+18446744073709551615) exceeds section bound 45` |
| leaf entry key repointed elsewhere | `corrupt: leaf entry key does not match child node name interval` |
| root child_count -> 9,000,000 | `corrupt: directory leaf count != child_count` |
| symlink a->b->a; self->self; dir/up->../dir/up | file validates; lookup returns `link_cycle` |
| list with `limits.max_listing=100` on 50k dir | `limit_exceeded: listing > 100` |
| validate with `limits.max_nodes=1000` on 50k file | `limit_exceeded: node_count > 1000` |
| `read` on a directory | `corrupt: node 2: invalid record: read target is not a regular file` |
| malformed JSON request | `bad_request: request is not valid JSON: ...` |
| unknown op | `bad_request: invalid JSON request: unknown op "frobnicate"` |
| missing path | `not_found: path not found: nope` |

All CLI failures use exit code 1 and emit the JSON error envelope on stdout
plus a single human-readable line on stderr.

## Notes / limitations

- `mmap` support is implemented for Unix via raw FFI (`mmap`/`munmap`);
  non-Unix targets would need an alternative `Source`.
- The streaming reader holds one `File` behind a `Mutex`; each fetch is one
  seek plus one bounded read with a fixed 16 KiB payload buffer.
- The host's preinstalled `rustup` was unusable (partial/corrupt component
  downloads, repeatedly failing the post-download rename); rather than spend
  time on the installer, the canonical tarballs were unpacked to
  `/home/admin/rust-local` and used directly. This is an environment issue,
  not a project issue — the project itself has no dependencies.
