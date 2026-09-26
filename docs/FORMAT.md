# IFIX — Immutable File Index Format, version 1

IFIX is a read-only binary container for an immutable tree of file,
directory, and symbolic-link nodes. Its defining feature is a **multi-level
offset index** that resolves a single path by reading only
`O(depth · log_FANOUT children)` fixed-size blocks — never by scanning the
node table or a directory's siblings.

This document is the normative specification. Anything an implementation
accepts must be justified by a rule here; anything not permitted is rejected.

- All integers are **little-endian**.
- All offsets are **absolute byte positions** in the file (header fields) or
  **relative to the start of their section** (record fields), as noted.
- All interval arithmetic uses **checked 64-bit addition**. A value that
  overflows, runs past its section, or runs past end-of-file is invalid.
- The four sections are **tightly packed in a fixed order with no gaps and no
  overlap**. Every declared boundary is therefore cross-checked against its
  neighbor, never trusted on its own.
- A reader **does not allocate according to a declared count or length**
  before that value has been range-checked against the real file size.

## 1. Overall layout

```
offset 0           superblock (128 bytes)
offset 128         NODES  section : node_count * 64 bytes
                   NAMES  section : packed UTF-8 name bytes
                   BLOCKS section : block_count * 260 bytes
                   BLOBS  section : packed file contents / symlink targets
EOF = blobs_end
```

There are exactly four sections in the order NODES, NAMES, BLOCKS, BLOBS.
The next section starts at the exact byte the previous one ends
(`nodes_end == names_start`, and so on), and the file ends exactly at
`blobs_end`. A trailing byte, a gap, or an overlap makes the file invalid.

## 2. Superblock (128 bytes)

| Offset | Size | Field        | Rules |
|-------:|-----:|--------------|-------|
| 0      | 8    | magic        | exactly `49 46 49 58 0a 00 01 0a` (`IFIX\n\x00\x01\n`) |
| 8      | 1    | version      | exactly `1` |
| 9      | 7    | reserved     | all zero |
| 16     | 8    | nodes_start  | exactly 128 |
| 24     | 8    | nodes_end    | multiple of 64; `>= nodes_start` |
| 32     | 8    | names_start  | exactly `nodes_end` |
| 40     | 8    | names_end    | `>= names_start` |
| 48     | 8    | blocks_start | exactly `names_end` |
| 56     | 8    | blocks_end   | exactly `blocks_start + block_count*260`; multiple of 260 |
| 64     | 8    | blobs_start  | exactly `blocks_end` |
| 72     | 8    | blobs_end    | exactly the real file length; `>= blobs_start` |
| 80     | 4    | root_node    | `< node_count`; that node is a directory with an empty name |
| 84     | 4    | root_block   | either `0xFFFFFFFF` (empty root only) or `< block_count`, and must equal the root node's `first_block` |
| 88     | 8    | flags        | zero |
| 96     | 32   | reserved     | all zero |

`node_count = (nodes_end - nodes_start) / 64`
`block_count = (blocks_end - blocks_start) / 260`

Both counts are derived, never declared, so a header cannot claim a million
records in a hundred-byte file: the section simply fails alignment or
end-of-file checks first.

## 3. NODES section — 64-byte records

| Offset | Size | Field        | Rules |
|-------:|-----:|--------------|-------|
| 0      | 1    | type         | `1`=file, `2`=dir, `3`=symlink; anything else invalid |
| 1      | 3    | reserved     | zero |
| 4      | 4    | name_off     | relative to NAMES |
| 8      | 4    | name_len     | bytes; `name_off + name_len <= names_len` |
| 12     | 4    | reserved     | zero |
| 16     | 8    | data_off     | relative to BLOBS (files/symlinks) |
| 24     | 8    | data_len     | `data_off + data_len <= blobs_len` |
| 32     | 4    | first_block  | dirs: block index or `0xFFFFFFFF` when empty; others: `0xFFFFFFFF` |
| 36     | 4    | child_count  | dirs: exact number of leaf entries under `first_block`; others: 0 |
| 40     | 8    | mode         | stored as given (POSIX bits) |
| 48     | 8    | mtime        | stored as given (unix seconds) |
| 56     | 8    | reserved     | zero |

Per-type rules:

- **dir**: `data_off = data_len = 0`. If `first_block = NIL` then
  `child_count = 0`; otherwise `child_count > 0`.
- **file / symlink**: `first_block = NIL`, `child_count = 0`, and the blob
  interval must be in range. A symlink target is a nonempty UTF-8 byte string
  in BLOBS.
- Every non-root node has a nonempty UTF-8 name containing neither `/` nor
  NUL (and never exactly `.` or `..`). The root node's name is empty.

Intervals of **distinct nodes' names never overlap** (the producer packs them
disjointly; the whole-file validator proves it). Non-empty blob intervals of
distinct payload nodes never overlap. Multiple zero-length payloads share
offset 0 by convention.

## 4. NAMES section

A packed byte pool. Names are never NUL-terminated on disk; their length
comes from the node record. A decoder materializes a name only when it needs
to compare or return it, and rejects names longer than its configured
`max_name_len` before reading them.

## 5. BLOCKS section — the multi-level offset index

Every directory owns an independent B+-style search tree of fixed-size
**260-byte blocks**. A block is:

| Offset | Size | Field |
|-------:|-----:|-------|
| 0      | 1    | kind: `1`=leaf, `2`=internal; anything else invalid |
| 1      | 3    | reserved, zero |
| 4      | 256  | 16 entries of 16 bytes each |

Each 16-byte entry:

| Offset | Size | Field |
|-------:|-----:|-------|
| 0      | 4    | key_off (relative to NAMES) |
| 4      | 4    | key_len (in range of NAMES, nonzero) |
| 8      | 8    | child |

- Active entries occupy slots `0..k` with no gaps; every slot from `k` on is
  inactive. An inactive entry is exactly eight `0xFF` bytes in its `child`
  field (`u64::MAX`); key fields of inactive slots are ignored.
- Active entry keys are **strictly ascending byte strings**.
- **Leaf block** (`kind=1`): `child` is a node id `< node_count` (the high
  32 bits are therefore zero). Its key interval **equals** that node's name
  interval.
- **Internal block** (`kind=2`): `child` is a block index `< block_count`,
  strictly **greater than the block that contains the entry**. Blocks are
  numbered in **preorder per directory tree**, so every edge points forward;
  this single rule makes a reference cycle impossible to encode. The entry
  key equals the **minimum key anywhere in the referenced child subtree**
  (the separator), and subtree key ranges are disjoint and ordered.

### 5.1 Routing

To find name `w` in one directory:

1. Start at the directory's `first_block`.
2. Binary-search the block's active keys for the lower bound of `w`.
3. In a leaf: an equal key is the child node; otherwise the name is absent.
4. In an internal block: choose the last entry whose key is `<= w` and
   descend to its block.
5. A valid tree is finite because every descent strictly increases the block
   id; a decoder additionally caps traversal hops at `block_count`.

Thus a lookup in a directory of `N` children touches `ceil(log_16 N)` blocks
plus one node record, regardless of total file size.

### 5.2 Whole-tree index invariants (validated)

For every directory, all blocks reachable from `first_block`:

- form a single rooted acyclic tree (no sharing between directories, no
  orphan block anywhere in the section),
- contain exactly `child_count` leaf entries, all distinct,
- list leaf keys strictly ascending and equal to child node names,
- carry internal separators equal to the referenced subtree's true minimum
  key, with strictly ordered non-overlapping subtree ranges.

The node graph itself is a single rooted tree: node 0 is the root directory,
every other node has exactly one parent directory, and no node is
unreachable or multiply reachable.

## 6. BLOBS section

A packed byte pool of regular-file contents and symlink targets. Reads are
bounded: a decoder refuses to return more than its configured `max_output`
bytes and performs payload I/O through a fixed-size streaming buffer, so
opening and reading never buffers an entire large file.

## 7. Resource limits (decoder policy)

The reference decoder takes a `Limits` value; defaults:

| limit | default | meaning |
|-------|--------:|---------|
| `max_file_len` | 1 GiB | reject larger files at open |
| `max_nodes` | 4,000,000 | cap on derived node count |
| `max_blocks` | 4,000,000 | cap on derived block count |
| `max_output` | 64 MiB | max bytes returned by one content read |
| `max_name_len` | 4096 | max materialized name |
| `max_listing` | 1,000,000 | max entries per listing |
| `max_depth` | 128 | path components |
| `max_link_depth` | 40 | symlink expansions before cycle error |

## 8. Validation order (what is checked and when)

1. **Open**: file length ≥ 128; magic, version, reserved bytes; every section
   range with checked arithmetic; tight packing/order/alignment; derived
   counts; root references; configured caps. No section content is read.
2. **On access**: each node/block fetched is fully field-validated at that
   moment (intervals, types, padding, reference domains, ordering,
   forward-block rule). A lookup cannot touch an invalid record without
   rejecting it.
3. **Full validation** (`ifix validate`): phase 1 plus every node and name,
   every block, interval disjointness, every index tree's structural
   invariants, single-parent/single-root node tree, and root-block
   consistency.

## 9. JSON control protocol

Single request object in, single response object out:

- build: `{"op":"build","tree":<node>,"output":"archive.ifix"}`; node payloads
  use `"content_base64"` (strict RFC 4648) or `"content_file"` (host path
  inlined at build time).
- validate / lookup / list / read / crosscheck: see `examples/`.

Responses are `{"ok":true,"result":{...}}` or
`{"ok":false,"error":{"kind":"...","message":"..."}}`.

## 10. Non-goals

Concurrency control, mutation/appending, compression, encryption, and any
on-disk pointer other than the bounded offset families above are explicitly
out of scope for version 1.
