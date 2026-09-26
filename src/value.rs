//! In-memory values produced by the decoder and accepted by the encoder.
//!
//! Presence is modeled explicitly with [`Option`]: `None` means *absent*
//! while `Some(Value::Int32(0))` means *present with an explicit zero*.
//! These two cases have different wire encodings (omitted field vs. a field
//! entry carrying zero) and are kept distinct end-to-end.

use std::collections::HashMap;

use crate::base64;
use crate::error::{Error, Result};
use crate::ieee;
use crate::json::Json;
use crate::schema::{Field, Message, Schema, Type};
use crate::wire::TagId;

/// A scalar or nested-message value.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    /// Signed varint (two's-complement u64 on the wire).
    Int64(i64),
    /// Zig-zag signed 64-bit.
    Sint64(i64),
    /// Unsigned fixed 64-bit.
    Fixed64(u64),
    /// Signed 32-bit varint (u32 on the wire, two's complement).
    Int32(i32),
    /// Zig-zag signed 32-bit.
    Sint32(i32),
    /// Unsigned fixed 32-bit.
    Fixed32(u32),
    Bool(bool),
    Double(f64),
    Str(String),
    Bytes(Vec<u8>),
    Msg(MessageValue),
}

impl Value {
    /// The wire tag id this value encodes as.
    pub fn tag_id(&self) -> TagId {
        match self {
            Value::Int64(_) | Value::Int32(_) => TagId::Varint,
            Value::Sint64(_) | Value::Sint32(_) => TagId::Zigzag,
            Value::Bool(_) => TagId::Bool,
            Value::Fixed64(_) => TagId::Fixed64,
            Value::Fixed32(_) => TagId::Fixed32,
            Value::Double(_) => TagId::Double,
            Value::Str(_) => TagId::String,
            Value::Bytes(_) => TagId::Bytes,
            Value::Msg(_) => TagId::Message,
        }
    }
}

/// One field's decoded content.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum FieldValue {
    #[default]
    Absent,
    /// Single-valued field that is present (carries an explicit value,
    /// including explicit zero / empty string).
    Present(Value),
    /// Repeated field, in wire order.
    Repeated(Vec<Value>),
}

/// Raw bytes of a field the reader's schema does not know.
///
/// `payload` holds the bytes following the tag, exactly as received:
/// varint bytes for varint fields, 8/4 bytes for fixed fields, or the raw
/// length-delimited region (without its length prefix). This lets the writer
/// re-emit unknown fields byte-for-byte.
#[derive(Debug, Clone, PartialEq)]
pub struct UnknownField {
    pub number: u32,
    pub tag: TagId,
    pub payload: Vec<u8>,
}

/// A decoded message: known fields by number plus retained unknown fields.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MessageValue {
    pub fields: HashMap<u32, FieldValue>,
    pub unknown: Vec<UnknownField>,
}

impl MessageValue {
    pub fn new() -> MessageValue {
        MessageValue::default()
    }

    pub fn put(&mut self, n: u32, v: Value) {
        self.fields.insert(n, FieldValue::Present(v));
    }

    pub fn push_repeated(&mut self, n: u32, v: Value) {
        match self
            .fields
            .entry(n)
            .or_insert(FieldValue::Repeated(Vec::new()))
        {
            FieldValue::Repeated(vals) => vals.push(v),
            // Field previously seen as single; normal decoding rejects this,
            // but programmatic construction is funneled through put().
            other => *other = FieldValue::Repeated(vec![take_single(other), v]),
        }
    }

    pub fn get(&self, n: u32) -> Option<&Value> {
        match self.fields.get(&n) {
            Some(FieldValue::Present(v)) => Some(v),
            _ => None,
        }
    }
}

fn take_single(fv: &mut FieldValue) -> Value {
    match std::mem::take(fv) {
        FieldValue::Present(v) => v,
        _ => unreachable!("caller guarantees Present"),
    }
}

// ---------------------------------------------------------------------------
// JSON -> Value (encode input)
// ---------------------------------------------------------------------------

/// Convert a JSON object into a root message value according to `schema`.
pub fn json_to_message(schema: &Schema, obj: &Json) -> Result<MessageValue> {
    let root = schema.root_message();
    json_to_message_typed(schema, root, obj)
}

fn json_to_message_typed(schema: &Schema, msg: &Message, obj: &Json) -> Result<MessageValue> {
    let entries = obj.as_object().ok_or_else(|| {
        Error::InvalidValue(format!("message `{}` must be a JSON object", msg.name))
    })?;

    let mut out = MessageValue::new();

    for (key, jv) in entries {
        let field = msg.field_by_name(key).ok_or_else(|| {
            Error::InvalidValue(format!(
                "unknown field `{key}` for message `{}` (JSON encode input must match schema; \
                 to forward unknown fields use the binary path)",
                msg.name
            ))
        })?;

        if field.is_repeated() {
            let arr = match jv {
                Json::Array(a) => a,
                Json::Null => continue,
                _ => {
                    return Err(Error::InvalidValue(format!(
                        "field `{}` is repeated; expected a JSON array",
                        field.name
                    )))
                }
            };
            for item in arr {
                if matches!(item, Json::Null) {
                    return Err(Error::InvalidValue(format!(
                        "null element in repeated field `{}`",
                        field.name
                    )));
                }
                out.push_repeated(field.number, convert_scalar(schema, field, item)?);
            }
        } else {
            if matches!(jv, Json::Null) {
                continue; // explicit null = absent for an optional single field
            }
            let v = convert_scalar(schema, field, jv)?;
            out.put(field.number, v);
        }
    }

    // Required-field validation happens in the writer too, but fail early with
    // a field name here.
    for f in &msg.fields {
        if crate::schema::Presence::Required == f.presence && !out.fields.contains_key(&f.number) {
            return Err(Error::MissingRequired {
                number: f.number,
                name: f.name.clone(),
            });
        }
    }
    Ok(out)
}

fn convert_scalar(schema: &Schema, field: &Field, jv: &Json) -> Result<Value> {
    let mismatch = || {
        Err(Error::InvalidValue(format!(
            "field `{}`: JSON {} is not assignable to {}",
            field.name,
            json_kind(jv),
            field.ty.as_str()
        )))
    };
    Ok(match field.ty {
        Type::Int64 => match jv.as_i64() {
            Some(i) => Value::Int64(i),
            None => return mismatch(),
        },
        Type::Sint64 => match jv.as_i64() {
            Some(i) => Value::Sint64(i),
            None => return mismatch(),
        },
        Type::Fixed64 => match jv.as_u64() {
            Some(u) => Value::Fixed64(u),
            None => return mismatch(),
        },
        Type::Int32 => match jv.as_i64() {
            Some(i) => {
                ieee::check_i32(i)?;
                Value::Int32(i as i32)
            }
            None => return mismatch(),
        },
        Type::Sint32 => match jv.as_i64() {
            Some(i) => {
                ieee::check_i32(i)?;
                Value::Sint32(i as i32)
            }
            None => return mismatch(),
        },
        Type::Fixed32 => match jv.as_u64() {
            Some(u) if u <= u32::MAX as u64 => Value::Fixed32(u as u32),
            _ => return mismatch(),
        },
        Type::Bool => match jv.as_bool() {
            Some(b) => Value::Bool(b),
            None => return mismatch(),
        },
        Type::Double => match jv.as_f64() {
            Some(f) => Value::Double(f),
            None => return mismatch(),
        },
        Type::String => match jv.as_str() {
            Some(s) => Value::Str(s.to_string()),
            None => return mismatch(),
        },
        Type::Bytes => match jv.as_str() {
            Some(s) => Value::Bytes(base64::decode(s)?),
            None => return mismatch(),
        },
        Type::Message => {
            let target = field.references.as_deref().unwrap();
            let sub = schema.message(target).unwrap();
            Value::Msg(json_to_message_typed(schema, sub, jv)?)
        }
    })
}

fn json_kind(jv: &Json) -> &'static str {
    match jv {
        Json::Null => "null",
        Json::Bool(_) => "boolean",
        Json::Int(_) | Json::UInt(_) | Json::Float(_) => "number",
        Json::Str(_) => "string",
        Json::Array(_) => "array",
        Json::Object(_) => "object",
    }
}

// ---------------------------------------------------------------------------
// Value -> JSON (decode output)
// ---------------------------------------------------------------------------

/// Render a decoded message as JSON using field names from `schema`.
/// Unknown fields are included under a separate key so their forwarding is
/// observable and verifiable.
pub fn message_to_json(schema: &Schema, mv: &MessageValue) -> Json {
    message_to_json_typed(schema, schema.root_message(), mv)
}

pub(crate) fn message_to_json_typed(schema: &Schema, msg: &Message, mv: &MessageValue) -> Json {
    let mut pairs: Vec<(String, Json)> = Vec::new();

    // Known fields in schema declaration order.
    for f in &msg.fields {
        match mv.fields.get(&f.number) {
            None | Some(FieldValue::Absent) => continue, // absent: key omitted
            Some(FieldValue::Present(v)) => {
                pairs.push((f.name.clone(), value_to_json(schema, f, v)));
            }
            Some(FieldValue::Repeated(vs)) => {
                pairs.push((
                    f.name.clone(),
                    Json::Array(vs.iter().map(|v| value_to_json(schema, f, v)).collect()),
                ));
            }
        }
    }

    if !mv.unknown.is_empty() {
        let unknown: Vec<Json> = mv
            .unknown
            .iter()
            .map(|u| {
                Json::Object(vec![
                    ("number".to_string(), Json::UInt(u.number as u64)),
                    ("tag".to_string(), Json::Str(u.tag.as_str().to_string())),
                    (
                        "data_base64".to_string(),
                        Json::Str(base64::encode(&u.payload)),
                    ),
                ])
            })
            .collect();
        pairs.push(("__unknown_fields__".to_string(), Json::Array(unknown)));
    }

    Json::Object(pairs)
}

fn value_to_json(schema: &Schema, field: &Field, v: &Value) -> Json {
    match (field.ty, v) {
        (Type::Message, Value::Msg(sub)) => {
            let target = field.references.as_deref().unwrap();
            message_to_json_typed(schema, schema.message(target).unwrap(), sub)
        }
        (_, other) => scalar_to_json(other),
    }
}

fn scalar_to_json(v: &Value) -> Json {
    match v {
        Value::Int64(i) => Json::Int(*i),
        Value::Sint64(i) => Json::Int(*i),
        Value::Fixed64(u) => Json::UInt(*u),
        Value::Int32(i) => Json::Int(*i as i64),
        Value::Sint32(i) => Json::Int(*i as i64),
        Value::Fixed32(u) => Json::UInt(*u as u64),
        Value::Bool(b) => Json::Bool(*b),
        Value::Double(f) => Json::Float(*f),
        Value::Str(s) => Json::Str(s.clone()),
        Value::Bytes(b) => Json::Str(base64::encode(b)),
        Value::Msg(_) => {
            // Nested messages are rendered via value_to_json with field context.
            Json::Null
        }
    }
}
