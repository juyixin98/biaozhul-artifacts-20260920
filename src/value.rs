//! Dynamic message values used by the JSON control surface.
//!
//! The library is schema-driven rather than derive-based: a request
//! supplies a schema and a [`Message`] of plain JSON values, and the codec
//! validates each value against the schema while encoding. A
//! [`Field::Present`] always carries an explicit value, so a JSON `null`
//! (or an omitted key) is distinguishable from an explicit `0`/`""`/
//! `false`.

use std::collections::BTreeMap;

use crate::wire::RawField;

/// One field value in a decoded/encoded message.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Int(i64),
    Uint(u64),
    Float(f32),
    Double(f64),
    Bool(bool),
    Str(String),
    Bytes(Vec<u8>),
    Message(Message),
}

/// A field's presence. Repeated fields use [`Field::Repeated`]; singular
/// fields use [`Field::Missing`] / [`Field::Present`].
#[derive(Debug, Clone, PartialEq)]
pub enum Field {
    /// The field is absent from the message. This is **not** the same as
    /// being present with a zero value.
    Missing,
    /// The field carries one explicitly supplied value.
    Present(Value),
    /// The field carries zero or more values in wire order.
    Repeated(Vec<Value>),
}

impl Field {
    /// True when the field carries no value at all (not even an explicit
    /// zero). An empty `repeated` is considered present-but-empty.
    pub fn is_missing(&self) -> bool {
        matches!(self, Field::Missing)
    }
}

/// A message value: field number -> field, plus unknown fields captured
/// verbatim from a prior decode so they survive re-encoding (forwarding).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Message {
    pub fields: BTreeMap<u32, Field>,
    /// Fields the active schema did not know about, in wire order. These
    /// are re-emitted byte-for-byte on encode.
    pub unknown: Vec<RawField>,
}

impl Message {
    pub fn new() -> Message {
        Message {
            fields: BTreeMap::new(),
            unknown: Vec::new(),
        }
    }

    /// Record a singular value, replacing any previous one.
    pub fn put(&mut self, number: u32, value: Value) {
        self.fields.insert(number, Field::Present(value));
    }

    /// Append one occurrence to a repeated field.
    pub fn push(&mut self, number: u32, value: Value) {
        match self.fields.entry(number) {
            std::collections::btree_map::Entry::Occupied(mut e) => match e.get_mut() {
                Field::Repeated(v) => v.push(value),
                Field::Present(_) => {
                    let prev = std::mem::replace(e.get_mut(), Field::Repeated(vec![value]));
                    if let Field::Present(first) = prev {
                        if let Field::Repeated(v) = e.into_mut() {
                            v.insert(0, first);
                        }
                    }
                }
                Field::Missing => {
                    e.insert(Field::Repeated(vec![value]));
                }
            },
            std::collections::btree_map::Entry::Vacant(e) => {
                e.insert(Field::Repeated(vec![value]));
            }
        }
    }

    pub fn get(&self, number: u32) -> Option<&Field> {
        self.fields.get(&number)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_differs_from_explicit_zero() {
        assert_ne!(Field::Missing, Field::Present(Value::Int(0)));
        assert_ne!(Field::Missing, Field::Present(Value::Bool(false)));
        assert_ne!(Field::Missing, Field::Present(Value::Str(String::new())));
    }

    #[test]
    fn repeated_preserves_order() {
        let mut m = Message::new();
        m.push(1, Value::Int(10));
        m.push(1, Value::Int(20));
        match m.get(1) {
            Some(Field::Repeated(v)) => {
                assert_eq!(v.len(), 2);
                assert!(matches!(v[0], Value::Int(10)));
                assert!(matches!(v[1], Value::Int(20)));
            }
            other => panic!("expected repeated, got {other:?}"),
        }
    }
}
