//! # rbitset
//!
//! A dependency-free, streaming hybrid sparse-array / bitmap set library for
//! 32-bit unsigned integers, plus a binary serialization format and a JSON
//! control surface.
//!
//! * [`container::Container`] — one 16-bit universe as sorted array or bitmap.
//! * [`set::IntSet`] — the full `u32` set with union, intersection, difference.
//! * [`codec`] — bounded streaming binary encode/decode (see `docs/format.md`).
//! * [`json`] — strict JSON parser/serialiser, base64, and request dispatch.

pub mod codec;
pub mod container;
pub mod error;
pub mod json;
pub mod set;

pub use error::{Error, Result};
pub use set::IntSet;
