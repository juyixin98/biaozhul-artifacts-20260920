//! # bse — streaming binary codec with schema evolution
//!
//! `bse` defines a small documented wire format (**BSE1**), a schema
//! model with field numbers, optional/required/repeated cardinalities,
//! unknown-field preservation and explicit evolution rules, plus a JSON
//! control surface.
//!
//! * [`wire`] documents and implements the byte format.
//! * [`schema`] defines types and the evolution compatibility matrix.
//! * [`codec`] encodes/decodes messages under a schema with bounded
//!   memory and output size.
//! * [`api`] is the JSON control entry point.
//!
//! The crate has no third-party dependencies; JSON, base64 and varint
//! handling are implemented from scratch.

pub mod api;
pub mod codec;
pub mod error;
pub mod json;
pub mod jsonio;
pub mod schema;
pub mod value;
pub mod wire;

pub use error::Error;
pub use wire::Limits;
