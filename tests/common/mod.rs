//! Shared builders for integration tests.
#![allow(dead_code)]

use std::collections::BTreeMap;

use bse::schema::{Cardinality, FieldDef, MessageDef, ScalarType, Schema, TypeDef};
use bse::value::{Field, Message, Value};

pub fn scalar(name: &str, number: u32, ty: ScalarType, card: Cardinality) -> FieldDef {
    FieldDef::new(name, number, TypeDef::Scalar(ty), card)
}

pub fn message_field(name: &str, number: u32, ty: &str, card: Cardinality) -> FieldDef {
    FieldDef::new(name, number, TypeDef::Message(ty.to_string()), card)
}

pub fn schema(root: &str, msgs: Vec<(&str, Vec<FieldDef>)>) -> Schema {
    let mut map = BTreeMap::new();
    for (name, fields) in msgs {
        let mut by_number = BTreeMap::new();
        for f in fields {
            by_number.insert(f.number, f);
        }
        map.insert(
            name.to_string(),
            MessageDef {
                name: name.to_string(),
                fields: by_number,
            },
        );
    }
    Schema::new(map, root).expect("valid schema")
}

/// Build a message from (number, field) pairs.
pub fn msg(pairs: Vec<(u32, Field)>) -> Message {
    let mut m = Message::new();
    for (n, f) in pairs {
        m.fields.insert(n, f);
    }
    m
}

/// Local conversion trait (orphan rules forbid foreign `From` impls).
pub trait AsValue {
    fn v(self) -> Value;
}
impl AsValue for Value {
    fn v(self) -> Value {
        self
    }
}
impl AsValue for i64 {
    fn v(self) -> Value {
        Value::Int(self)
    }
}
impl AsValue for i32 {
    fn v(self) -> Value {
        Value::Int(self as i64)
    }
}
impl AsValue for u64 {
    fn v(self) -> Value {
        Value::Uint(self)
    }
}
impl AsValue for bool {
    fn v(self) -> Value {
        Value::Bool(self)
    }
}
impl AsValue for &str {
    fn v(self) -> Value {
        Value::Str(self.to_string())
    }
}
impl AsValue for f64 {
    fn v(self) -> Value {
        Value::Double(self)
    }
}
impl AsValue for f32 {
    fn v(self) -> Value {
        Value::Float(self)
    }
}

pub fn present<T: AsValue>(x: T) -> Field {
    Field::Present(x.v())
}

pub fn repeated(values: Vec<Value>) -> Field {
    Field::Repeated(values)
}

pub fn missing() -> Field {
    Field::Missing
}

/// Byte-string value helper.
pub fn bytes(b: impl Into<Vec<u8>>) -> Value {
    Value::Bytes(b.into())
}

/// Encode then decode under the same schema, returning the decoded message.
pub fn round_trip(schema: &Schema, message: &Message) -> (Message, bse::codec::DecodeStats) {
    let mut buf = Vec::new();
    bse::codec::encode_envelope(schema, message, &mut buf, Default::default()).expect("encode");
    let mut cursor = std::io::Cursor::new(buf);
    let d = bse::codec::decode_envelope(schema, &mut cursor, Default::default()).expect("decode");
    (d.message, d.stats)
}
