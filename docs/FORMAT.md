# IFIX1 — Immutable File Index, version 1

This document is the **normative specification** of the IFIX1 binary format.
All multi-byte integers are stored **little-endian**. All offsets are byte
offsets from the beginning of the file. The file layout is:

```
+-----------------------------------------+ offset 0
| Header (48 bytes)                       |
+-----------------------------------------+ header.nodes_offset
| Data region (blobs, e.g. file content)  |
+-----------------------------------------+ header.index_offset
| Node block                              |
+-----------------------------------------+ header.toc_offset
| Table of contents (TOC), length N*13    |
+-----------------------------------------+ EOF
```

Every region end equals the next region's start; the TOC ends exactly at
`file_len`. Decoders MUST reject any file whose declared intervals do not tile
the file this way.

## 1. Header (48 bytes)

| Offset | Size | Field            | Meaning |
|-------:|-----:|------------------|---------|
| 0      | 6    | `magic`          | ASCII `IFIX1` + NUL (`49 46 49 58 31 00`) |
| 6      | 1    | `flags`          | Bit 0: data blobs present. Other bits MUST be zero. |
| 7      | 1    | `log2_page`      | Leaf fanout exponent for the TOC B-tree (5..=16). |
| 8      | 2    | `version`        | `1` (u16 LE). |
| 10     | 2    | `reserved0`      | MUST be zero. |
| 12     | 4    | `node_count`     | Number of nodes (u32). Root is node `0`. |
| 16     | 8    | `data_bytes`     | Length of the data region (u64). |
| 24     | 8    | `nodes_offset`   | Start of the node block (u64). |
| 32     | 8    | `toc_offset`     | Start of the TOC (u64). |
| 40     | 4    | `file_crc32`     | CRC-32 (IEEE) of bytes 48..file_len, i.e. data region + node block + TOC (u32 LE). |
| 44     | 4    | `reserved1`      | MUST be zero. |

The data region starts at `48` immediately after the header. Structural
validation (region tiling, every offset/range) happens before the CRC is
trusted; a CRC mismatch is reported as `BAD_CRC`.

## 2. Data region

From offset 48 for `data_bytes`. Blobs are referenced by nodes as
`(data_offset, data_len)` pairs that MUST point inside this region.

## 3. Node block

Contains `node_count` fixed-size **node records of 48 bytes each**, beginning
at `nodes_offset`. A record describes one tree node (a directory or a file):

| Offset | Size | Field          | Meaning |
|-------:|-----:|----------------|---------|
| 0      | 8    | `data_offset`  | Absolute file offset of this node's blob (files only). |
| 8      | 8    | `data_len`     | Blob length in bytes. |
| 16     | 4    | `blob_hash`    | CRC-32 (IEEE) of the blob bytes (u32); `0` for empty blobs. |
| 20     | 4    | `reserved0`    | MUST be zero. |
| 24     | 4    | `first_child`  | Node id of the first child; `0xFFFFFFFF` if none. |
| 28     | 2    | `child_count`  | Number of children (u16). Children are contiguous ids. |
| 30     | 1    | `node_type`    | `1` = file, `2` = directory. |
| 31     | 1    | `name_len`     | Name length (0..=255). |
| 32     | 2    | `flags`        | MUST be zero in v1. |
| 34     | 2    | `reserved`     | MUST be zero. |
| 36     | 4    | `self_check`   | CRC-32 (IEEE) of this record's bytes 0..36. |
| 40     | 8    | `name_offset`  | Absolute file offset of the UTF-8 name bytes. |
| 48     |      |                | (end of record) |

Rules enforced by the decoder:

* `node_count * 48 + nodes_offset == toc_offset` (node block exactly tiles).
* Every node id referenced (`first_child .. first_child + child_count`,
  except the sentinel `0xFFFFFFFF`) is `< node_count`. `child_count == 0`
  requires the sentinel; non-zero requires a real id.
* Every node except root (id 0) has **exactly one** parent; the root has
  none. Children ids of one node MUST be distinct and non-overlapping with
  any other node's child id interval. This forbids sharing/overlaps/cycles
  structurally; a depth guard additionally rejects pathological chains.
  Root reaches all nodes (no orphans).
* Directory nodes MUST have `data_len == 0` and `data_offset == 0`.
* File nodes with `data_len > 0` MUST satisfy
  `48 <= data_offset` and `data_offset + data_len <= 48 + data_bytes`,
  with checked arithmetic. Empty blobs use `(0, 0)`.
* `name_offset + name_len` lies within the data region (names are blobs),
  and names MUST be valid UTF-8. Sibling names are unique.
* `self_check` MUST equal CRC-32 of the record's first 36 bytes.

## 4. Table of contents (multi-layer offset index)

The TOC begins at `toc_offset` and ends at EOF. It is a read-optimized
B-tree keyed by node id, built bottom-up over the records:

* **Leaves** hold `(node_id:u32, record_offset:u64)` pairs (12 bytes),
  sorted ascending by id, covering `[0, node_count)` exactly once.
* **Internal levels** hold `(first_id:u32, child_entry:u64)` pairs
  (12 bytes); `child_entry` is the *file offset* of the child-level entry
  whose subtree begins there. Each entry covers a contiguous id interval;
  the last key of a subtree is `next.first_id - 1`.
* Every level contains exactly `ceil(n / fanout)` entries except possibly
  the root which has `1..=fanout`; root level has a single entry whose
  `first_id == 0` and `child_entry` points to the level below (or, when
  the tree has one level, directly at the leaf block).
* Fanout `F = 2^log2_page` (header), leaf and internal pages hold at most
  `F` entries; `log2_page ∈ [5, 16]`.

Layout inside the TOC region, stored **bottom-up** (leaves first):

```
leaf level:   level_len[0] bytes (= leaf_entries * 12)
internal L1:  level_len[1] bytes
...
root level:   level_len[L-1] bytes (= 12, single entry)
level table:  L * u64 LE, length in bytes of each level, leaves first
level count:  u32 LE (L, >= 1)
fanout byte:  u8 (== header.log2_page)
tail magic:   4 bytes b"TEND"
```

A lookup for node id `k` reads: tail (17 bytes) → level table → root entry
→ at most `L-1` internal pages → one leaf page → the record offset, then
the 48-byte node record. Bytes touched are
`O(depth)` pages plus the record, independent of `node_count`.

The decoder validates the TOC structurally before trusting it for
navigation: level sizes are consistent with `node_count` and fanout; every
`child_entry` points at a 12-byte-aligned entry inside the expected child
level's byte range; keys are sorted, start at 0, contiguous and non-empty.
A corrupt pointer (out of range, misaligned, pointing at the wrong level,
cyclic) is rejected with `TOC_POINTER` / `TOC_STRUCTURE` — no allocation
keyed on TOC-declared sizes occurs before these checks.

## 5. CRC-32

IEEE polynomial `0xEDB88320` reflected, init `0xFFFFFFFF`, final XOR
`0xFFFFFFFF` (the variant used by zlib/PNG/gzip), implemented in this
crate with a hand-rolled table.

## 6. Validation order (decoder)

1. Read header only; check magic/version/flags/reserved/log2_page and the
   48-byte size against actual file length. No heap allocation yet.
2. Check region tiling with checked arithmetic:
   `48 <= nodes_offset`, `48 + data_bytes == nodes_offset`,
   `nodes_offset + node_count*48 == toc_offset`, `toc_offset <= file_len`,
   tail fits. Apply configured `Limits`.
3. Parse TOC tail + level table; fully validate TOC structure.
4. Validate every node record (ranges, self CRC, types) — streamed in
   pages; verify parent/child graph (exactly-once reachability, no overlap,
   no cycle, sibling-name uniqueness, UTF-8) in a single bounded pass.
5. Verify `file_crc32` over bytes 48..file_len last.
6. Only a fully validated file is ever served from.

## 7. Safety properties

* Declared counts/offsets never drive allocation: the decoder maps records
  via fixed-size arithmetic against the measured file length and caps every
  allocation with `Limits` (see `src/format.rs`).
* Lookup depth is bounded by the validated TOC height and `max_depth`.
* Decoded output is bounded by `max_output_bytes`; streamed JSON build
  input is bounded by `max_input_bytes`.
* The format is immutable: writers produce one complete file; there is no
  in-place mutation operation.
