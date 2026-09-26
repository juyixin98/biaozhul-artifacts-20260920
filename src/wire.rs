//! BSE1 wire tags.
//!
//! Every field entry starts with a single uvarint:
//!
//! ```text
//! tag = (field_number << 4) | type_id
//! ```
//!
//! Unlike formats that collapse everything onto three wire types, BSE1 gives
//! each incompatible scalar family its own 4-bit `type_id`. A reader can
//! therefore *reject* an incompatible type change directly from the tag —
//! `int64`→`sint64`, `int64`→`bool`, `fixed64`→`double`, `string`→`bytes`
//! and `bytes`→`message` are all distinguishable on the wire.
//!
//! ```text
//!  0  VARINT   int32 / int64 payloads (LEB128, non-negative interpretation)
//!  1  ZIGZAG   sint32 / sint64 (zig-zag LEB128)
//!  2  BOOL     LEB128, value restricted to 0/1
//!  3  FIXED64  8 big-endian bytes (unsigned integer)
//!  4  FIXED32  4 big-endian bytes (unsigned integer)
//!  5  DOUBLE   8 big-endian bytes, IEEE 754 binary64
//!  6  BYTES    LEB128 length + raw bytes
//!  7  STRING   LEB128 length + UTF-8 bytes
//!  8  MESSAGE  LEB128 length + nested message body
//!  9  PACKED   LEB128 length + 1 element-type byte (0..=6) + elements
//! ```
//!
//! Compatibility rules:
//! - `int32`→`int64` and `sint32`→`sint64` share a tag and are compatible
//!   widenings; the reverse narrowing is rejected when an out-of-range value
//!   appears.
//! - Every other type change changes the tag and is rejected.
//! - Packed (`9`) regions are accepted only for schema-declared repeated
//!   scalar fields; the embedded element type must match the field.

use crate::error::{Error, Result};
use crate::schema::Type;

/// The 4-bit payload discriminator in a field tag.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TagId {
    Varint = 0,
    Zigzag = 1,
    Bool = 2,
    Fixed64 = 3,
    Fixed32 = 4,
    Double = 5,
    Bytes = 6,
    String = 7,
    Message = 8,
    Packed = 9,
}

impl TagId {
    pub fn from_u8(b: u8) -> Result<TagId> {
        Ok(match b {
            0 => TagId::Varint,
            1 => TagId::Zigzag,
            2 => TagId::Bool,
            3 => TagId::Fixed64,
            4 => TagId::Fixed32,
            5 => TagId::Double,
            6 => TagId::Bytes,
            7 => TagId::String,
            8 => TagId::Message,
            9 => TagId::Packed,
            n => {
                return Err(Error::Wire(format!(
                    "unknown type id {n} in tag (requires a newer BSE reader)"
                )))
            }
        })
    }

    pub fn as_u8(self) -> u8 {
        self as u8
    }

    pub fn as_str(self) -> &'static str {
        match self {
            TagId::Varint => "varint",
            TagId::Zigzag => "zigzag",
            TagId::Bool => "bool",
            TagId::Fixed64 => "fixed64",
            TagId::Fixed32 => "fixed32",
            TagId::Double => "double",
            TagId::Bytes => "bytes",
            TagId::String => "string",
            TagId::Message => "message",
            TagId::Packed => "packed-region",
        }
    }

    pub fn is_length_delimited(self) -> bool {
        matches!(
            self,
            TagId::Bytes | TagId::String | TagId::Message | TagId::Packed
        )
    }
}

/// Map a schema scalar type to its on-wire tag id.
pub fn tag_id_for(ty: Type) -> TagId {
    match ty {
        Type::Int32 | Type::Int64 => TagId::Varint,
        Type::Sint32 | Type::Sint64 => TagId::Zigzag,
        Type::Bool => TagId::Bool,
        Type::Fixed64 => TagId::Fixed64,
        Type::Fixed32 => TagId::Fixed32,
        Type::Double => TagId::Double,
        Type::Bytes => TagId::Bytes,
        Type::String => TagId::String,
        Type::Message => TagId::Message,
    }
}

/// Encode `(field_number, type_id)` as the tag uvarint value.
pub fn make_tag(number: u32, id: TagId) -> u64 {
    (u64::from(number) << 4) | id.as_u8() as u64
}

/// Decode a tag uvarint into field number and type id.
pub fn split_tag(tag: u64) -> Result<(u32, TagId)> {
    let number = tag >> 4;
    if number == 0 || number > u32::MAX as u64 {
        return Err(Error::Wire(format!("invalid field number {number} in tag")));
    }
    let id = TagId::from_u8((tag & 0x0f) as u8)?;
    Ok((number as u32, id))
}

/// Largest legal field number (must fit the uvarint tag alongside 4 id bits).
pub const MAX_FIELD_NUMBER: u32 = (1u64 << 29) as u32 - 1;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_roundtrip() {
        for n in [1u32, 15, 16, 1000, MAX_FIELD_NUMBER] {
            let (g, id) = split_tag(make_tag(n, TagId::String)).unwrap();
            assert_eq!(g, n);
            assert_eq!(id, TagId::String);
        }
    }

    #[test]
    fn rejects_field_zero() {
        assert!(split_tag(make_tag(0, TagId::Varint)).is_err());
    }
}
