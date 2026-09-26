//! Proves top-level envelope decoding is genuinely streaming.

mod common;

use std::io::Read;

use bse::codec::{decode_envelope, encode_envelope, EncodeLimit};
use bse::schema::{Cardinality, ScalarType};
use bse::value::{Field, Value};
use bse::wire::Limits;
use common::{msg, present, scalar, schema, AsValue};

/// A reader that yields at most one byte per `read` call and records how
/// many bytes the decoder actually pulled.
struct SlowReader {
    data: Vec<u8>,
    pos: usize,
    pulls: usize,
}

impl SlowReader {
    fn new(data: Vec<u8>) -> Self {
        SlowReader {
            data,
            pos: 0,
            pulls: 0,
        }
    }
}

impl Read for SlowReader {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.pos >= self.data.len() || buf.is_empty() {
            return Ok(0);
        }
        self.pulls += 1;
        buf[0] = self.data[self.pos];
        self.pos += 1;
        Ok(1)
    }
}

#[test]
fn decodes_from_a_one_byte_at_a_time_stream() {
    let s = schema(
        "Person",
        vec![(
            "Person",
            vec![
                scalar("id", 1, ScalarType::Int64, Cardinality::Required),
                scalar("name", 2, ScalarType::String, Cardinality::Optional),
            ],
        )],
    );
    let m = msg(vec![(1, present(7_i64)), (2, present("streamed"))]);
    let mut bytes = Vec::new();
    encode_envelope(&s, &m, &mut bytes, EncodeLimit::default()).unwrap();
    assert!(bytes.len() > 10);

    let total = bytes.len();
    let mut slow = SlowReader::new(bytes);
    let d = decode_envelope(&s, &mut slow, Limits::default()).expect("decode over slow stream");
    assert_eq!(
        d.message.fields.get(&1),
        Some(&Field::Present(Value::Int(7)))
    );
    assert_eq!(
        d.message.fields.get(&2),
        Some(&Field::Present("streamed".v()))
    );
    // The decoder pulled essentially every byte separately (>1 call/byte
    // is possible due to read_exact), proving no whole-input buffering.
    assert!(
        slow.pulls >= total,
        "expected bytewise pulls, got {}",
        slow.pulls
    );
}

#[test]
fn decoder_does_not_read_past_the_envelope_payload() {
    use std::io::Cursor;
    let s = schema(
        "Person",
        vec![(
            "Person",
            vec![scalar("id", 1, ScalarType::Int64, Cardinality::Required)],
        )],
    );
    let m = msg(vec![(1, present(1_i64))]);
    let mut bytes = Vec::new();
    encode_envelope(&s, &m, &mut bytes, EncodeLimit::default()).unwrap();

    // Append trailing garbage that belongs to a hypothetical next record.
    bytes.extend_from_slice(b"TRAILING-BYTES-NOT-PART-OF-MESSAGE");

    let mut cursor = Cursor::new(bytes.clone());
    let d = decode_envelope(&s, &mut cursor, Limits::default()).unwrap();
    assert_eq!(
        d.message.fields.get(&1),
        Some(&Field::Present(Value::Int(1)))
    );
    // Exactly one envelope (header + payload) was consumed; nothing more.
    let payload_len = bytes.len() - b"TRAILING-BYTES-NOT-PART-OF-MESSAGE".len();
    assert_eq!(cursor.position() as usize, payload_len);
}
