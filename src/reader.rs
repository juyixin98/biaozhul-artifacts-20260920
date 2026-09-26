//! Streaming BSE1 decoder.
//!
//! Reads from any [`Read`] without buffering whole messages. Length-delimited
//! payloads that must be retained for forwarding (unknown fields) are
//! buffered, but every byte is charged against [`Limits::max_message_bytes`]
//! *before* being read.
//!
//! # Evolution behaviour
//!
//! - A field **number** not present in the reader's schema is captured as an
//!   [`UnknownField`](crate::value::UnknownField) and copied byte-for-byte on
//!   re-encode (forwarding).
//! - A known number whose tag id is incompatible with the declared type is a
//!   hard [`Error::TypeMismatch`].
//! - `int32`/`int64` share the `VARINT` tag and `sint32`/`sint64` share
//!   `ZIGZAG`: widening reads always succeed; narrowing reads fail only when
//!   an actual value is out of range.
//! - Packed regions and unpacked entries for the same repeated field are both
//!   accepted; a packed region for a single field is rejected.
//! - Missing fields decode as absent, never as a default zero; an explicit
//!   on-wire zero decodes as present zero. The two are distinct in the JSON
//!   output (key omitted vs. key present with value 0).

use std::io::{self, Read};

use crate::error::{Error, Result};
use crate::ieee;
use crate::limits::Limits;
use crate::schema::{Field, Message, Presence, Repetition, Schema, Type};
use crate::value::{FieldValue, MessageValue, UnknownField, Value};
use crate::varint::{read_uvarint, zigzag_decode};
use crate::wire::{split_tag, tag_id_for, TagId};
use crate::writer::{HEADER_LEN, MAGIC, VERSION};

/// Result of a successful decode.
#[derive(Debug)]
pub struct DecodeOutput {
    pub message: MessageValue,
    /// Fingerprint stored in the file header (producing schema version).
    pub wire_fingerprint: u64,
    /// Fingerprint of the reader's schema.
    pub schema_fingerprint: u64,
}

impl DecodeOutput {
    /// True iff the data was produced by exactly the reader's schema.
    pub fn fingerprint_match(&self) -> bool {
        self.wire_fingerprint == self.schema_fingerprint
    }
}

/// Decode a complete message held in memory.
pub fn decode(schema: &Schema, input: &[u8], limits: &Limits) -> Result<DecodeOutput> {
    decode_from_stream(schema, io::Cursor::new(input), limits)
}

/// Decode from a stream. A clean end-of-stream ends the root message; a
/// mid-entry EOF is reported as truncated wire data.
pub fn decode_from_stream<R: Read>(
    schema: &Schema,
    input: R,
    limits: &Limits,
) -> Result<DecodeOutput> {
    let mut br = BoundedReader {
        inner: input,
        consumed: 0,
        limit: limits.max_message_bytes.saturating_add(HEADER_LEN as u64),
    };

    // ---- header ----
    let mut magic = [0u8; 4];
    br.read_exact(&mut magic).map_err(truncation("magic"))?;
    if &magic != MAGIC {
        return Err(Error::Wire(format!(
            "not a BSE1 file (bad magic {magic:02x?}, expected {MAGIC:02x?})"
        )));
    }
    let mut vf = [0u8; 2];
    br.read_exact(&mut vf)
        .map_err(truncation("version/flags"))?;
    if vf[0] != VERSION {
        return Err(Error::Wire(format!(
            "unsupported BSE version {} (this reader supports {VERSION})",
            vf[0]
        )));
    }
    if vf[1] != 0 {
        return Err(Error::Wire(format!(
            "unsupported header flags 0x{:02x} (requires a newer BSE reader)",
            vf[1]
        )));
    }
    let mut fp = [0u8; 8];
    br.read_exact(&mut fp).map_err(truncation("fingerprint"))?;
    let wire_fingerprint = u64::from_be_bytes(fp);

    // Body budget excludes the header.
    br.consumed = 0;
    br.limit = limits.max_message_bytes;

    let cx = DecodeCx { limits };
    let message = {
        let mut frame = Frame {
            br: &mut br,
            remaining: None,
        };
        cx.decode_body(schema, schema.root_message(), &mut frame, 0)?
    };
    Ok(DecodeOutput {
        message,
        wire_fingerprint,
        schema_fingerprint: crate::fingerprint::fingerprint(schema),
    })
}

fn truncation(part: &'static str) -> impl Fn(io::Error) -> Error {
    move |e: io::Error| match e.kind() {
        io::ErrorKind::UnexpectedEof => Error::Wire(format!(
            "unexpected end of input while reading {part} (truncated)"
        )),
        io::ErrorKind::OutOfMemory => Error::LimitExceeded(e.to_string()),
        _ => Error::Io(e),
    }
}

// ---------------------------------------------------------------------------
// Bounded I/O: global byte accounting against Limits::max_message_bytes
// ---------------------------------------------------------------------------

struct BoundedReader<R> {
    inner: R,
    consumed: u64,
    limit: u64,
}

impl<R: Read> BoundedReader<R> {
    fn read_exact(&mut self, buf: &mut [u8]) -> io::Result<()> {
        self.consumed = self.consumed.saturating_add(buf.len() as u64);
        if self.consumed > self.limit {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                format!("input exceeds {} byte limit", self.limit),
            ));
        }
        self.inner.read_exact(buf)
    }
}

// ---------------------------------------------------------------------------
// Frame: one message boundary.
//
// The root frame has remaining == None and ends at clean EOF. A nested
// message or packed region has remaining == Some(n) and ends exactly when
// the budget reaches zero. Every byte also passes through BoundedReader, so
// the global limit covers skipped unknown fields.
// ---------------------------------------------------------------------------

struct Frame<'a, R> {
    br: &'a mut BoundedReader<R>,
    remaining: Option<u64>,
}

impl<'a, R: Read> Frame<'a, R> {
    /// First byte of a new entry, or None at a clean frame boundary.
    fn entry_first_byte(&mut self) -> Result<Option<u8>> {
        if self.remaining == Some(0) {
            return Ok(None);
        }
        let mut b = 0u8;
        match self.br.read_exact(std::slice::from_mut(&mut b)) {
            Ok(()) => {
                self.charge(1)?;
                Ok(Some(b))
            }
            Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => {
                if self.remaining.is_none() {
                    Ok(None) // clean root EOF
                } else {
                    Err(truncated())
                }
            }
            Err(e) => Err(map_io(e)),
        }
    }

    fn read_exact(&mut self, buf: &mut [u8]) -> Result<()> {
        if let Some(rem) = self.remaining {
            if buf.len() as u64 > rem {
                return Err(truncated());
            }
        }
        self.br.read_exact(buf).map_err(map_io)?;
        self.charge(buf.len() as u64)?;
        Ok(())
    }

    fn read_u8(&mut self) -> Result<u8> {
        let mut b = [0u8; 1];
        self.read_exact(&mut b)?;
        Ok(b[0])
    }

    fn charge(&mut self, n: u64) -> Result<()> {
        if let Some(rem) = self.remaining.as_mut() {
            *rem -= n; // guarded by callers
        }
        Ok(())
    }

    /// Open a length-delimited sub-frame; its length varint is read from this
    /// frame and charged to it.
    fn open_subframe(&mut self) -> Result<Frame<'_, R>> {
        let length = read_frame_varint(self)?;
        self.subframe(length)
    }

    fn subframe(&mut self, length: u64) -> Result<Frame<'_, R>> {
        if let Some(rem) = self.remaining {
            if length > rem {
                return Err(truncated());
            }
            self.remaining = Some(rem - length);
        }
        Ok(Frame {
            br: self.br,
            remaining: Some(length),
        })
    }

    fn at_end(&self) -> bool {
        self.remaining == Some(0)
    }
}

fn truncated() -> Error {
    Error::Wire("unexpected end of input inside a length-delimited region (truncated)".into())
}

fn map_io(e: io::Error) -> Error {
    match e.kind() {
        io::ErrorKind::UnexpectedEof => {
            Error::Wire("unexpected end of input while reading field payload (truncated)".into())
        }
        io::ErrorKind::OutOfMemory => Error::LimitExceeded(e.to_string()),
        _ => Error::Io(e),
    }
}

/// Native varint read over a frame (no io::Read adapter, so the original
/// bschema error type — Wire vs LimitExceeded — is preserved).
fn read_frame_varint<R: Read>(frame: &mut Frame<R>) -> Result<u64> {
    let first = frame.read_u8()?;
    continue_frame_varint(frame, first)
}

/// Continue a varint whose first byte was already consumed.
fn continue_frame_varint<R: Read>(frame: &mut Frame<R>, first: u8) -> Result<u64> {
    if first & 0x80 == 0 {
        return Ok(u64::from(first & 0x7f));
    }
    let mut x = u64::from(first & 0x7f);
    for i in 1..10u32 {
        let b = frame.read_u8()?;
        if i == 9 && b > 1 {
            return Err(Error::Wire("varint overflow (u64)".into()));
        }
        x |= u64::from(b & 0x7f) << (7 * i);
        if b & 0x80 == 0 {
            return Ok(x);
        }
    }
    Err(Error::Wire("varint longer than 10 bytes".into()))
}

// ---------------------------------------------------------------------------
// Decoder
// ---------------------------------------------------------------------------

struct DecodeCx<'a> {
    limits: &'a Limits,
}

impl<'l> DecodeCx<'l> {
    fn decode_body<R: Read>(
        &self,
        schema: &Schema,
        msg_def: &Message,
        frame: &mut Frame<R>,
        depth: u32,
    ) -> Result<MessageValue> {
        let mut out = MessageValue::new();

        while let Some(first) = frame.entry_first_byte()? {
            // Continue the tag varint using the already-consumed first byte.
            let tag = continue_frame_varint(frame, first)?;
            let (number, id) = split_tag(tag)?;

            match msg_def.field_by_number(number) {
                Some(field) => self.decode_known(schema, field, id, frame, depth, &mut out)?,
                None => {
                    let payload = read_unknown_payload(id, frame)?;
                    out.unknown.push(UnknownField {
                        number,
                        tag: id,
                        payload,
                    });
                }
            }
        }

        for f in &msg_def.fields {
            if f.presence == Presence::Required && !out.fields.contains_key(&f.number) {
                return Err(Error::MissingRequired {
                    number: f.number,
                    name: f.name.clone(),
                });
            }
        }
        Ok(out)
    }

    fn decode_known<R: Read>(
        &self,
        schema: &Schema,
        field: &Field,
        id: TagId,
        frame: &mut Frame<R>,
        depth: u32,
        out: &mut MessageValue,
    ) -> Result<()> {
        let expected = tag_id_for(field.ty);

        if id == TagId::Packed {
            return match field.repetition {
                Repetition::Packed | Repetition::Unpacked => {
                    let values = self.decode_packed(schema, field, frame, depth)?;
                    for v in values {
                        out.push_repeated(field.number, v);
                    }
                    check_repeated_count(field, out, self.limits)
                }
                Repetition::Single => Err(Error::TypeMismatch {
                    number: field.number,
                    name: field.name.clone(),
                    expected: expected.as_str().into(),
                    found: "packed-region (field is not repeated)".into(),
                }),
            };
        }

        if id != expected {
            return Err(Error::TypeMismatch {
                number: field.number,
                name: field.name.clone(),
                expected: expected.as_str().into(),
                found: id.as_str().into(),
            });
        }

        let v = self.decode_scalar(schema, field, id, frame, depth)?;

        match field.repetition {
            Repetition::Single => {
                // Multiple entries for a single-valued field must not silently
                // collapse; reject rather than last-write-wins.
                if out.fields.contains_key(&field.number) {
                    return Err(Error::Wire(format!(
                        "field {} (`{}`) appears more than once but is not repeated",
                        field.number, field.name
                    )));
                }
                out.put(field.number, v);
            }
            Repetition::Packed | Repetition::Unpacked => {
                out.push_repeated(field.number, v);
                check_repeated_count(field, out, self.limits)?;
            }
        }
        Ok(())
    }

    fn decode_scalar<R: Read>(
        &self,
        schema: &Schema,
        field: &Field,
        id: TagId,
        frame: &mut Frame<R>,
        depth: u32,
    ) -> Result<Value> {
        Ok(match id {
            TagId::Varint => {
                let u = read_frame_varint(frame)?;
                match field.ty {
                    Type::Int32 => {
                        if u > i32::MAX as u64 {
                            return Err(range_err(field, u));
                        }
                        Value::Int32(u as i32)
                    }
                    Type::Int64 => {
                        if u > i64::MAX as u64 {
                            return Err(range_err(field, u));
                        }
                        Value::Int64(u as i64)
                    }
                    other => unreachable!("tag/type checked: {other:?}"),
                }
            }
            TagId::Zigzag => {
                let u = read_frame_varint(frame)?;
                let signed = zigzag_decode(u);
                match field.ty {
                    Type::Sint32 => {
                        if !(i32::MIN as i64..=i32::MAX as i64).contains(&signed) {
                            return Err(range_err(field, u));
                        }
                        Value::Sint32(signed as i32)
                    }
                    Type::Sint64 => Value::Sint64(signed),
                    other => unreachable!("tag/type checked: {other:?}"),
                }
            }
            TagId::Bool => {
                let u = read_frame_varint(frame)?;
                match u {
                    0 => Value::Bool(false),
                    1 => Value::Bool(true),
                    _ => {
                        return Err(Error::Wire(format!(
                            "bool field `{}` has non-canonical value {u} (must be 0 or 1)",
                            field.name
                        )))
                    }
                }
            }
            TagId::Fixed64 => {
                let mut b = [0u8; 8];
                frame.read_exact(&mut b)?;
                match field.ty {
                    Type::Fixed64 => Value::Fixed64(u64::from_be_bytes(b)),
                    other => unreachable!("tag/type checked: {other:?}"),
                }
            }
            TagId::Fixed32 => {
                let mut b = [0u8; 4];
                frame.read_exact(&mut b)?;
                Value::Fixed32(u32::from_be_bytes(b))
            }
            TagId::Double => {
                let mut b = [0u8; 8];
                frame.read_exact(&mut b)?;
                Value::Double(ieee::f64_from_be_bytes(&b))
            }
            TagId::String => {
                let bytes = read_region(frame)?;
                Value::Str(String::from_utf8(bytes)?)
            }
            TagId::Bytes => Value::Bytes(read_region(frame)?),
            TagId::Message => {
                let depth = depth + 1;
                if depth > self.limits.max_nesting {
                    return Err(Error::LimitExceeded(format!(
                        "nested message depth exceeds {}",
                        self.limits.max_nesting
                    )));
                }
                let target = field.references.as_deref().ok_or_else(|| {
                    Error::Wire(format!(
                        "field {} (`{}`) carries a message but reader schema declares a scalar",
                        field.number, field.name
                    ))
                })?;
                let sub_def = schema.message(target).ok_or_else(|| {
                    Error::Wire(format!(
                        "unknown nested message type `{target}` in reader schema"
                    ))
                })?;
                let mut sub = frame.open_subframe()?;
                let mv = self.decode_body(schema, sub_def, &mut sub, depth)?;
                if !sub.at_end() {
                    return Err(Error::Wire(
                        "nested message consumed fewer bytes than its declared length".into(),
                    ));
                }
                Value::Msg(mv)
            }
            TagId::Packed => unreachable!("handled in decode_known"),
        })
    }

    fn decode_packed<R: Read>(
        &self,
        schema: &Schema,
        field: &Field,
        frame: &mut Frame<R>,
        depth: u32,
    ) -> Result<Vec<Value>> {
        let length = read_frame_varint(frame)?;
        if length == 0 {
            return Err(Error::Wire(format!(
                "packed field `{}` has empty region (missing element-type byte)",
                field.name
            )));
        }
        let mut region = frame.subframe(length)?;
        let elem_byte = region.read_u8()?;
        let elem_id = TagId::from_u8(elem_byte)?;
        let expected = tag_id_for(field.ty);
        if !matches!(
            elem_id,
            TagId::Varint
                | TagId::Zigzag
                | TagId::Bool
                | TagId::Fixed32
                | TagId::Fixed64
                | TagId::Double
        ) || elem_id != expected
        {
            return Err(Error::TypeMismatch {
                number: field.number,
                name: field.name.clone(),
                expected: format!("packed {}", expected.as_str()),
                found: format!("packed {}", elem_id.as_str()),
            });
        }

        let mut values = Vec::new();
        while !region.at_end() {
            let v = self.decode_scalar(schema, field, elem_id, &mut region, depth)?;
            values.push(v);
            if values.len() > self.limits.max_repeated {
                return Err(Error::LimitExceeded(format!(
                    "repeated field `{}` exceeds {} values",
                    field.name, self.limits.max_repeated
                )));
            }
        }
        Ok(values)
    }
}

fn range_err(field: &Field, u: u64) -> Error {
    Error::Wire(format!(
        "value {u} for field `{}` is outside the {} range (incompatible int-width evolution?)",
        field.name,
        field.ty.as_str()
    ))
}

fn check_repeated_count(field: &Field, out: &MessageValue, limits: &Limits) -> Result<()> {
    if let Some(FieldValue::Repeated(v)) = out.fields.get(&field.number) {
        if v.len() > limits.max_repeated {
            return Err(Error::LimitExceeded(format!(
                "repeated field `{}` exceeds {} values",
                field.name, limits.max_repeated
            )));
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Unknown-field payload capture (raw bytes, for byte-exact forwarding)
// ---------------------------------------------------------------------------

fn read_region<R: Read>(frame: &mut Frame<R>) -> Result<Vec<u8>> {
    let length = read_frame_varint(frame)?;
    if length > isize::MAX as u64 {
        return Err(Error::LimitExceeded(format!(
            "length-delimited payload of {length} bytes is too large to buffer"
        )));
    }
    let mut buf = vec![0u8; length as usize];
    frame.read_exact(&mut buf)?;
    Ok(buf)
}

fn read_unknown_payload<R: Read>(id: TagId, frame: &mut Frame<R>) -> Result<Vec<u8>> {
    match id {
        TagId::Varint | TagId::Zigzag | TagId::Bool => {
            // Preserve raw varint bytes so re-emission is byte-exact, while
            // still validating the encoding. Validation and capture happen in
            // a single read pass (re-reading would consume bytes twice).
            let mut payload = Vec::with_capacity(10);
            loop {
                let b = frame.read_u8()?;
                payload.push(b);
                if b & 0x80 == 0 {
                    break;
                }
                if payload.len() >= 10 {
                    return Err(Error::Wire("varint longer than 10 bytes".into()));
                }
            }
            let mut cur = io::Cursor::new(&payload);
            read_uvarint(&mut cur)?;
            Ok(payload)
        }
        TagId::Fixed64 | TagId::Double => {
            let mut b = vec![0u8; 8];
            frame.read_exact(&mut b)?;
            Ok(b)
        }
        TagId::Fixed32 => {
            let mut b = vec![0u8; 4];
            frame.read_exact(&mut b)?;
            Ok(b)
        }
        TagId::String | TagId::Bytes | TagId::Message | TagId::Packed => read_region(frame),
    }
}
