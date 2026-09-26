# IFIX — Immutable File Index

A dependency-free Rust library and CLI for a **streaming binary codec of a
read-only file tree**, backed by a **multi-level offset index** that resolves
a single path without scanning the file.

Everything — JSON parsing, Base64, and the `mmap(2)` wrapper — is implemented
in this crate. There are **no third-party dependencies**; the build works
offline with only `std`.

## Why the format is safe to open

Nothing in the decoder trusts an on-disk offset, count, or interval:

- the four sections are **tightly packed in a fixed order**, so every
  declared boundary is cross-checked against its neighbor and against the
  real file size (checked 64-bit arithmetic, no overflow);
- counts are **derived** from section length and record size, never read as a
  standalone "allocate this many" field, so a 200-byte file cannot claim
  millions of records — opening never allocates a declared size;
- every node and index block is fully validated **at the moment it is read**
  (type, reserved bytes, interval bounds, reference domains, ordering);
- block edges always point to **strictly higher block ids**, making index
  cycles impossible to encode; traversal is additionally hop-capped;
- decoded output is bounded (`max_output`, `max_listing`, `max_name_len`,
  `max_depth`, `max_link_depth`) and file payloads stream through a fixed
  16 KiB buffer instead of buffering whole blobs.

See [`docs/FORMAT.md`](docs/FORMAT.md) for the normative specification.

## The multi-level offset index

Every directory owns an independent B+-style tree of fixed **260-byte
blocks**, each holding up to **16** ordered entries (fanout 16):

- leaf entry: key = child name, value = node id;
- internal entry: key = minimum name in that subtree, value = block id.

Looking up `w` in a directory binary-searches one block per level and
descends, touching `ceil(log_16 N)` blocks and one node record — sibling
subtrees and the rest of the node table are never read. A directory with
100,000 children needs about 5 block reads regardless of file size.

Blocks are numbered **preorder per directory tree**, so every parent→child
edge increases the block index; that single ordering rule is the acyclicity
guarantee.

## Building

```bash
cargo build --release
cargo test
```

No network access is required (zero dependencies). The produced binary is
`target/release/ifix`.

## JSON control protocol (primary interface)

One request object in, one response object out:

```bash
ifix run --request examples/build.json
ifix run --request examples/validate.json
ifix run --request examples/lookup.json
ifix run --request examples/list.json
ifix run --request examples/read.json
ifix run --request examples/crosscheck.json
```

Successful responses: `{"ok": true, "result": {...}}`.
Failures: `{"ok": false, "error": {"kind": "...", "message": "..."}}`.

### Operations

| op | required fields | notes |
|----|-----------------|-------|
| `build` | `tree`, `output` | file payloads inline via `content_base64`, or referenced from the host via `content_file` |
| `validate` | `file` | whole-file structural validation; `backend`: `file` (default) or `mmap` |
| `lookup` | `file`, `path` | `follow` (default true) toggles final-symlink following; returns node metadata + symlink target |
| `list` | `file`, `path` | immediate children in ascending key order, capped by `max_listing` |
| `read` | `file`, `path` | `offset`, `len`, `output_file`; inline base64 only under `max_inline_base64` |
| `crosscheck` | `file` | runs every lookup/listing/read through **both** the streaming and mmap readers and asserts byte-identical results |

An optional `limits` object may tighten any default cap (see FORMAT.md §7).

Equivalent convenience subcommands exist:

```bash
ifix build  --request examples/build.json --output /tmp/demo.ifix
ifix validate --file /tmp/demo.ifix
ifix lookup   --file /tmp/demo.ifix --path /etc/hostname --backend mmap
ifix list     --file /tmp/demo.ifix --path /
ifix read     --file /tmp/demo.ifix --path /etc/hostname --offset 0 --len 5
ifix crosscheck --file /tmp/demo.ifix
```

### Tree document

```json
{
  "name": "",
  "type": "dir",
  "children": [
    {"name": "a.txt", "type": "file", "mode": 420, "mtime": 0, "content_base64": "aGk="},
    {"name": "link",  "type": "symlink", "target": "a.txt"},
    {"name": "d", "type": "dir", "children": []}
  ]
}
```

- Root must be a directory with an empty `name`.
- Names are nonempty UTF-8, never containing `/`, NUL, `.`, or `..`.
- `type` is `file`, `dir` (alias `directory`), or `symlink`.
- File payload is strict padded Base64; symlink `target` is a nonempty string.

## Library API

```rust
use ifix::{Limits, Reader};

let reader = Reader::open_file("archive.ifix", Limits::default())?; // streaming
let mmap   = Reader::open_mmap("archive.ifix", Limits::default())?; // reference

let node = reader.lookup("/etc/hostname", true)?;
let meta = reader.list("/")?;
let bytes = reader.read_file(&node)?;

let report = ifix::validate::validate(&reader)?;
```

Build images from a JSON value with `ifix::writer::build_from_json`, or
consume the typed modules directly: `format`, `reader`, `writer`,
`validate`, `json`, `base64`, `mmap`.

## Two backends, one engine

`FileSource` performs one `seek + bounded read` per fetched record and never
maps the whole file; `MmapSource` is a private read-only mapping built
through raw `mmap(2)`/`munmap(2)` FFI. Both implement the same `Source`
trait and run the **identical** decode engine, so their answers are
byte-identical — asserted per-path by `crosscheck` and by the test suite.

## Security model for untrusted input

| Attack | Defense |
|--------|---------|
| mutated offsets / lengths | checked arithmetic + section-bound + neighbor-boundary checks at open, per-record at access |
| allocation bomb via declared counts | counts derived from real section length; no allocation before range validation |
| index cycle (`block → itself/back`) | child block id must be strictly greater; hop cap; validator graph check |
| node overlap / sharing | whole-file validator proves one parent per node, disjoint name/blob intervals, no shared/orphan blocks |
| separator/pointer desync | internal key must equal the referenced subtree's true minimum key |
| truncation | file length vs every section end; exact-EOF rule; short-read errors on every fetch |
| symlink loops | per-resolution link-seen set + `max_link_depth`; `lstat` mode via `follow:false` |
| output exhaustion | `max_output`, streaming fixed-size buffer, `offset`/`len` bounded reads |
| bad names / types / padding | strict field validation; unknown bytes rejected |

## Repository layout

```
src/
  format.rs    superblock, constants, header validation, Limits
  reader.rs    Source trait, File/Mmap/Slice sources, lookup/list/read
  writer.rs    JSON tree -> validated binary image, postorder index build
  validate.rs  whole-file integrity proof
  json.rs      hand-written JSON parser/serializer
  base64.rs    strict RFC 4648 Base64
  mmap.rs      raw mmap/munmap FFI wrapper
  error.rs     typed errors
  bin/ifix.rs  JSON control entry point / CLI
docs/FORMAT.md normative on-disk specification
examples/      sample JSON requests
tests/         mutation, cycle, and stream/mmap equivalence tests
```

## Non-goals

Mutation/append, compression, encryption, and concurrency control are out of
scope for version 1.
