//! Schema model: messages, numbered fields, scalar types and repetition
//! rules. A schema is a JSON document (see `examples/` and README).
//!
//! Evolution rules enforced by the codec:
//! - Fields are identified by number; renaming a field is safe.
//! - A field's type tag must not change: reading a field number known to the
//!   reader's schema with a different wire type is rejected as incompatible
//!   (see [`crate::reader`]).
//! - New fields may be added; old fields removed (their bytes are retained as
//!   unknown fields when forwarded).
//! - Cardinality changes are governed by [`Cardinality`] /
//!   [`Repetition`] rules, documented per variant.

use std::collections::HashMap;

use crate::error::{Error, Result};
use crate::json::Json;

/// Scalar value types. Each maps to exactly one wire tag id (see
/// [`crate::wire`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Type {
    /// LEB128 varint, range 0..=2^63-1 (rejected on overflow).
    Int64,
    /// Zig-zag LEB128 varint, 64-bit signed.
    Sint64,
    /// 8-byte big-endian fixed integer, unsigned.
    Fixed64,
    /// LEB128 varint, 32-bit signed range.
    Int32,
    /// Zig-zag varint, 32-bit signed range.
    Sint32,
    /// 4-byte big-endian fixed integer, unsigned 32-bit.
    Fixed32,
    /// Varint 0/1.
    Bool,
    /// Fixed 8-byte IEEE 754 double.
    Double,
    /// LEB128 length + UTF-8 bytes.
    String,
    /// LEB128 length + raw bytes.
    Bytes,
    /// A nested message; wire tag is MESSAGE.
    Message,
}

impl Type {
    pub fn parse(s: &str) -> Result<Type> {
        Ok(match s {
            "int64" => Type::Int64,
            "sint64" => Type::Sint64,
            "fixed64" => Type::Fixed64,
            "int32" => Type::Int32,
            "sint32" => Type::Sint32,
            "fixed32" => Type::Fixed32,
            "bool" => Type::Bool,
            "double" => Type::Double,
            "string" => Type::String,
            "bytes" => Type::Bytes,
            _ => {
                return Err(Error::Schema(format!(
                    "unknown type `{s}` (declared message types are referenced by their name)"
                )))
            }
        })
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Type::Int64 => "int64",
            Type::Sint64 => "sint64",
            Type::Fixed64 => "fixed64",
            Type::Int32 => "int32",
            Type::Sint32 => "sint32",
            Type::Fixed32 => "fixed32",
            Type::Bool => "bool",
            Type::Double => "double",
            Type::String => "string",
            Type::Bytes => "bytes",
            Type::Message => "message",
        }
    }
}

/// Whether a field may be omitted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Presence {
    /// Field may be absent; absence is distinct from an explicit zero value.
    Optional,
    /// Decoding fails if the field is absent (also enforced on encode).
    Required,
}

/// How repeated values are represented on the wire.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Repetition {
    /// No repetition: at most one value.
    Single,
    /// Packed: scalar repeated values are encoded as one length-delimited
    /// region containing concatenated values. Forwarding old unpacked data
    /// works because unknown/known entries of either shape are accepted by
    /// readers (see reader).
    Packed,
    /// Unpacked: each value is emitted as its own tagged field entry.
    Unpacked,
}

/// One declared field.
#[derive(Debug, Clone)]
pub struct Field {
    pub number: u32,
    pub name: String,
    pub ty: Type,
    /// For `ty == Message`, the referenced message name.
    pub references: Option<String>,
    pub presence: Presence,
    pub repetition: Repetition,
}

impl Field {
    pub fn is_repeated(&self) -> bool {
        self.repetition != Repetition::Single
    }
}

/// A message definition.
#[derive(Debug, Clone)]
pub struct Message {
    pub name: String,
    pub fields: Vec<Field>,
    by_number: HashMap<u32, usize>,
}

impl Message {
    fn new(name: String, fields: Vec<Field>) -> Result<Message> {
        let mut by_number = HashMap::new();
        let mut seen_names = HashMap::new();
        for (i, f) in fields.iter().enumerate() {
            if f.number == 0 {
                return Err(Error::Schema(format!(
                    "field number 0 is reserved (message `{name}`, field `{}`)",
                    f.name
                )));
            }
            if by_number.insert(f.number, i).is_some() {
                return Err(Error::Schema(format!(
                    "duplicate field number {} in message `{name}`",
                    f.number
                )));
            }
            if let Some(prev) = seen_names.insert(&f.name, f.number) {
                return Err(Error::Schema(format!(
                    "duplicate field name `{}` (numbers {prev} and {}) in message `{name}`",
                    f.name, f.number
                )));
            }
        }
        Ok(Message {
            name,
            fields,
            by_number,
        })
    }

    pub fn field_by_number(&self, n: u32) -> Option<&Field> {
        self.by_number.get(&n).map(|&i| &self.fields[i])
    }

    pub fn field_by_name(&self, name: &str) -> Option<&Field> {
        self.fields.iter().find(|f| f.name == name)
    }
}

/// A complete, validated schema: a set of named messages plus the name of the
/// root/top-level message.
#[derive(Debug, Clone)]
pub struct Schema {
    pub root: String,
    messages: HashMap<String, Message>,
    /// Stable declaration order (for diagnostics and JSON schema emission).
    order: Vec<String>,
}

impl Schema {
    /// Parse a schema JSON document of the form:
    /// ```json
    /// { "root": "Person", "messages": [ { "name": "Person", "fields": [ ... ] } ] }
    /// ```
    pub fn from_json(doc: &Json) -> Result<Schema> {
        let root = doc
            .get("root")
            .and_then(|v| v.as_str())
            .ok_or_else(|| Error::Schema("missing string `root`".into()))?
            .to_string();

        let msgs = doc
            .get("messages")
            .and_then(|v| v.as_array())
            .ok_or_else(|| Error::Schema("missing array `messages`".into()))?;

        let mut order = Vec::new();
        let mut messages = HashMap::new();
        for m in msgs {
            let name = m
                .get("name")
                .and_then(|v| v.as_str())
                .ok_or_else(|| Error::Schema("each message needs a string `name`".into()))?
                .to_string();
            let fields_json = m
                .get("fields")
                .and_then(|v| v.as_array())
                .ok_or_else(|| Error::Schema(format!("message `{name}` needs array `fields`")))?;

            let mut fields = Vec::new();
            for f in fields_json {
                fields.push(parse_field(f)?);
            }
            if order.contains(&name) {
                return Err(Error::Schema(format!("duplicate message name `{name}`")));
            }
            order.push(name.clone());
            messages.insert(name.clone(), Message::new(name.clone(), fields)?);
        }

        if !messages.contains_key(&root) {
            return Err(Error::Schema(format!(
                "root message `{root}` is not defined in `messages`"
            )));
        }

        // Validate message references.
        for m in messages.values() {
            for f in &m.fields {
                if f.ty == Type::Message {
                    let target = f.references.as_deref().ok_or_else(|| {
                        Error::Schema(format!(
                            "field `{}.{}` of message type needs `references`",
                            m.name, f.name
                        ))
                    })?;
                    if !messages.contains_key(target) {
                        return Err(Error::Schema(format!(
                            "field `{}.{}` references unknown message `{target}`",
                            m.name, f.name
                        )));
                    }
                }
            }
        }

        Ok(Schema {
            root,
            messages,
            order,
        })
    }

    pub fn root_message(&self) -> &Message {
        &self.messages[&self.root]
    }

    pub fn message(&self, name: &str) -> Option<&Message> {
        self.messages.get(name)
    }

    pub fn message_names(&self) -> &[String] {
        &self.order
    }
}

fn parse_field(f: &Json) -> Result<Field> {
    let number = f
        .get("number")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| Error::Schema("each field needs integer `number`".into()))?;
    if number > u32::MAX as u64 {
        return Err(Error::Schema(format!("field number {number} > 2^32-1")));
    }
    let name = f
        .get("name")
        .and_then(|v| v.as_str())
        .ok_or_else(|| Error::Schema(format!("field {number} needs string `name`")))?
        .to_string();
    let type_str = f
        .get("type")
        .and_then(|v| v.as_str())
        .ok_or_else(|| Error::Schema(format!("field `{name}` needs string `type`")))?;

    // `type` may be a declared message name.
    let (ty, references) = match Type::parse(type_str) {
        Ok(t) => (t, None),
        Err(_) => (Type::Message, Some(type_str.to_string())),
    };

    let references = match f.get("references") {
        Some(Json::Str(s)) => {
            if ty != Type::Message {
                return Err(Error::Schema(format!(
                    "field `{name}`: `references` only valid on message-typed fields"
                )));
            }
            Some(s.clone())
        }
        None => references,
        Some(_) => {
            return Err(Error::Schema(format!(
                "field `{name}`: `references` must be a string"
            )))
        }
    };

    let presence = match f.get("required") {
        Some(Json::Bool(true)) => Presence::Required,
        Some(Json::Bool(false)) | None => Presence::Optional,
        Some(_) => {
            return Err(Error::Schema(format!(
                "field `{name}`: `required` must be bool"
            )))
        }
    };

    let repetition = match f.get("repeated") {
        None | Some(Json::Bool(false)) => Repetition::Single,
        Some(Json::Bool(true)) => Repetition::Unpacked,
        Some(Json::Str(s)) if s == "unpacked" => Repetition::Unpacked,
        Some(Json::Str(s)) if s == "packed" => {
            if matches!(ty, Type::Message) {
                return Err(Error::Schema(format!(
                    "field `{name}`: packed repetition is only valid for scalar types"
                )));
            }
            Repetition::Packed
        }
        Some(other) => {
            return Err(Error::Schema(format!(
            "field `{name}`: `repeated` must be true/false/\"packed\"/\"unpacked\", got {other:?}"
        )))
        }
    };

    if presence == Presence::Required && repetition != Repetition::Single {
        return Err(Error::Schema(format!(
            "field `{name}`: required repeated fields are not allowed (use optional repeated)"
        )));
    }

    Ok(Field {
        number: number as u32,
        name,
        ty,
        references,
        presence,
        repetition,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::parse;

    #[test]
    fn parses_valid_schema() {
        let doc = parse(
            r#"{"root":"P","messages":[{"name":"P","fields":[
                {"number":1,"name":"id","type":"int32","required":true},
                {"number":2,"name":"tags","type":"string","repeated":"packed"}]}]}"#,
        )
        .unwrap();
        let s = Schema::from_json(&doc).unwrap();
        assert_eq!(s.root_message().fields.len(), 2);
    }

    #[test]
    fn rejects_bad_schemas() {
        let base = r#"{"root":"P","messages":[{"name":"P","fields":[FIELDS]}]}"#;
        let dup_num = base.replace(
            "FIELDS",
            r#"{"number":1,"name":"a","type":"int32"},{"number":1,"name":"b","type":"int32"}"#,
        );
        assert!(Schema::from_json(&parse(&dup_num).unwrap()).is_err());
        let zero = base.replace("FIELDS", r#"{"number":0,"name":"a","type":"int32"}"#);
        assert!(Schema::from_json(&parse(&zero).unwrap()).is_err());
        let bad_ref = base.replace("FIELDS", r#"{"number":1,"name":"a","type":"Missing"}"#);
        assert!(Schema::from_json(&parse(&bad_ref).unwrap()).is_err());
    }
}
