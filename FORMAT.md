# BSE1 Wire Format Specification

**Status:** normative for `bschema` 0.1.0
**Scope:** describes the exact byte layout that the library and CLI
produce/consume. All multi-byte fixed-width integers are **big-endian**
(network order). Variable-length integers are unsigned **LEB128** (the same
base-128 varint used by Protocol Buffers), at most 10 bytes for a u64.

---

## 1. Design goals

1. **Schema evolution by field number.** Fields carry stable numbers; names
   are not serialized and may be changed freely.
2. **Unknown-field retention.** A reader that does not know a field number
   keeps its raw payload and forwards it byte-for-byte.
3. **Explicit presence.** An absent optional field is *not* the same as a
   field explicitly set to zero / empty.
4. **Incompatible type changes are rejected, not reinterpreted.** Every
   scalar family that must never be silently misread has its own 4-bit type
   discriminator in the tag.
5. **Bounded streaming.** Decoders work over any `Read`, never buffer more
   than the configured byte limit, and enforce nesting and repetition caps.

## 2. File layout

```
offset  size  field
------  ----  -------------------------------------------------
0       4     magic: ASCII "BSE1"
4       1     version: u8, currently 1
5       1     flags: u8, currently 0 (nonzero => newer reader)
6       8     schema fingerprint: u64 big-endian (see §6)
14      ...   root message body: zero or more field entries
```

A file/stream contains exactly one root message; the message ends when the
stream ends. Nested messages are length-delimited (§4), so they are
self-framing and the codec is streaming: no total-length prefix is needed for
the root.

Truncated input (EOF in the middle of any entry, length prefix, nested region
or fixed-width value) is a hard *corrupt wire data* error. A clean EOF
between entries is the normal end of the root message.

## 3. Field tags

Every field entry begins with one uvarint:

```
tag = (field_number << 4) | type_id
```

- `field_number` is a positive u29 (1 .. 536,870,911). Number 0 is reserved
  and rejected.
- `type_id` is one of:

| id | name     | payload                                              | schema types        |
|----|----------|------------------------------------------------------|---------------------|
| 0  | VARINT   | unsigned LEB128                                      | `int32`, `int64`    |
| 1  | ZIGZAG   | zig-zag LEB128                                       | `sint32`, `sint64`  |
| 2  | BOOL     | LEB128, value **must** be 0 or 1                     | `bool`              |
| 3  | FIXED64  | 8 bytes BE                                           | `fixed64`           |
| 4  | FIXED32  | 4 bytes BE                                           | `fixed32`           |
| 5  | DOUBLE   | 8 bytes BE, IEEE 754 binary64 (all bit patterns)     | `double`            |
| 6  | BYTES    | LEB128 length + raw octets                           | `bytes`             |
| 7  | STRING   | LEB128 length + UTF-8 octets (validated)             | `string`            |
| 8  | MESSAGE  | LEB128 length + nested message body                  | nested message      |
| 9  | PACKED   | LEB128 length + 1 element-type byte + element bytes  | packed repeated     |

A type id outside 0..=9 is rejected as corrupt ("requires a newer reader")
rather than guessed at.

### 3.1 Integer encodings

- **VARINT** payloads are interpreted as unsigned. `int32` accepts
  0..=2,147,483,647 and `int64` accepts 0..=9,223,372,036,854,775,807;
  values outside the declared width are rejected on decode (see §7).
  Negative values have no representation here; use the signed types.
- **ZIGZAG** maps signed integers bijectively to unsigned varints:
  `zz(n) = (n << 1) ^ (n >> 63)` (64-bit; 32-bit fields use the 32-bit
  equivalent). Thus 0→0, −1→1, 1→2, −2→3, …
- `sint32` accepts the full i32 range; values outside it are rejected.
- **FIXED32/FIXED64** are unsigned big-endian.
- **BOOL** rejects any varint other than canonical `0`/`1`.
- **DOUBLE** preserves every bit pattern including NaN payloads and −0.0.

## 4. Length-delimited payloads

`BYTES`, `STRING`, `MESSAGE`:

```
length: uvarint   (number of bytes that follow)
data:    length bytes
```

- `STRING` data must be valid UTF-8; invalid UTF-8 is a decode error.
- `MESSAGE` data is itself a sequence of field entries ending exactly when
  the declared length is consumed. Consuming too few or running past the
  declared length is corrupt.
- Lengths that exceed the configured input byte budget, that run past the
  stream, or that exceed the addressable allocation size are rejected before
  the bytes are read.

## 5. Repetition: packed and unpacked

Schema fields may be:

- single-valued (default),
- `"repeated": true` / `"unpacked"` — one tagged entry per value,
- `"repeated": "packed"` — one `PACKED` region for all values.

A PACKED region's payload is:

```
length: uvarint
elem_type: u8          // one of the scalar ids 0..=6 (never BYTES/STRING/MESSAGE/PACKED)
elements:  concatenated scalar payloads (no separators)
```

Packed is only valid for scalar numeric types (`int*`, `sint*`, `bool`,
`fixed*`, `double`). A packed region for a non-repeated field, or whose
element type does not equal the field's type id, is an incompatibility error.

**Interoperability of the two strategies:** readers accept both shapes for
any repeated field — unpacked entries and packed regions may even be
interleaved on the wire, and values are appended in encounter order. Writers
emit the shape declared by their schema. Changing the declared repetition
strategy is therefore a compatible evolution.

A single-valued field appearing more than once on the wire is rejected
(last-write-wins would silently destroy data).

## 6. Schema fingerprint

The header carries an FNV-1a 64-bit fingerprint of the schema's **wire
contract**, rendered as 16 hexadecimal digits in JSON output. The canonical
text hashed is:

```
BSE1|root=<rootName>
;m=<messageName>|f=<number>,<typeToken>[-><reference>],<req|opt>,<1|packed|unpacked>
... (messages sorted by name; fields within a message sorted by number)
```

Field **names are deliberately excluded**: renaming a field does not change
the wire contract. Adding/removing a field or changing a type/rule changes
the fingerprint, which lets a reader report whether data came from exactly
its schema version (`fingerprint_match`) without a schema registry. The
fingerprint is informational — evolution rules below are always enforced
from field numbers and type ids, never from fingerprint equality.

## 7. Evolution rules

| Change                                        | Result                              |
|-----------------------------------------------|-------------------------------------|
| Rename a field / message                      | Compatible (fingerprint unchanged)  |
| Add an optional field                         | Old readers forward it as unknown   |
| Add a required field                          | **Not allowed** by data semantics: old data lacks it; add as optional |
| Remove a field                                | Safe; old data's bytes are unknown to new readers |
| `int32` → `int64`, `sint32` → `sint64`        | Compatible widening; reader enforces the target width per value |
| `int64` → `int32` / `sint64` → `sint32`       | Rejected the moment an out-of-range value appears |
| Any other type change (e.g. string→int64, fixed64→double, string→bytes, varint→zigzag) | **Rejected** (`incompatible type`) from the tag itself |
| packed ↔ unpacked repetition                  | Compatible (both shapes read)       |
| Repeated ↔ single                             | Rejected when the conflicting shape appears |
| Message A → message B for a field             | Distinct from scalars; readers validate the nested body against the declared referenced type |

Errors are explicit:

- `incompatible type for field N (name): expected wire type X, found Y`
- `required field N (name) is missing`
- `value V for field .. is outside the int32 range (incompatible int-width evolution?)`

## 8. Presence semantics

- Optional fields are omitted entirely when unset: no tag on the wire, no key
  in the JSON output.
- An explicit zero (`0`, `0.0`, `false`, `""`, empty bytes) is a normal
  present value: the tag is written and the JSON key appears.
- `null` for an optional field in JSON encode input means *absent*.
- An empty repeated array writes nothing and reads back as absent; there is
  no wire distinction between unset and empty for repeated fields (the list
  itself is what presence would guard).
- Required fields are enforced on encode **and** decode; a missing required
  field is an error even if everything else parsed.

## 9. Resource limits

All limits are explicit (`--max-bytes`, `--max-depth`, `--max-repeated`, or
the request envelope's `limits` object):

| limit               | default  | enforced against                                        |
|---------------------|----------|--------------------------------------------------------|
| max message bytes   | 64 MiB   | every body byte read, including skipped unknown fields |
| max output bytes    | 64 MiB   | every body byte written                                |
| max nesting depth   | 64       | nested MESSAGE entries, encode and decode              |
| max repeated values | 1,000,000| elements accumulated per repeated field                |

A claimed length is checked against the remaining budget *before* its bytes
are read, so a hostile `length = 2^63−1` header cannot trigger a huge
allocation.

## 10. Worked example

Schema:

```json
{ "root": "Person", "messages": [{ "name": "Person", "fields": [
  {"number": 1, "name": "id",   "type": "int32", "required": true},
  {"number": 2, "name": "name", "type": "string"},
  {"number": 3, "name": "admin","type": "bool"}
]}]}
```

Message `{"id": 1, "name": "Ada"}` (admin absent):

```
42 53 45 31                         "BSE1"
01 00                               version 1, flags 0
xx xx xx xx xx xx xx xx             schema fingerprint (u64 BE)
11                                  tag: (1<<4)|0 = 0x11  field 1 VARINT
01                                  int32 value 1
27                                  tag: (2<<4)|7 = 0x27  field 2 STRING
03 41 64 61                         length 3, "Ada"
```

Field 3 is absent; a reader with field 4, `age` (`int32`), would decode
this with no `age` key, while bytes tagged as field 4 from a newer writer
would be captured under `__unknown_fields__` in JSON and forwarded
unchanged.

## 11. Security / robustness notes

- All input validation happens at the boundary (wire and JSON).
- No recursion beyond `max_nesting`; unknown nested fields are copied as
  opaque bounded regions and do not recurse.
- Unknown varint payloads are capped at 10 bytes and validated.
- The decoder never allocates for a length before charging it against the
  byte budget.
- JSON output is valid JSON: non-finite doubles are emitted as `null`
  (the binary keeps the exact NaN/Infinity bits for same-schema consumers).
