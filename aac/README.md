# aac — Adaptive Arithmetic Coding (pure backend)

A small, **dependency-free** Rust library and command-line tool for streaming
**adaptive integer-range arithmetic coding** of binary data, plus a **JSON
control entry point**. The bit/byte format is fully documented in
[`FORMAT.md`](./FORMAT.md).

No frontend is provided.

## Features

- Integer interval arithmetic coder with the classic E1/E2/E3 renormalization
  and deferred-bit (pending) carry handling — implemented from scratch.
- Adaptive order-0 model over `256 byte symbols + 1 EOF symbol`, Fenwick-tree
  cumulative frequencies, and a **fixed** halving rescale rule shared
  identically by encoder and decoder.
- Explicit **EOF terminator**; byte-level **truncation is always detected**
  (the valid stream carries a flush + 32 zero-tail bits, so a decoder never
  relies on imaginary padding for a complete stream).
- Self-describing `AC01` container with length fields and **CRC-32**.
- **Bounded memory**: fixed-size model (257 entries) and 64 KiB I/O buffers;
  encoding and decoding stream with constant memory regardless of file size.
- **Bounded output**: decoded length and coded-payload growth are capped.
- **Deterministic**: the same input always yields the same bytes.
- Zero third-party crates (standard library only); builds offline.

## Layout

```
aac/
├── Cargo.toml
├── FORMAT.md            # bit-level format specification
├── README.md
├── examples/
│   └── roundtrip.rs     # minimal library round-trip demo
├── examples-requests/   # ready-to-use JSON requests + captured responses
├── scripts/
│   └── acceptance.sh    # reproducible end-to-end acceptance checklist
├── src/
│   ├── lib.rs           # crate API
│   ├── main.rs          # CLI + JSON control entry point
│   ├── constants.rs     # frozen format constants
│   ├── model.rs         # adaptive model + Fenwick tree + rescaling
│   ├── coder.rs         # integer-range encoder/decoder
│   ├── bitio.rs         # MSB-first bit-at-a-time Read/Write adapters
│   ├── container.rs     # AC01 envelope, CRC-32, streaming pack/unpack
│   ├── jsonutil.rs      # minimal JSON parser/serializer + base64
│   └── error.rs         # typed errors
└── tests/
    └── codec_test.rs    # end-to-end / acceptance tests
```

## Build

Requires only a Rust toolchain with `std` (developed and tested with Rust
1.75 and 1.98):

```sh
cargo build --release
```

The binary is `target/release/aac`.

## Command-line usage

```sh
# Encode a file into an AC01 container
aac encode -i INPUT -o OUTPUT.ac01 [--max-payload-bytes N]

# Decode back
aac decode -i INPUT.ac01 -o OUTPUT [--max-output-bytes N]
```

Use `-` for a path to read stdin / write stdout. Both modes stream, so they
work on files much larger than memory.

On success, a one-line summary is printed to **stderr**; stdout stays clean
for piping. Exit status is non-zero on any error.

## JSON control entry point

`aac json [REQUEST.json]` reads one JSON request from the named file or
**stdin**, and writes one JSON response to **stdout**. Byte payloads travel as
standard padded **base64**.

### Request

```json
{ "op": "encode", "data": "<base64 raw source bytes>", "max_output": 12345 }
{ "op": "decode", "data": "<base64 AC01 container>",   "max_output": 12345 }
```

`max_output` is optional. For `encode` it caps coded payload size; for
`decode` it caps decoded byte length.

### Success response

```json
{
  "ok": true,
  "op": "encode",
  "result": "<base64>",
  "stats": {
    "input_bytes": 27,
    "output_bytes": 31,
    "coded_bits": 245,
    "rescales": 0,
    "symbols": 28
  }
}
```

### Error response

```json
{ "ok": false, "op": "decode", "error": "UnexpectedEnd",
  "message": "unexpected end of coded stream (missing or damaged terminator)" }
```

Error `error` codes: `Io`, `InvalidFormat`, `UnexpectedEnd`,
`OutputLimitExceeded`, `InvalidBase64`, `InvalidJson`. A failed op exits with
status 1 while still emitting the JSON envelope.

See [`examples-requests/`](./examples-requests) for ready-to-use sample
requests (and their captured responses):

- `encode-empty.json` — encode the empty string;
- `encode-hello.json` / `decode-hello.json` — a full round-trip;
- `decode-hello-cap3.json` — output-length cap rejection;
- `decode-truncated.json` — truncated/corrupt container rejection;
- `encode-badbase64.json` — malformed base64 rejection.

### Quick examples

```sh
# encode the text "hello"
printf '{"op":"encode","data":"%s"}' "$(printf hello | base64)" | aac json

# round-trip through JSON in a shell (jq-style without dependencies):
echo '{"op":"encode","data":"aGVsbG8="}' | aac json > enc.json
```

## Library API

```rust
use aac::{pack, unpack, pack_stream, unpack_stream};

// Buffered, whole-slice convenience:
let (container, enc_stats) = pack(b"hello", None)?;
let (decoded, dec_stats) = unpack(&container, None)?;
assert_eq!(decoded, b"hello");

// Constant-memory streaming over arbitrary Read/Write:
let mut file = std::fs::File::create("out.ac01")?;
pack_stream(std::io::stdin(), &mut file, Some(1 << 30))?;

let src = std::fs::File::open("out.ac01")?;
let mut out = std::io::stdout();
unpack_stream(src, &mut out, Some(1 << 30))?;
```

Lower-level building blocks are public too: `aac::Encoder`,
`aac::Decoder`, `aac::Model`, `aac::coder::encode_bytes/decode_bytes`
(raw payload without container), and `aac::Crc32`.

## Tests

```sh
cargo test
```

The suite (34 tests) covers, among other things:

- empty input;
- every single byte value and a buffer containing all 256 values;
- long heavily-skewed streams that trigger **many rescales**;
- 40 seeded random fuzz round-trips (uniform and biased);
- exhaustive enumeration of all binary sequences up to length 9 and all
  ternary sequences up to length 5;
- every truncated prefix of several containers and raw payloads;
- byte corruption at many positions, bad magic/version, forged lengths,
  non-zero padding;
- output / payload length limits;
- deterministic repeated encoding;
- byte-identical output from streaming and buffered APIs.

## Security & robustness notes

- All lengths are untrusted and validated; `original_length` is cross-checked
  against the actual decoded count and the caller cap.
- CRC-32 guards integrity; it detects accidental truncation/corruption but is
  not a cryptographic authentication mechanism.
- No unsafe code, no heap growth proportional to input, no network access.
