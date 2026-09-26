# BSE1 wire format

**BSE1** ("Binary Schema Evolution, version 1") is a small, self-describing
tagged binary message format designed for forward/backward compatibility. It
is implemented from scratch in this crate (LEB128 varints, zig-zag, the JSON
control surface and base64 are all hand-written, no third-party crates).

This document is the normative specification of the byte layout.

## 1. Design goals

* **Field numbers, not positions.** Fields are identified by stable numbers;
  fields may be added, removed or reordered freely.
* **Unknown fields are preserved.** A reader that does not know a field
  captures its raw bytes and re-emits them unchanged, so data can pass through
  intermediate services without losing fields added by newer producers.
* **Presence is first class.** An absent optional field is never confused with
  an explicit zero, empty string, empty byte string or `false`.
* **Bounded decoding.** Per-value length, total output size and nesting depth
  are all capped, so hostile input cannot exhaust memory or the stack.
* **Streaming.** The decoder pulls from any `std::io::Read`; it never requires
  the entire input to be resident. Individual length-delimited values are
  buffered, each capped by the configured value-length budget.

## 2. Scalar types and wire types

Every field payload begins after a tag that names one of four **wire types**:

| Wire type | Bits | Name   | Payload                                              |
|-----------|------|--------|------------------------------------------------------|
| VARINT    | `0`  | varint | LEB128 unsigned varint (signed ints use zig-zag)     |
| I64       | `1`  | fixed64| 8 bytes, little-endian (IEEE-754 `double`)          |
| LEN       | `2`  | length | varint length `n`, followed by exactly `n` bytes    |
| I32       | `3`  | fixed32| 4 bytes, little-endian (IEEE-754 `float`)           |

Schema scalar types map to wire types as follows:

| Schema type | Signed | Wire type | Encoding                                        |
|-------------|--------|-----------|-------------------------------------------------|
| `int32`     | yes    | VARINT    | 32-bit zig-zag (`zigzag32`)                     |
| `int64`     | yes    | VARINT    | 64-bit zig-zag (`zigzag64`)                     |
| `uint32`    | no     | VARINT    | plain LEB128; values must fit in 32 bits        |
| `uint64`    | no     | VARINT    | plain LEB128                                    |
| `bool`      | n/a    | VARINT    | exactly `0` or `1`                               |
| `float`     | n/a    | I32       | 4-byte IEEE-754 binary32, little-endian         |
| `double`    | n/a    | I64       | 8-byte IEEE-754 binary64, little-endian         |
| `string`    | n/a    | LEN       | valid UTF-8 bytes                               |
| `bytes`     | n/a    | LEN       | opaque octets                                   |
| message     | n/a    | LEN       | an encoded sub-message exactly `n` bytes long   |

### 2.1 Varint

Unsigned LEB128: each byte carries 7 payload bits in bits `0..6`; bit `7` is
a continuation bit (1 = another byte follows). Bits are emitted least
significant group first. A varint uses **at most 10 bytes** for 64 bits; an
11th continuation byte is a protocol error.

### 2.2 Zig-zag

Signed integers are mapped bijectively to non-negative values so that small
magnitudes (positive *and* negative) stay compact:

```
zigzag32(n) = (n << 1) ^ (n >> 31)      // arithmetic right shift
zigzag64(n) = (n << 1) ^ (n >> 63)
```

Examples: `0 -> 0`, `-1 -> 1`, `1 -> 2`, `-2 -> 3`, `2 -> 4`.

## 3. Message layout

A message is a sequence of fields. There is **no end-of-message marker**:

* an embedded message occupies exactly the number of bytes declared by its
  surrounding LEN header;
* a top-level message ends at the EOF of its source (or at the payload
  boundary declared by the BSE1 envelope, see §6).

### 3.1 Tag

Each field starts with a single varint tag:

```
tag = (field_number << 3) | wire_type
```

* `field_number` is in `1 ..= 2^29 - 1`. Number `0` is rejected.
* The low three bits are the wire type (`0..=3`); any other value is rejected.

### 3.2 Field order and repetition

* Fields MAY appear in any order, not necessarily ascending.
* A field MAY appear more than once.
* For a **singular** (`required`/`optional`) field, if a number repeats, the
  last occurrence wins — but the field remains *present*. It never collapses
  back to "missing".
* For a **repeated** field, every occurrence is appended in wire order.

### 3.3 Packed repeated scalars

A `repeated` field of a numeric scalar type (varint, `float`, `double`; not
`string`, `bytes` or messages) is normally encoded **packed**: one tag with
wire type LEN, whose payload is the concatenation of the *tagless* encoded
primitives.

A decoder MUST accept both forms for the same repeated scalar:

* one packed LEN field;
* multiple ordinary fields, one tag per value.

This makes switching the `packed` flag a wire-compatible schema change.

## 4. Presence and cardinality

Every field has one of three cardinalities:

| Cardinality | Meaning on the wire |
|-------------|---------------------|
| `required`  | exactly one value; a message missing it is invalid |
| `optional`  | zero or one value |
| `repeated`  | zero to many values |

**Absence is encoded by omission** — an absent optional/repeated field emits
zero bytes. Consequently:

| JSON input                | Meaning                                   | Wire bytes |
|---------------------------|-------------------------------------------|------------|
| key omitted / `null` / `{"missing":true}` | field absent                | none       |
| `{"id": 0}`               | field **present** with explicit int zero  | `08 00`    |
| `{"active": false}`       | field **present** with explicit false     | `30 00`    |
| `{"name": ""}`            | field **present** with explicit empty text| a LEN with `n=0` |
| `{"tags": []}`            | repeated present but empty                | none (but distinct in the in-memory/JSON model) |

The decode JSON output uses an explicit presence wrapper for every field so the
distinction is always visible:

```json
{ "id": {"present": 0}, "email": {"missing": true}, "tags": {"values": []} }
```

## 5. Schema evolution rules

Two versions of a message type are compared over the union of their field
numbers. The rules are:

### 5.1 Adding a field

* Adding an `optional` or `repeated` field is **compatible**: old data simply
  lacks it, and the reader treats it as absent.
* Adding a `required` field is **incompatible**: existing messages cannot
  contain it and fail the required-field check.

### 5.2 Removing a field

* Removing a field is **compatible** for readers: the field becomes *unknown*
  and its raw bytes are captured and forwarded. Removed field numbers must not
  be reused for a different meaning.

### 5.3 Changing a type

* A change that moves a value to a **different wire family** is
  **incompatible** and reading is rejected at the first offending tag:
  e.g. `string`/`bytes` (LEN) ↔ integer (VARINT) ↔ `double` (I64) ↔
  `float` (I32), or scalar ↔ message.
* Within the same wire family the change is accepted: `int32 ↔ int64 ↔
  uint32 ↔ uint64` (all VARINT), `float` stays I32 and `double` stays I64.
  Width is validated: a varint that does not fit a declared 32-bit type is
  rejected rather than silently truncated.
* `bool ↔ int` shares the VARINT family but is reported as *directional /
  lossy* by the compatibility checker, because bool only carries 0/1.

### 5.4 Changing cardinality

| Old        | New        | Verdict      | Reason |
|------------|------------|--------------|--------|
| same       | same       | compatible   |        |
| `required` | `optional` | compatible   | widening |
| `required` | `repeated` | compatible   | one value becomes a one-element list |
| `optional` | `repeated` | compatible   | present value becomes one element |
| `optional` | `required` | incompatible | old data may omit the field |
| `repeated` | `optional` | incompatible | old data may carry many values |
| `repeated` | `required` | incompatible | old data may carry zero/many values |

### 5.5 Embedded messages

Unknown fields inside an embedded message are preserved independently at that
nesting level. Changing a field's referenced message type to a *different*
named type is incompatible.

## 6. Envelope (file/stream framing)

A standalone BSE1 document is wrapped in an envelope so files are
self-describing:

```
offset  size  field
0       4     magic, ASCII "BSE1"
4       1     version, currently 1
5       1     flags, currently 0 (any other value is rejected)
6       4     payload length, unsigned 32-bit little-endian
10      N     payload (the encoded top-level message), N == payload length
```

The envelope length lets a reader allocate/read exactly the top-level message
and detect trailing data or truncation. A payload that claims more bytes than
remain, or exceeds the configured total-output budget, is rejected.

## 7. Limits and error behaviour

A decode is always performed against a `Limits` value:

| Limit           | Default | Protects against |
|-----------------|---------|------------------|
| `max_value_len` | 16 MiB  | one huge LEN value / decompression-style blow-up |
| `max_output`    | 64 MiB  | total bytes charged by one decode |
| `max_depth`     | 32      | deeply nested messages (stack exhaustion) |

Every malformed condition produces a typed error rather than a panic,
including: truncated input (`UnexpectedEof`), a LEN header that overruns its
boundary (`LengthOutOfBounds`), over-limit values/output
(`LengthExceedsLimit`, `OutputLimitExceeded`), nesting too deep
(`NestingTooDeep`), field number zero, unknown wire type, an over-long varint,
a bool outside `{0,1}`, and non-UTF-8 strings.

## 8. Worked byte example

Encoding under a schema with `id` = field 1 (`int64`, required) and
`name` = field 2 (`string`, required), with `id = 0` and `name = "Ada"`:

```
08 00                        # tag=(1<<3)|0 = 0x08, varint payload 0   -> id = 0 (explicit zero)
12 03 41 64 61               # tag=(2<<3)|2 = 0x12, LEN len=3, "Ada"   -> name
```

Note the explicit `id = 0` still costs a byte (`08 00`); an absent optional
field would cost nothing at all.
