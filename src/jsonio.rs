//! Conversion between JSON documents and the crate's [`Schema`] /
//! [`Message`] / [`Value`] types.
//!
//! ## Schema JSON
//!
//! ```json
//! {
//!   "root": "Person",
//!   "types": [
//!     { "name": "Person", "fields": [
//!       { "name": "id",    "number": 1, "type": "int64",   "cardinality": "required" },
//!       { "name": "email", "number": 3, "type": "string",  "cardinality": "optional" },
//!       { "name": "tags",  "number": 4, "type": "string",  "cardinality": "repeated" },
//!       { "name": "scores","number": 6, "type": "int32",   "cardinality": "repeated", "packed": true }
//!     ]}
//!   ]
//! }
//! ```
//!
//! ## Message JSON (input shorthand)
//!
//! Keys are field **names**. Presence rules:
//!
//! * a key that is omitted, or present as JSON `null`, means the field is
//!   **absent** (`missing`);
//! * a present scalar/object means one explicit value — `{"id": 0}` is an
//!   explicit zero and is never confused with an omitted `id`;
//! * a present array means a `repeated` field.
//!
//! Explicit wrappers are also accepted (and are the only form used in
//! output, so absence can never be mistaken for an explicit zero):
//!
//! ```json
//! { "id": { "present": 0 }, "email": { "missing": true },
//!   "tags": { "values": ["x"] } }
//! ```
//!
//! `bytes` values use standard base64 strings on the way in and out.

use std::collections::BTreeMap;

use crate::error::{Error, Result};
use crate::json::{base64_decode, base64_encode, JsonObject, JsonValue};
use crate::schema::{Cardinality, FieldDef, MessageDef, ScalarType, Schema, TypeDef};
use crate::value::{Field, Message, Value};
use crate::wire::{Limits, RawField, WireType};

/// Parse a schema document.
pub fn schema_from_json(doc: &JsonValue) -> Result<Schema> {
    let obj = as_object(doc, "schema")?;
    let root = obj
        .get("root")
        .and_then(JsonValue::as_str)
        .ok_or_else(|| Error::Schema("schema.root must be a string".into()))?;
    let types = obj
        .get("types")
        .and_then(JsonValue::as_array)
        .ok_or_else(|| Error::Schema("schema.types must be an array".into()))?;

    let mut messages = BTreeMap::new();
    for entry in types {
        let tobj = as_object(entry, "schema.types[]")?;
        let name = tobj
            .get("name")
            .and_then(JsonValue::as_str)
            .ok_or_else(|| Error::Schema("each type needs a string 'name'".into()))?;
        let fields_arr = tobj
            .get("fields")
            .and_then(JsonValue::as_array)
            .ok_or_else(|| Error::Schema(format!("type '{name}' needs a 'fields' array")))?;

        let mut fields = BTreeMap::new();
        for f in fields_arr {
            let fobj = as_object(f, "field")?;
            let fname = fobj
                .get("name")
                .and_then(JsonValue::as_str)
                .ok_or_else(|| Error::Schema("each field needs a string 'name'".into()))?;
            let number = fobj
                .get("number")
                .and_then(JsonValue::as_int)
                .and_then(|n| u32::try_from(n).ok())
                .ok_or_else(|| {
                    Error::Schema(format!("field '{fname}' needs an integer 'number'"))
                })?;
            let type_name = fobj
                .get("type")
                .and_then(JsonValue::as_str)
                .ok_or_else(|| Error::Schema(format!("field '{fname}' needs a string 'type'")))?;
            let cardinality = match fobj.get("cardinality") {
                Some(JsonValue::Str(s)) => Cardinality::from_name(s)?,
                None => Cardinality::Optional,
                Some(_) => {
                    return Err(Error::Schema(format!(
                        "field '{fname}' cardinality must be a string"
                    )));
                }
            };
            let ty = type_from_name(type_name);
            let mut def = FieldDef::new(fname, number, ty, cardinality);
            if let Some(JsonValue::Bool(p)) = fobj.get("packed") {
                def.packed = *p;
            }
            if fields.insert(number, def).is_some() {
                return Err(Error::Schema(format!(
                    "duplicate field number {number} in type '{name}'"
                )));
            }
        }
        if messages
            .insert(
                name.to_string(),
                MessageDef {
                    name: name.to_string(),
                    fields,
                },
            )
            .is_some()
        {
            return Err(Error::Schema(format!("duplicate type name '{name}'")));
        }
    }

    Schema::new(messages, root)
}

fn type_from_name(name: &str) -> TypeDef {
    match ScalarType::from_name(name) {
        Ok(s) => TypeDef::Scalar(s),
        Err(_) => TypeDef::Message(name.to_string()),
    }
}

fn as_object<'a>(v: &'a JsonValue, what: &str) -> Result<&'a JsonObject> {
    v.as_object()
        .ok_or_else(|| Error::Schema(format!("{what} must be a JSON object")))
}

/// Parse a message document against a schema's root message.
pub fn message_from_json(schema: &Schema, doc: &JsonValue) -> Result<Message> {
    let root_name = schema.root.clone();
    let root_def = schema.root_message();
    let obj = doc
        .as_object()
        .ok_or_else(|| Error::TypeMismatch("message must be a JSON object".into()))?;
    // Allow either a bare field map or {"fields": {...}}.
    let fields_obj = match obj.get("fields") {
        Some(JsonValue::Object(f)) => f,
        Some(_) => {
            return Err(Error::TypeMismatch("'fields' must be an object".into()));
        }
        None => obj,
    };
    parse_message_fields(schema, &root_name, root_def, fields_obj)
}

fn parse_message_fields(
    schema: &Schema,
    msg_name: &str,
    def: &MessageDef,
    obj: &JsonObject,
) -> Result<Message> {
    let mut message = Message::new();

    // Index schema fields by name for keyed access.
    let by_name: BTreeMap<&str, &FieldDef> =
        def.fields.values().map(|f| (f.name.as_str(), f)).collect();

    for (key, json_value) in obj.iter() {
        let field_def = by_name.get(key.as_str()).ok_or_else(|| {
            Error::InvalidInput(format!(
                "message for '{msg_name}' contains unknown field name '{key}'"
            ))
        })?;
        let field = parse_field(schema, field_def, json_value)?;
        message.fields.insert(field_def.number, field);
    }
    Ok(message)
}

fn parse_field(schema: &Schema, def: &FieldDef, json_value: &JsonValue) -> Result<Field> {
    // Explicit wrapper?
    if let JsonValue::Object(wrapper) = json_value {
        if let Some(v) = wrapper.get("present") {
            if def.cardinality == Cardinality::Repeated {
                return Err(Error::TypeMismatch(format!(
                    "field '{}' is repeated; use a \"values\" wrapper array",
                    def.name
                )));
            }
            return Ok(Field::Present(parse_value(schema, def, v)?));
        }
        if let Some(JsonValue::Bool(true)) = wrapper.get("missing") {
            if def.cardinality == Cardinality::Required {
                return Err(Error::MissingField(format!(
                    "field '{}' is required but marked missing",
                    def.name
                )));
            }
            return Ok(Field::Missing);
        }
        if let Some(arr) = wrapper.get("values") {
            let items = arr.as_array().ok_or_else(|| {
                Error::TypeMismatch(format!("field '{}' values must be an array", def.name))
            })?;
            if def.cardinality != Cardinality::Repeated {
                return Err(Error::TypeMismatch(format!(
                    "field '{}' is not repeated but given 'values'",
                    def.name
                )));
            }
            let mut values = Vec::with_capacity(items.len());
            for item in items {
                values.push(parse_value(schema, def, item)?);
            }
            return Ok(Field::Repeated(values));
        }
    }

    // Shorthand.
    match json_value {
        JsonValue::Null => {
            if def.cardinality == Cardinality::Required {
                return Err(Error::MissingField(format!(
                    "required field '{}' is null",
                    def.name
                )));
            }
            Ok(Field::Missing)
        }
        JsonValue::Array(items) => {
            if def.cardinality != Cardinality::Repeated {
                return Err(Error::TypeMismatch(format!(
                    "field '{}' is singular but given an array",
                    def.name
                )));
            }
            let mut values = Vec::with_capacity(items.len());
            for item in items {
                values.push(parse_value(schema, def, item)?);
            }
            Ok(Field::Repeated(values))
        }
        other => {
            if def.cardinality == Cardinality::Repeated {
                return Err(Error::TypeMismatch(format!(
                    "field '{}' is repeated but given a single value",
                    def.name
                )));
            }
            Ok(Field::Present(parse_value(schema, def, other)?))
        }
    }
}

fn parse_value(schema: &Schema, def: &FieldDef, v: &JsonValue) -> Result<Value> {
    match &def.ty {
        TypeDef::Scalar(s) => parse_scalar(*s, v),
        TypeDef::Message(name) => {
            let nested_def = schema
                .message(name)
                .ok_or_else(|| Error::Schema(format!("unknown message type '{name}'")))?;
            let obj = v.as_object().ok_or_else(|| {
                Error::TypeMismatch(format!("field '{}' expects an object", def.name))
            })?;
            let fields_obj = match obj.get("fields") {
                Some(JsonValue::Object(f)) => f,
                Some(_) => {
                    return Err(Error::TypeMismatch("'fields' must be an object".into()));
                }
                None => obj,
            };
            Ok(Value::Message(parse_message_fields(
                schema, name, nested_def, fields_obj,
            )?))
        }
    }
}

fn parse_scalar(s: ScalarType, v: &JsonValue) -> Result<Value> {
    match (s, v) {
        (ScalarType::Bool, JsonValue::Bool(b)) => Ok(Value::Bool(*b)),
        (ScalarType::Int32 | ScalarType::Int64, JsonValue::Int(n)) => Ok(Value::Int(*n)),
        (ScalarType::Int32 | ScalarType::Int64, JsonValue::Float(f)) if f.fract() == 0.0 => {
            Ok(Value::Int(*f as i64))
        }
        (ScalarType::Uint32 | ScalarType::Uint64, JsonValue::Int(n)) if *n >= 0 => {
            Ok(Value::Uint(*n as u64))
        }
        // Unsigned literal (possibly above i64::MAX) feeding a uint type.
        (ScalarType::Uint32 | ScalarType::Uint64, JsonValue::UInt(u)) => Ok(Value::Uint(*u)),
        (ScalarType::Uint32 | ScalarType::Uint64, JsonValue::Float(f))
            if f.fract() == 0.0 && *f >= 0.0 =>
        {
            Ok(Value::Uint(*f as u64))
        }
        (ScalarType::Float, JsonValue::Int(n)) => Ok(Value::Float(*n as f32)),
        (ScalarType::Float, JsonValue::UInt(u)) => Ok(Value::Float(*u as f32)),
        (ScalarType::Float, JsonValue::Float(f)) => Ok(Value::Float(*f as f32)),
        (ScalarType::Double, JsonValue::Int(n)) => Ok(Value::Double(*n as f64)),
        (ScalarType::Double, JsonValue::UInt(u)) => Ok(Value::Double(*u as f64)),
        (ScalarType::Double, JsonValue::Float(f)) => Ok(Value::Double(*f)),
        (ScalarType::String, JsonValue::Str(t)) => Ok(Value::Str(t.clone())),
        (ScalarType::Bytes, JsonValue::Str(t)) => Ok(Value::Bytes(base64_decode(t)?)),
        (s, got) => Err(Error::TypeMismatch(format!(
            "{} cannot be built from JSON {}",
            s.as_name(),
            json_type_name(got)
        ))),
    }
}

fn json_type_name(v: &JsonValue) -> &'static str {
    match v {
        JsonValue::Null => "null",
        JsonValue::Bool(_) => "bool",
        JsonValue::Int(_) | JsonValue::UInt(_) | JsonValue::Float(_) => "number",
        JsonValue::Str(_) => "string",
        JsonValue::Array(_) => "array",
        JsonValue::Object(_) => "object",
    }
}

// ---------------------------------------------------------------------------
// Decoded message -> JSON
// ---------------------------------------------------------------------------

/// Serialize a decoded message to an unambiguous JSON object. Every field
/// uses a presence wrapper, so missing fields and explicit zero values are
/// visibly different in the output.
pub fn message_to_json(schema: &Schema, message: &Message) -> JsonValue {
    JsonValue::Object(message_fields_to_json(
        schema,
        schema.root_message(),
        message,
    ))
}

fn message_fields_to_json(schema: &Schema, def: &MessageDef, message: &Message) -> JsonObject {
    let mut out = JsonObject::new();
    for (number, fdef) in &def.fields {
        let wrapper = match message.fields.get(number) {
            // A repeated field is never "missing": absent on the wire means
            // an empty list, rendered explicitly as {"values": []}.
            None | Some(Field::Missing) if fdef.cardinality == Cardinality::Repeated => {
                let mut o = JsonObject::new();
                o.set("values", JsonValue::Array(Vec::new()));
                JsonValue::Object(o)
            }
            Some(Field::Missing) | None => {
                let mut o = JsonObject::new();
                o.set("missing", JsonValue::Bool(true));
                JsonValue::Object(o)
            }
            Some(Field::Present(v)) => {
                let mut o = JsonObject::new();
                o.set("present", value_to_json(schema, fdef, v));
                JsonValue::Object(o)
            }
            Some(Field::Repeated(values)) => {
                let items: Vec<JsonValue> = values
                    .iter()
                    .map(|v| value_to_json(schema, fdef, v))
                    .collect();
                let mut o = JsonObject::new();
                o.set("values", JsonValue::Array(items));
                JsonValue::Object(o)
            }
        };
        out.set(fdef.name.clone(), wrapper);
    }

    // Unknown fields are reported separately so a reader using an older or
    // newer schema can see exactly what was preserved.
    if !message.unknown.is_empty() {
        let unknown: Vec<JsonValue> = message
            .unknown
            .iter()
            .map(|raw| {
                let mut o = JsonObject::new();
                o.set("field", JsonValue::Int(raw.field as i64));
                o.set("wire_type", JsonValue::Int(raw.wire_type as i64));
                o.set("payload_b64", JsonValue::Str(base64_encode(&raw.payload)));
                o.set("payload_hex", JsonValue::Str(hex_encode(&raw.payload)));
                JsonValue::Object(o)
            })
            .collect();
        let mut unknown_obj = JsonObject::new();
        unknown_obj.set("count", JsonValue::Int(unknown.len() as i64));
        unknown_obj.set("fields", JsonValue::Array(unknown));
        out.set("__unknown__", JsonValue::Object(unknown_obj));
    }
    out
}

fn value_to_json(schema: &Schema, def: &FieldDef, v: &Value) -> JsonValue {
    match v {
        Value::Int(n) => JsonValue::Int(*n),
        Value::Uint(n) => JsonValue::UInt(*n),
        Value::Float(f) => ser_float(*f as f64),
        Value::Double(d) => ser_float(*d),
        Value::Bool(b) => JsonValue::Bool(*b),
        Value::Str(s) => JsonValue::Str(s.clone()),
        Value::Bytes(b) => JsonValue::Str(base64_encode(b)),
        Value::Message(m) => match &def.ty {
            TypeDef::Message(name) => match schema.message(name) {
                Some(nested_def) => {
                    JsonValue::Object(message_fields_to_json(schema, nested_def, m))
                }
                None => JsonValue::Null,
            },
            _ => JsonValue::Null,
        },
    }
}

fn ser_float(f: f64) -> JsonValue {
    // Integer-valued and fractional floats both render as JSON numbers;
    // the serializer chooses the shortest round-tripping representation.
    JsonValue::Float(f)
}

/// Decode limits from an optional request object.
pub fn limits_from_json(doc: &JsonObject) -> Limits {
    let mut limits = Limits::default();
    if let Some(JsonValue::Int(n)) = doc.get("max_value_len") {
        if *n >= 0 {
            limits.max_value_len = *n as u64;
        }
    }
    if let Some(JsonValue::Int(n)) = doc.get("max_output") {
        if *n >= 0 {
            limits.max_output = *n as u64;
        }
    }
    if let Some(JsonValue::Int(n)) = doc.get("max_depth") {
        if *n >= 0 {
            limits.max_depth = *n as u32;
        }
    }
    limits
}

/// Extract binary input from either `data_b64` or `data_hex`.
pub fn data_from_request(doc: &JsonObject) -> Result<Vec<u8>> {
    if let Some(JsonValue::Str(s)) = doc.get("data_b64") {
        return base64_decode(s);
    }
    if let Some(JsonValue::Str(s)) = doc.get("data_hex") {
        return hex_decode(s);
    }
    Err(Error::InvalidInput(
        "request must include 'data_b64' or 'data_hex'".into(),
    ))
}

/// Lower-case hex encoding.
pub fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(nibble(b >> 4));
        s.push(nibble(b & 0x0f));
    }
    s
}

fn nibble(n: u8) -> char {
    match n {
        0..=9 => (b'0' + n) as char,
        _ => (b'a' + (n - 10)) as char,
    }
}

/// Hex decoding; whitespace and odd length are rejected.
pub fn hex_decode(s: &str) -> Result<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        return Err(Error::InvalidInput("hex string has odd length".into()));
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    for pair in bytes.chunks(2) {
        let hi = hex_nibble(pair[0])?;
        let lo = hex_nibble(pair[1])?;
        out.push((hi << 4) | lo);
    }
    Ok(out)
}

fn hex_nibble(c: u8) -> Result<u8> {
    match c {
        b'0'..=b'9' => Ok(c - b'0'),
        b'a'..=b'f' => Ok(c - b'a' + 10),
        b'A'..=b'F' => Ok(c - b'A' + 10),
        _ => Err(Error::InvalidInput(format!("invalid hex digit '{c}'"))),
    }
}

/// Render a raw field (used in diagnostic output).
pub fn raw_field_to_json(raw: &RawField) -> JsonValue {
    let wt: i64 = match raw.wire_type {
        WireType::Varint => 0,
        WireType::I64 => 1,
        WireType::Len => 2,
        WireType::I32 => 3,
    };
    let mut o = JsonObject::new();
    o.set("field", JsonValue::Int(raw.field as i64));
    o.set("wire_type", JsonValue::Int(wt));
    o.set("payload_hex", JsonValue::Str(hex_encode(&raw.payload)));
    JsonValue::Object(o)
}
