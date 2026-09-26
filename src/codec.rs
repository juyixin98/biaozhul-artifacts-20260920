//! Schema-driven encoder and decoder.
//!
//! * [`encode_message`] validates a [`Message`] against a [`Schema`] and
//!   streams the BSE1 fields to any [`Write`].
//! * [`decode_message`] streams fields from any [`Read`], accepts fields
//!   known to the schema, captures unknown fields verbatim, and rejects
//!   wire data that is incompatible with the schema (wrong wire type,
//!   missing required field, truncation, over-long values).

use std::io::{Read, Write};

use crate::error::{Error, Result};
use crate::schema::{Cardinality, FieldDef, MessageDef, ScalarType, Schema, TypeDef};
use crate::value::{Field, Message, Value};
use crate::wire::{
    write_envelope, zigzag_decode32, zigzag_decode64, zigzag_encode32, zigzag_encode64, Limits,
    RawField, WireReader, WireType, WireWriter,
};

// ---------------------------------------------------------------------------
// Encoder side
// ---------------------------------------------------------------------------

/// Upper bound applied while encoding.
#[derive(Debug, Clone, Copy)]
pub struct EncodeLimit {
    /// Maximum total encoded size in bytes.
    pub max_output: u64,
}

impl Default for EncodeLimit {
    fn default() -> Self {
        EncodeLimit {
            max_output: 64 * 1024 * 1024,
        }
    }
}

/// Encode the schema's root message and wrap it in a BSE1 envelope.
/// Returns the number of payload bytes written.
pub fn encode_envelope<W: Write>(
    schema: &Schema,
    message: &Message,
    out: &mut W,
    limit: EncodeLimit,
) -> Result<u64> {
    let mut payload = Vec::new();
    let n = encode_message(schema, schema.root_message(), message, &mut payload, limit)?;
    write_envelope(out, &payload)?;
    Ok(n)
}

/// Encode one message definition's fields to `out`. Returns wire bytes.
///
/// The message is assembled in an internal buffer and only written once it
/// is known to fit `limit`, so an over-limit message never partially
/// reaches `out`.
pub fn encode_message<W: Write>(
    schema: &Schema,
    msg_def: &MessageDef,
    message: &Message,
    out: &mut W,
    limit: EncodeLimit,
) -> Result<u64> {
    let mut buffer = Vec::new();
    {
        let mut writer = WireWriter::new(&mut buffer);
        encode_fields(schema, msg_def, message, &mut writer)?;
        writer.into_inner()?;
    }
    if buffer.len() as u64 > limit.max_output {
        return Err(Error::OutputLimitExceeded {
            limit: limit.max_output,
        });
    }
    out.write_all(&buffer)?;
    Ok(buffer.len() as u64)
}

fn encode_fields<W: Write>(
    schema: &Schema,
    msg_def: &MessageDef,
    message: &Message,
    w: &mut WireWriter<W>,
) -> Result<()> {
    // Known fields in ascending field-number order.
    for (number, field) in &message.fields {
        let def = msg_def.field(*number).ok_or_else(|| {
            Error::TypeMismatch(format!("field {number} is not declared in schema"))
        })?;
        match field {
            Field::Missing => {
                if def.cardinality == Cardinality::Required {
                    return Err(Error::MissingField(format!(
                        "{} (field {number}) in message '{}'",
                        def.name, msg_def.name
                    )));
                }
                // Optional/repeated missing: emit nothing.
            }
            Field::Present(value) => {
                if def.cardinality == Cardinality::Repeated {
                    return Err(Error::TypeMismatch(format!(
                        "field '{}' is repeated but was given a single value",
                        def.name
                    )));
                }
                w.tag(def.number, wire_for(&def.ty))?;
                write_value(schema, def, value, w)?;
            }
            Field::Repeated(values) => {
                if def.cardinality != Cardinality::Repeated {
                    return Err(Error::TypeMismatch(format!(
                        "field '{}' is singular but was given a list",
                        def.name
                    )));
                }
                write_repeated(schema, def, values, w)?;
            }
        }
    }

    // Required presence even when the key is absent from the value map.
    for (number, def) in &msg_def.fields {
        if def.cardinality == Cardinality::Required {
            match message.fields.get(number) {
                Some(Field::Present(_)) => {}
                Some(Field::Repeated(_)) => {
                    return Err(Error::TypeMismatch(format!(
                        "required field '{}' supplied as repeated",
                        def.name
                    )));
                }
                _ => {
                    return Err(Error::MissingField(format!(
                        "{} (field {number}) in message '{}'",
                        def.name, msg_def.name
                    )));
                }
            }
        }
    }

    // Unknown fields forwarded verbatim, in captured order.
    for raw in &message.unknown {
        let mut tag = Vec::with_capacity(5);
        crate::wire::write_varint(&mut tag, crate::wire::encode_tag(raw.field, raw.wire_type));
        w.raw(&tag)?;
        w.raw(&raw.payload)?;
    }
    Ok(())
}

fn wire_for(ty: &TypeDef) -> WireType {
    match ty {
        TypeDef::Scalar(s) => s.wire_type(),
        TypeDef::Message(_) => WireType::Len,
    }
}

fn is_packable(ty: &TypeDef) -> bool {
    matches!(
        ty,
        TypeDef::Scalar(
            ScalarType::Int32
                | ScalarType::Int64
                | ScalarType::Uint32
                | ScalarType::Uint64
                | ScalarType::Bool
                | ScalarType::Float
                | ScalarType::Double
        )
    )
}

fn write_repeated<W: Write>(
    schema: &Schema,
    def: &FieldDef,
    values: &[Value],
    w: &mut WireWriter<W>,
) -> Result<()> {
    if def.packed && is_packable(&def.ty) {
        let s = scalar_of(&def.ty)?;
        // Packed: one tag + one LEN of tagless primitives.
        let mut packed = Vec::new();
        {
            let mut inner = WireWriter::new(&mut packed);
            for v in values {
                write_scalar(s, v, &mut inner)?;
            }
        }
        w.tag(def.number, WireType::Len)?;
        w.len_bytes(&packed)?;
    } else {
        // Unpacked: one tag per occurrence (also the form used for
        // string/bytes/message repetitions).
        for v in values {
            w.tag(def.number, wire_for(&def.ty))?;
            write_value(schema, def, v, w)?;
        }
    }
    Ok(())
}

fn scalar_of(ty: &TypeDef) -> Result<ScalarType> {
    match ty {
        TypeDef::Scalar(s) => Ok(*s),
        TypeDef::Message(n) => Err(Error::Schema(format!(
            "expected scalar, found message '{n}'"
        ))),
    }
}

fn write_value<W: Write>(
    schema: &Schema,
    def: &FieldDef,
    value: &Value,
    w: &mut WireWriter<W>,
) -> Result<()> {
    match &def.ty {
        TypeDef::Scalar(s) => write_scalar(*s, value, w),
        TypeDef::Message(name) => {
            let nested_def = schema
                .message(name)
                .ok_or_else(|| Error::Schema(format!("unknown message type '{name}'")))?;
            let nested = match value {
                Value::Message(m) => m,
                other => {
                    return Err(Error::TypeMismatch(format!(
                        "field '{}' expects message '{name}', got {}",
                        def.name,
                        kind(other)
                    )));
                }
            };
            let mut payload = Vec::new();
            encode_message(
                schema,
                nested_def,
                nested,
                &mut payload,
                EncodeLimit::default(),
            )?;
            w.len_bytes(&payload)
        }
    }
}

fn write_scalar<W: Write>(s: ScalarType, value: &Value, w: &mut WireWriter<W>) -> Result<()> {
    match (s, value) {
        (ScalarType::Int32, Value::Int(n)) => {
            let v = i32::try_from(*n)
                .map_err(|_| Error::TypeMismatch(format!("int32 out of range: {n}")))?;
            w.varint(zigzag_encode32(v) as u64)
        }
        (ScalarType::Int64, Value::Int(n)) => w.varint(zigzag_encode64(*n)),
        (ScalarType::Uint32, Value::Uint(n)) => {
            let v = u32::try_from(*n)
                .map_err(|_| Error::TypeMismatch(format!("uint32 out of range: {n}")))?;
            w.varint(v as u64)
        }
        (ScalarType::Uint64, Value::Uint(n)) => w.varint(*n),
        (ScalarType::Bool, Value::Bool(b)) => w.varint(u64::from(*b)),
        (ScalarType::Float, Value::Float(f)) => w.fixed32(f.to_le_bytes()),
        (ScalarType::Double, Value::Double(d)) => w.fixed64(d.to_le_bytes()),
        (ScalarType::String, Value::Str(t)) => {
            std::str::from_utf8(t.as_bytes())
                .map_err(|_| Error::MalformedValue("string field is not valid UTF-8".into()))?;
            w.len_bytes(t.as_bytes())
        }
        (ScalarType::Bytes, Value::Bytes(b)) => w.len_bytes(b),
        (s, v) => Err(Error::TypeMismatch(format!(
            "{} cannot hold {}",
            s.as_name(),
            kind(v)
        ))),
    }
}

fn kind(v: &Value) -> &'static str {
    match v {
        Value::Int(_) => "int",
        Value::Uint(_) => "uint",
        Value::Float(_) => "float",
        Value::Double(_) => "double",
        Value::Bool(_) => "bool",
        Value::Str(_) => "string",
        Value::Bytes(_) => "bytes",
        Value::Message(_) => "message",
    }
}

// ---------------------------------------------------------------------------
// Decoder side
// ---------------------------------------------------------------------------

/// Statistics from one decode, surfaced in the JSON response and tests.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DecodeStats {
    pub fields_seen: usize,
    pub unknown_fields: usize,
    pub unknown_bytes: usize,
}

/// Output of a decode.
#[derive(Debug, Clone)]
pub struct Decoded {
    pub message: Message,
    pub stats: DecodeStats,
}

/// Decode the schema's root message from a BSE1 envelope.
///
/// The envelope payload is streamed lazily (bounded to the declared length
/// and the output budget), not buffered whole.
pub fn decode_envelope(schema: &Schema, src: &mut dyn Read, limits: Limits) -> Result<Decoded> {
    let mut reader = WireReader::from_envelope(src, limits)?;
    decode_root(schema, schema.root_message(), &mut reader)
}

/// Decode one message from `src` until EOF.
pub fn decode_message(
    schema: &Schema,
    msg_def: &MessageDef,
    src: &mut dyn Read,
    limits: Limits,
) -> Result<Decoded> {
    let mut reader = WireReader::new(src, limits);
    decode_root(schema, msg_def, &mut reader)
}

fn decode_root(
    schema: &Schema,
    msg_def: &MessageDef,
    reader: &mut WireReader<'_>,
) -> Result<Decoded> {
    let mut message = Message::new();
    let mut stats = DecodeStats::default();
    decode_fields(schema, msg_def, reader, &mut message, &mut stats)?;
    check_required(msg_def, &message)?;
    Ok(Decoded { message, stats })
}

fn check_required(msg_def: &MessageDef, message: &Message) -> Result<()> {
    for (number, def) in &msg_def.fields {
        if def.cardinality == Cardinality::Required {
            match message.fields.get(number) {
                Some(Field::Present(_)) => {}
                _ => {
                    return Err(Error::MissingField(format!(
                        "{} (field {number}) in message '{}'",
                        def.name, msg_def.name
                    )));
                }
            }
        }
    }
    Ok(())
}

fn decode_fields(
    schema: &Schema,
    msg_def: &MessageDef,
    reader: &mut WireReader<'_>,
    message: &mut Message,
    stats: &mut DecodeStats,
) -> Result<()> {
    while let Some(tag) = reader.next_tag()? {
        stats.fields_seen += 1;
        match msg_def.field(tag.field) {
            Some(def) => decode_known(schema, def, tag.wire_type, reader, message, stats)?,
            None => {
                let payload = reader.skip_value(tag.wire_type)?;
                stats.unknown_fields += 1;
                stats.unknown_bytes += payload.len();
                message.unknown.push(RawField {
                    field: tag.field,
                    wire_type: tag.wire_type,
                    payload,
                });
            }
        }
    }
    Ok(())
}

fn decode_known(
    schema: &Schema,
    def: &FieldDef,
    wire_type: WireType,
    reader: &mut WireReader<'_>,
    message: &mut Message,
    stats: &mut DecodeStats,
) -> Result<()> {
    if def.cardinality == Cardinality::Repeated {
        // Packed form: one LEN holding tagless primitives.
        if wire_type == WireType::Len && is_packable(&def.ty) {
            let raw = reader.read_len()?;
            let values = parse_packed(scalar_of(&def.ty)?, raw, reader.limits())?;
            for v in values {
                message.push(def.number, v);
            }
            return Ok(());
        }
        reject_mismatch(def, wire_type)?;
        let value = read_value(schema, def, reader, stats)?;
        message.push(def.number, value);
        return Ok(());
    }

    reject_mismatch(def, wire_type)?;
    let value = read_value(schema, def, reader, stats)?;
    // Duplicate occurrence of a singular field: last value wins, but the
    // field remains Present (never collapses back to Missing).
    message.fields.insert(def.number, Field::Present(value));
    Ok(())
}

fn reject_mismatch(def: &FieldDef, got: WireType) -> Result<()> {
    let expected = wire_for(&def.ty);
    if got != expected {
        return Err(Error::IncompatibleEvolution {
            field: def.number,
            reason: format!(
                "field '{}' ({}) expects wire type {expected:?} but data carries {got:?}; \
                 the field's type changed incompatibly",
                def.name,
                type_name(&def.ty)
            ),
        });
    }
    Ok(())
}

fn type_name(ty: &TypeDef) -> String {
    match ty {
        TypeDef::Scalar(s) => s.as_name().to_string(),
        TypeDef::Message(n) => format!("message {n}"),
    }
}

fn read_value(
    schema: &Schema,
    def: &FieldDef,
    reader: &mut WireReader<'_>,
    stats: &mut DecodeStats,
) -> Result<Value> {
    match &def.ty {
        TypeDef::Scalar(s) => read_scalar(*s, reader),
        TypeDef::Message(name) => {
            let nested_def = schema
                .message(name)
                .ok_or_else(|| Error::Schema(format!("unknown message type '{name}'")))?
                .clone();
            let mut sub = reader.read_message()?;
            let mut nested = Message::new();
            decode_fields(schema, &nested_def, &mut sub, &mut nested, stats)?;
            check_required(&nested_def, &nested)?;
            Ok(Value::Message(nested))
        }
    }
}

fn read_scalar(s: ScalarType, reader: &mut WireReader<'_>) -> Result<Value> {
    match s {
        ScalarType::Int32 => {
            let u = reader.read_varint()?;
            if u > u32::MAX as u64 {
                return Err(Error::IncompatibleEvolution {
                    field: 0,
                    reason: format!("varint {u} does not fit int32"),
                });
            }
            Ok(Value::Int(i64::from(zigzag_decode32(u as u32))))
        }
        ScalarType::Int64 => Ok(Value::Int(zigzag_decode64(reader.read_varint()?))),
        ScalarType::Uint32 => {
            let u = reader.read_varint()?;
            u32::try_from(u)
                .map(|_| Value::Uint(u))
                .map_err(|_| Error::IncompatibleEvolution {
                    field: 0,
                    reason: format!("varint {u} does not fit uint32"),
                })
        }
        ScalarType::Uint64 => Ok(Value::Uint(reader.read_varint()?)),
        ScalarType::Bool => match reader.read_varint()? {
            0 => Ok(Value::Bool(false)),
            1 => Ok(Value::Bool(true)),
            other => Err(Error::MalformedValue(format!(
                "bool must be 0 or 1, got {other}"
            ))),
        },
        ScalarType::Float => Ok(Value::Float(f32::from_le_bytes(reader.read_fixed32()?))),
        ScalarType::Double => Ok(Value::Double(f64::from_le_bytes(reader.read_fixed64()?))),
        ScalarType::String => {
            let bytes = reader.read_len()?;
            String::from_utf8(bytes)
                .map(Value::Str)
                .map_err(|_| Error::MalformedValue("string field is not valid UTF-8".into()))
        }
        ScalarType::Bytes => Ok(Value::Bytes(reader.read_len()?)),
    }
}

fn parse_packed(s: ScalarType, raw: Vec<u8>, limits: Limits) -> Result<Vec<Value>> {
    let mut sub = WireReader::buffered(raw, limits, 0);
    let mut out = Vec::new();
    while !sub.at_end() {
        out.push(read_packed_scalar(s, &mut sub)?);
    }
    Ok(out)
}

fn read_packed_scalar(s: ScalarType, reader: &mut WireReader<'_>) -> Result<Value> {
    match s {
        ScalarType::Int32 => {
            let u = reader.read_varint()?;
            if u > u32::MAX as u64 {
                return Err(Error::IncompatibleEvolution {
                    field: 0,
                    reason: format!("packed varint {u} does not fit int32"),
                });
            }
            Ok(Value::Int(i64::from(zigzag_decode32(u as u32))))
        }
        ScalarType::Int64 => Ok(Value::Int(zigzag_decode64(reader.read_varint()?))),
        ScalarType::Uint32 => {
            let u = reader.read_varint()?;
            if u > u32::MAX as u64 {
                return Err(Error::IncompatibleEvolution {
                    field: 0,
                    reason: format!("packed varint {u} does not fit uint32"),
                });
            }
            Ok(Value::Uint(u))
        }
        ScalarType::Uint64 => Ok(Value::Uint(reader.read_varint()?)),
        ScalarType::Bool => match reader.read_varint()? {
            0 => Ok(Value::Bool(false)),
            1 => Ok(Value::Bool(true)),
            other => Err(Error::MalformedValue(format!(
                "packed bool must be 0 or 1, got {other}"
            ))),
        },
        ScalarType::Float => Ok(Value::Float(f32::from_le_bytes(reader.read_fixed32()?))),
        ScalarType::Double => Ok(Value::Double(f64::from_le_bytes(reader.read_fixed64()?))),
        other => Err(Error::Schema(format!("{other:?} cannot be packed"))),
    }
}
