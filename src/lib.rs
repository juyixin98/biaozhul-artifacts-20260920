//! # bschema — streaming binary schema-evolution codec
//!
//! A small, dependency-free Rust library implementing the **BSE1** binary
//! message format: numbered fields, explicit presence, packed/unpacked
//! repetition, byte-exact unknown-field forwarding and strict rejection of
//! incompatible type evolution. See `FORMAT.md` for the wire specification.
//!
//! Entry points:
//! - [`schema::Schema::from_json`] — parse a schema document
//! - [`writer::encode`] / [`writer::encode_to_stream`] — encode
//! - [`reader::decode`] / [`reader::decode_from_stream`] — decode
//! - [`value::json_to_message`] / [`value::message_to_json`] — JSON bridge

pub mod base64;
pub mod cli;
pub mod error;
pub mod fingerprint;
pub mod ieee;
pub mod json;
pub mod limits;
pub mod reader;
pub mod schema;
pub mod value;
pub mod varint;
pub mod wire;
pub mod writer;

pub use error::Error;
pub use limits::Limits;
pub use reader::{decode, decode_from_stream, DecodeOutput};
pub use schema::Schema;
pub use value::{json_to_message, message_to_json, MessageValue, Value};
pub use wire::TagId;
pub use writer::{encode, encode_to_stream};
