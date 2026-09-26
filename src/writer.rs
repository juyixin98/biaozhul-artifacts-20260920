//! Streaming BSE1 encoder.
//!
//! File layout (see `FORMAT.md`):
//! ```text
//! "BSE1" | version:u8(=1) | flags:u8(=0) | fingerprint:u64 BE | body...
//! ```
//! The body is a sequence of tagged field entries; a root message ends with
//! the stream. Nested messages and packed regions are length-delimited and
//! self-framing, which is what makes a streaming decoder possible.

use std::io::Write;

use crate::error::{Error, Result};
use crate::ieee;
use crate::limits::Limits;
use crate::schema::{Field, Message, Presence, Repetition, Schema, Type};
use crate::value::{FieldValue, MessageValue, UnknownField, Value};
use crate::varint::{uvarint_len, write_uvarint, zigzag_encode};
use crate::wire::{make_tag, tag_id_for, TagId};

/// File magic and version.
pub const MAGIC: &[u8; 4] = b"BSE1";
pub const VERSION: u8 = 1;
pub const HEADER_LEN: usize = 14;

/// Encode a root message to a freshly allocated, limit-checked buffer.
pub fn encode(schema: &Schema, msg: &MessageValue, limits: &Limits) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    encode_to_stream(schema, msg, limits, &mut out)?;
    Ok(out)
}

/// Encode a root message to any [`Write`].
///
/// Field entries are streamed directly; only nested message bodies and packed
/// regions are buffered (a length prefix must precede the bytes), and each
/// buffer is bounded by `limits.max_output_bytes`.
pub fn encode_to_stream<W: Write>(
    schema: &Schema,
    msg: &MessageValue,
    limits: &Limits,
    w: &mut W,
) -> Result<()> {
    w.write_all(MAGIC)?;
    w.write_all(&[VERSION, 0])?;
    w.write_all(&crate::fingerprint::fingerprint(schema).to_be_bytes())?;

    let mut cw = CountingWrite {
        inner: w,
        written: HEADER_LEN as u64,
        limit: limits.max_output_bytes,
    };
    write_body(schema, schema.root_message(), msg, limits, 0, &mut cw)?;
    Ok(())
}

struct CountingWrite<'a, W: Write + ?Sized> {
    inner: &'a mut W,
    written: u64,
    limit: u64,
}

impl<W: Write + ?Sized> CountingWrite<'_, W> {
    fn write_bytes(&mut self, b: &[u8]) -> Result<()> {
        self.written = self.written.saturating_add(b.len() as u64);
        if self.written > self.limit {
            return Err(Error::LimitExceeded(format!(
                "encoded output exceeds {} bytes",
                self.limit
            )));
        }
        self.inner.write_all(b)?;
        Ok(())
    }
}

/// Write a message body: known fields in schema order, then unknown fields.
fn write_body<W: Write + ?Sized>(
    schema: &Schema,
    msg_def: &Message,
    msg: &MessageValue,
    limits: &Limits,
    depth: u32,
    w: &mut CountingWrite<W>,
) -> Result<()> {
    for field in &msg_def.fields {
        match msg.fields.get(&field.number) {
            None | Some(FieldValue::Absent) => {
                if field.presence == Presence::Required {
                    return Err(Error::MissingRequired {
                        number: field.number,
                        name: field.name.clone(),
                    });
                }
            }
            Some(FieldValue::Present(v)) => {
                write_entry(schema, field, v, limits, depth, w)?;
            }
            Some(FieldValue::Repeated(values)) => {
                if values.len() > limits.max_repeated {
                    return Err(Error::LimitExceeded(format!(
                        "repeated field `{}` has {} values (limit {})",
                        field.name,
                        values.len(),
                        limits.max_repeated
                    )));
                }
                match field.repetition {
                    Repetition::Packed => write_packed(schema, field, values, limits, depth, w)?,
                    Repetition::Unpacked | Repetition::Single => {
                        for v in values {
                            write_entry(schema, field, v, limits, depth, w)?;
                        }
                    }
                }
            }
        }
    }

    for u in &msg.unknown {
        write_unknown(w, u)?;
    }
    Ok(())
}

/// Write one tagged entry.
fn write_entry<W: Write + ?Sized>(
    schema: &Schema,
    field: &Field,
    v: &Value,
    limits: &Limits,
    depth: u32,
    w: &mut CountingWrite<W>,
) -> Result<()> {
    let id = tag_id_for(field.ty);
    if v.tag_id() != id {
        return Err(Error::InvalidValue(format!(
            "field `{}`: value does not match declared type {}",
            field.name,
            field.ty.as_str()
        )));
    }
    write_tag(w, field.number, id)?;
    write_payload(schema, field, v, limits, depth, w)
}

fn write_tag<W: Write + ?Sized>(w: &mut CountingWrite<W>, number: u32, id: TagId) -> Result<()> {
    let tag = make_tag(number, id);
    let mut buf = [0u8; 10];
    let n = write_uvarint(&mut &mut buf[..], tag).unwrap();
    debug_assert_eq!(n, uvarint_len(tag));
    w.write_bytes(&buf[..n])
}

fn write_varint_value<W: Write + ?Sized>(w: &mut CountingWrite<W>, v: u64) -> Result<()> {
    let mut buf = [0u8; 10];
    let n = write_uvarint(&mut &mut buf[..], v).unwrap();
    w.write_bytes(&buf[..n])
}

fn write_length<W: Write + ?Sized>(w: &mut CountingWrite<W>, b: &[u8]) -> Result<()> {
    write_varint_value(w, b.len() as u64)?;
    w.write_bytes(b)
}

/// Write the payload bytes of `v` (the caller has already written the tag).
fn write_payload<W: Write + ?Sized>(
    schema: &Schema,
    field: &Field,
    v: &Value,
    limits: &Limits,
    depth: u32,
    w: &mut CountingWrite<W>,
) -> Result<()> {
    match v {
        Value::Int64(i) => {
            if *i < 0 {
                return Err(Error::InvalidValue(format!(
                    "field `{}`: int64 must be non-negative, got {i} (use sint64 for signed)",
                    field.name
                )));
            }
            write_varint_value(w, *i as u64)
        }
        Value::Sint64(i) => write_varint_value(w, zigzag_encode(*i)),
        Value::Int32(i) => {
            if *i < 0 {
                return Err(Error::InvalidValue(format!(
                    "field `{}`: int32 must be non-negative, got {i} (use sint32 for signed)",
                    field.name
                )));
            }
            write_varint_value(w, *i as u32 as u64)
        }
        Value::Sint32(i) => {
            // Zig-zag in signed 32-bit arithmetic (wrapping shl), then zero-
            // extend: zz(-7) = 13, zz(i32::MIN) = 2^32-1 (a 5-byte varint).
            let zz = (i.wrapping_shl(1) ^ (i >> 31)) as u32 as u64;
            write_varint_value(w, zz)
        }
        Value::Fixed64(u) => w.write_bytes(&u.to_be_bytes()),
        Value::Fixed32(u) => w.write_bytes(&u.to_be_bytes()),
        Value::Bool(b) => write_varint_value(w, u64::from(*b)),
        Value::Double(f) => w.write_bytes(&ieee::f64_to_be_bytes(*f)),
        Value::Str(s) => {
            let b = s.as_bytes();
            write_length(w, b)
        }
        Value::Bytes(b) => write_length(w, b),
        Value::Msg(sub) => {
            let depth = depth + 1;
            if depth > limits.max_nesting {
                return Err(Error::LimitExceeded(format!(
                    "nested message depth exceeds {}",
                    limits.max_nesting
                )));
            }
            let target = field.references.as_deref().ok_or_else(|| {
                Error::InvalidValue(format!(
                    "field `{}` value is a message but field is not message-typed",
                    field.name
                ))
            })?;
            let sub_def = schema.message(target).ok_or_else(|| {
                Error::Schema(format!("nested message type `{target}` not in schema"))
            })?;
            let mut body = Vec::new();
            {
                let mut cw = CountingWrite {
                    inner: &mut body,
                    written: 0,
                    limit: limits.max_output_bytes,
                };
                write_body(schema, sub_def, sub, limits, depth, &mut cw)?;
            }
            write_length(w, &body)
        }
    }
}

/// Packed scalar region: tag(PACKED), length, 1 element-type byte, then
/// concatenated element payloads.
fn write_packed<W: Write + ?Sized>(
    schema: &Schema,
    field: &Field,
    values: &[Value],
    limits: &Limits,
    depth: u32,
    w: &mut CountingWrite<W>,
) -> Result<()> {
    if field.ty == Type::Message {
        return Err(Error::Schema(
            "packed repetition cannot be used on message fields".into(),
        ));
    }
    let elem_id = tag_id_for(field.ty);
    let mut region = Vec::new();
    region.push(elem_id.as_u8());
    {
        let mut sub = CountingWrite {
            inner: &mut region,
            written: 1,
            limit: limits.max_output_bytes,
        };
        for v in values {
            if v.tag_id() != elem_id {
                return Err(Error::InvalidValue(format!(
                    "repeated field `{}`: value does not match declared type {}",
                    field.name,
                    field.ty.as_str()
                )));
            }
            write_payload(schema, field, v, limits, depth, &mut sub)?;
        }
    }
    write_tag(w, field.number, TagId::Packed)?;
    write_length(w, &region)
}

/// Re-emit an unknown field exactly as captured on the wire.
///
/// For length-delimited tags (`BYTES`/`STRING`/`MESSAGE`/`PACKED`) `payload`
/// holds the region without its length prefix; for fixed tags it is the raw
/// fixed-width bytes; for `VARINT`/`ZIGZAG`/`BOOL` it is the raw varint
/// bytes.
fn write_unknown<W: Write + ?Sized>(w: &mut CountingWrite<W>, u: &UnknownField) -> Result<()> {
    write_tag(w, u.number, u.tag)?;
    if u.tag.is_length_delimited() {
        write_length(w, &u.payload)
    } else {
        w.write_bytes(&u.payload)
    }
}
