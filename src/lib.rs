//! # resp-incremental
//!
//! A hand-written, zero-dependency **incremental RESP2 parser** in Rust plus a
//! small local TCP test server.
//!
//! ## Supported RESP2 subset
//!
//! | Wire form        | Rust value            |
//! |------------------|-----------------------|
//! | `+OK\r\n`        | [`Value::Simple`]     |
//! | `-ERR msg\r\n`   | [`Value::Error`]      |
//! | `:42\r\n`        | [`Value::Integer`]    |
//! | `$5\r\nhello\r\n`| [`Value::Bulk`] (binary-safe; CRLF may appear inside) |
//! | `$0\r\n\r\n`     | `Bulk(Vec::new())` — empty string, **not** null |
//! | `$-1\r\n`        | [`Value::Null`]       |
//! | `*-1\r\n`        | [`Value::Null`] (null array ⇒ nil) |
//! | `*2\r\n...`      | [`Value::Array`] (nests recursively) |
//!
//! Not in scope: RESP3 types, inline commands.
//!
//! ## Example
//!
//! ```
//! use resp_incremental::{Config, Parser, Poll, Value};
//!
//! let mut p = Parser::new(Config::default());
//! p.feed(b"$5\r\nhel").unwrap();
//! assert_eq!(p.try_next(), Poll::Pending);
//! p.feed(b"lo\r\n").unwrap();
//! assert_eq!(p.try_next(), Poll::Ready(Value::Bulk(b"hello".to_vec())));
//! ```

pub mod error;
pub mod parser;
pub mod value;

pub use error::{ParseError, Poll};
pub use parser::{try_parse, Config, Parser};
pub use value::Value;

/// Construct a [`Value::Simple`].
pub fn simple(s: impl Into<String>) -> Value {
    Value::Simple(s.into())
}

/// Construct a [`Value::Error`] reply, e.g. `err("ERR", "unknown command")`.
pub fn error_reply(kind: &str, msg: &str) -> Value {
    Value::Error(format!("{} {}", kind, msg))
}

/// Construct a binary-safe [`Value::Bulk`].
pub fn bulk(b: impl Into<Vec<u8>>) -> Value {
    Value::Bulk(b.into())
}

/// Construct an [`Value::Array`].
pub fn array(items: impl IntoIterator<Item = Value>) -> Value {
    Value::Array(items.into_iter().collect())
}
