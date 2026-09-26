# `AC01` coded format specification

This document defines the exact bit-level format produced and consumed by this
implementation. The format is **frozen**: all numeric parameters are fixed
constants, so output is deterministic across runs, builds and platforms
(big-endian integers are emitted explicitly).

There are two layers:

1. **Payload** — the adaptive integer arithmetic code stream (bits).
2. **Container** — a small self-describing envelope around the payload, with
   length metadata and a CRC.

---

## 1. Container layout

All multi-byte integers are **unsigned big-endian**.

```
offset      size  field
0           4     begin magic   ASCII "AC01"            (0x41 43 30 31)
4           1     version       0x01
5           N     payload       ceil(coded_bits/8) bytes
5+N         8     coded_bits    exact number of payload bits (u64)
13+N        8     original_length  source byte count (u64)
21+N        4     crc32         CRC-32/IEEE (u32)
25+N        4     end magic     ASCII "ACED"            (0x41 43 45 44)
```

The fixed trailer (everything after the payload) is exactly **24 bytes**:

| field             | bytes |
|-------------------|-------|
| `coded_bits`      | 8     |
| `original_length` | 8     |
| `crc32`           | 4     |
| end magic `ACED`  | 4     |

The fixed prefix is **5 bytes** (`AC01` + version).

### CRC-32

`crc32` is the standard IEEE 802.3 polynomial, reflected
(`0xEDB88320`), init `0xFFFFFFFF`, final XOR `0xFFFFFFFF`. It is computed over
**every preceding byte**, i.e. the begin magic, version, payload,
`coded_bits` and `original_length`. It does **not** cover itself or the end
magic. Check value: CRC-32 of the ASCII bytes `123456789` is `0xCBF43926`.

### Payload packing

Payload bits are packed **MSB-first**: the first coded bit is the most
significant bit of payload byte 0. The final payload byte is padded on the
right (low bits) with **zero** bits up to a byte boundary; the number of
padding bits is `(8 - coded_bits mod 8) mod 8`. A conforming decoder rejects a
non-zero padding bit.

### Validation performed on decode

A conforming decoder must reject the stream (with an error, never partial
silent output) when *any* of these fail:

1. Fewer than 29 bytes (5 prefix + 24 trailer), or begin magic / version
   mismatch, or missing end magic → truncation/foreign data.
2. `crc32` mismatch → truncation or corruption.
3. `coded_bits == 0`, or `ceil(coded_bits/8)` differs from the payload byte
   count.
4. Non-zero padding bits in the final payload byte.
5. `original_length` exceeds the caller-supplied output cap.
6. The decoded payload does not end in the EOF symbol using only real coded
   bits (the decoder never needs bits past the payload — see §4).
7. The number of decoded source bytes differs from `original_length`.

### Why the metadata is a trailer, not a header

Encoding is fully streaming: the exact coded bit count and (trivially) the
source length are only known after coding finishes. Placing the metadata in a
fixed 24-byte trailer lets the encoder write a constant-memory stream without
buffering the payload, while truncation is still detected deterministically
(via end magic + CRC + length fields).

---

## 2. Alphabet and adaptive model

The coder is an order-0 adaptive model over a **257-symbol alphabet**:

* symbols `0..=255` — literal source bytes,
* symbol `256` — **EOF** terminator (`EOF_SYMBOL`), emitted exactly once after
  all source bytes.

Symbol counts start uniformly: every symbol has count **1**
(`INITIAL_FREQUENCY`), so the initial total is `257`.

After a symbol is coded (by either side) its count is incremented by one.

### Rescaling (frequency renormalization)

Before incrementing, if the total count is at least `MAX_FREQUENCY = 16384`,
**every** symbol count is replaced by

```
count := max(1, floor(count / 2))
```

(`RESCALE_FLOOR = 1`), the total is recomputed, and the lookup structure is
rebuilt. Halving preserves the learned relative distribution while making
room for new evidence; the floor guarantees no symbol ever receives zero
probability. This rule is identical and applied in the same order by encoder
and decoder, so their models stay synchronized symbol-for-symbol.

Cumulative-frequency queries use a Fenwick (binary-indexed) tree; symbol
lookup by scaled cumulative value uses the standard O(log N) Fenwick
binary-search descent.

---

## 3. Integer interval arithmetic coder

State is a half-open integer interval `[low, high]` with

```
CODE_BITS = 32
TOP       = 2^32
HALF      = 2^31
QUARTER   = 2^30
```

initialized to `low = 0`, `high = TOP − 1`.

### Encoding one symbol

For a symbol with cumulative frequency `cum` (sum of counts below it),
frequency `freq`, and model total `total`, with current range
`range = high − low + 1`:

```
high = low + range * (cum + freq) / total − 1
low  = low + range * cum / total
```

All arithmetic is exact integer arithmetic; intermediate products
(`range * total`) fit in 64 bits because `range < 2^32` and
`total <= 2^32 * 16384 = 2^46`.

The encoder then renormalizes with the three classic cases, repeated until
none applies:

* **E1** — `high < HALF`: output bit `0`, then `pending` copies of `1`.
* **E2** — `low >= HALF`: output bit `1`, then `pending` copies of `0`;
  subtract `HALF` from `low` and `high`.
* **E3** — `low >= QUARTER` and `high < 3*QUARTER`: increment `pending`;
  subtract `QUARTER` from `low` and `high`.

After any case, double the interval: `low <<= 1`,
`high = (high << 1) | 1` (masked to 32 bits).

`pending` is the deferred-carry counter: when E3 holds, the eventual output
bit is uncertain until the interval commits to the lower or upper half; the
run of opposite bits emitted with the deciding bit realizes the carry without
back-patching already-emitted bytes.

### Decoding one symbol

The decoder keeps a 32-bit lookahead `value`, primed by reading the first 32
payload bits (missing bits are treated as zero, although a valid stream never
requires that — see below). The scaled cumulative target is

```
scaled = ((value − low + 1) * total − 1) / range
```

and the symbol is the unique one whose cumulative interval contains
`scaled`; the interval is narrowed with the same `cum`/`freq`/`total`
formula. The decoder then performs the **same** E1/E2/E3 renormalization,
keeping `value` in sync (`value -= HALF`, or `value -= QUARTER`), and shifts in
one new payload bit after every case:

```
value = ((value << 1) & (TOP − 1)) | next_bit
```

### Terminator and final flush

After all source symbols the encoder codes the EOF symbol (256), then performs
the standard Witten–Neal–Cleary flush:

1. `pending += 1`;
2. if `low < QUARTER`, output `0` followed by `pending` ones;
   otherwise output `1` followed by `pending` zeros;
3. output a further `CODE_BITS` (32) **zero** tail bits.

The decoder stops as soon as it decodes symbol 256. Because the encoder
appended the deciding flush bit plus 32 zero tail bits, a complete stream
always supplies every real bit the decoder consumes up to and including EOF;
the decoder therefore never reads into an imaginary all-zero tail for a valid
stream. If it does, the coded bytes were truncated and decoding aborts with
`UnexpectedEnd`.

### Empty input

An empty source codes exactly one symbol: EOF. Its container is 29 bytes and
its payload is 42 bits (6 payload bytes) for the initial uniform model.

---

## 4. Determinism and limits

* Coding is deterministic: identical input bytes always produce an identical
  container, regardless of platform.
* The model uses fixed memory (a 257-entry table); streaming I/O uses a fixed
  64 KiB read buffer. Memory use does not grow with input size.
* Decoded output length is always bounded by a caller-supplied cap
  (default `2^30` bytes); coded payload growth during encoding may likewise be
  capped.
* There are no padding/alignment markers between symbols; the EOF symbol is
  the sole end-of-data marker.

## 5. Notation summary

| Constant            | Value     | Meaning                                   |
|---------------------|-----------|-------------------------------------------|
| `CODE_BITS`         | 32        | interval width in bits                    |
| `TOP`               | 2^32      | one past the maximum interval value       |
| `HALF`              | 2^31      | E1/E2 boundary                            |
| `QUARTER`           | 2^30      | E3 boundary / final-flush threshold       |
| `MAX_FREQUENCY`     | 16384     | rescale trigger on total count            |
| `INITIAL_FREQUENCY` | 1         | per-symbol starting count                 |
| `RESCALE_FLOOR`     | 1         | minimum count after halving               |
| `EOF_SYMBOL`        | 256       | terminator symbol                         |
| alphabet size       | 257       | 256 bytes + EOF                           |
