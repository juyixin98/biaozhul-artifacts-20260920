# RBST v1 — streaming bitmap-set binary format

`RBST` is a compact, deterministic, stream-friendly serialisation of a set of
**32-bit unsigned integers**, stored as a hybrid of sorted sparse arrays and
fixed bitmaps (a Roaring-style layout, reimplemented from scratch).

All integers are **little-endian**. The layout is self-describing: a decoder
never needs out-of-band length information, and every declared length is
validated against both the remaining bytes and caller-supplied budgets.

## 1. File / stream layout

```
┌────────────────────────── Header (18 bytes) ───────────────────────────┐
│ magic[4]  = 0x52 0x42 0x53 0x54        ASCII "RBST"                    │
│ version   u8 = 1                                                      │
│ flags     u8 = 0 (must be zero; any set bit is rejected by v1)        │
│ chunks    u32 LE   number of container chunks that follow (<= 65536)  │
│ total     u64 LE   total number of distinct values in the whole set   │
└───────────────────────────────────────────────────────────────────────┘
then exactly `chunks` chunk records, in ascending key order:

┌────────────────────── Chunk record ──────────────────────┐
│ key   u16 LE   high 16 bits of every value in the chunk  │
│ kind  u8        0 = array container,  1 = bitmap container│
│ payload  (depends on kind, below)                         │
└───────────────────────────────────────────────────────────┘
```

### 1.1 Array container (`kind = 0`)

```
cardinality  u16 LE   number of values, 0 <= cardinality <= 4096
values       cardinality × u16 LE, strictly ascending, no duplicates
```

An array container stores at most **4096** values. The encoder chooses an
array whenever the chunk holds ≤ 4096 values (≤ 8192 bytes of payload).

### 1.2 Bitmap container (`kind = 1`)

```
cardinality  u32 LE   number of set bits, 0 <= cardinality <= 65536
words        1024 × u64 LE, exactly 8192 bytes, word 0 first
```

Bit `b` of word `w` (word `w`, mask `1 << (b & 63)`, value `64*w + (b&63)`)
being set means the chunk contains value `(key << 16) | (64*w + (b&63))`.

A bitmap container is chosen whenever a chunk holds **more than 4096**
values. The encoder **verifies** that the payload's actual popcount equals
the declared cardinality; the decoder does the same and rejects a mismatch.

`cardinality` is `u32` (not `u16`) because a full chunk contains 65 536
values, which does not fit in 16 bits.

## 2. Value decomposition

Every `u32` value `v` is split into:

* `key = v >> 16` (high 16 bits) — selects the chunk;
* `low = v & 0xFFFF` (low 16 bits) — the slot inside the container.

There are therefore at most 65 536 chunks, each covering 65 536 values; the
format can represent the entire `u32` universe (2^32 values, which needs the
u64 `total` header field). Empty chunks are never stored.

## 3. Canonical representation and container switching

The in-memory (and decoded) representation is canonical:

* chunk cardinality ≤ 4096  ⇒ array container;
* chunk cardinality > 4096  ⇒ bitmap container.

Switching representation never changes membership. A decoder receiving a
sparse bitmap (e.g. a hand-crafted stream with ≤ 4096 set bits) canonicalises
it to an array, so two sets with the same values always compare equal
regardless of which container kind appeared on the wire.

## 4. Bounds, limits and adversarial inputs

A decoder is constructed with two explicit budgets:

| budget       | meaning                                              |
|--------------|------------------------------------------------------|
| `max_bytes`  | total bytes the decoder is allowed to read           |
| `max_values` | total distinct values the decoder is allowed to emit |

The decoder additionally enforces the format's own structural caps:

* `chunks <= 65536`;
* array `cardinality <= 4096` (an over-large array length is rejected
  **before** its values are read, so a forged length cannot trigger a large
  allocation or an over-read);
* bitmap `cardinality <= 65536` and exactly 8192 payload bytes;
* chunk keys strictly ascending and unique;
* array values strictly ascending and unique;
* sum of container cardinalities equals header `total`;
* bitmap payload popcount equals its declared cardinality.

A truncated stream yields `UnexpectedEof` carrying the byte count the stream
would have needed; exceeding a budget yields `ByteLimitExceeded` or
`ValueLimitExceeded`. Reading is streamed one chunk at a time through any
`std::io::Read`, so peak memory is bounded by one bitmap (8 KiB) plus the
in-memory result set.

## 5. Size examples

| Set                                   | Bytes |
|---------------------------------------|-------|
| empty set (`chunks = 0, total = 0`)   | 18    |
| 3 values in one sparse chunk          | 18 + 2 + 3 + 6 = 29 |
| 4097 values in one chunk (bitmap)     | 18 + 2 + 5 + 8192 = 8217 |
| one full chunk (65536 values)         | 18 + 2 + 5 + 8192 = 8217 |

## 6. Example bytes

Encoding `{1, 2}` (one array chunk, key 0):

```
52 42 53 54  01 00  01 00 00 00  02 00 00 00 00 00 00 00
| "RBST"  | v1 fl|   chunks = 1  |        total = 2
00 00  00  02 00  01 00  02 00
key 0 |array| card 2 | low=1 | low=2
```

## 7. Non-goals

* No run-length or run-container type (only array and bitmap).
* No compression layer; compression belongs below this format if needed.
* No little-/big-endian negotiation — little-endian only.
