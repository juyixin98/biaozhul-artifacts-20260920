//! # http-framing
//!
//! Incremental, allocation-bounded HTTP/1.1 **request framing** library
//! plus a local TCP reference service. Pure Rust, std-only — no HTTP
//! parser crate is used for the core; every grammar rule lives in this
//! crate.
//!
//! ## Supported subset
//!
//! * Request line: `METHOD SP request-target SP HTTP/1.1 CRLF`,
//!   origin-form targets (`/…`) plus `OPTIONS *`.
//! * Header block: `field-name ":" OWS field-value OWS CRLF`,
//!   terminated by an empty line. Exactly one optional leading SP is
//!   accepted after the colon; HT and repeated/leading/trailing
//!   whitespace are rejected.
//! * Body framing:
//!   * absent (no CL, no TE) — no body;
//!   * exactly one, strictly-decimal `Content-Length`;
//!   * exactly one `Transfer-Encoding: chunked`, decoded per
//!     RFC 9112 §4 (extensions not supported; trailers parsed,
//!     forbidden fields rejected).
//! * Request pipelining: multiple requests per connection are framed
//!   independently.
//!
//! ## Deliberate rejections
//!
//! * `Transfer-Encoding` together with `Content-Length` (either order);
//! * duplicate / conflicting `Content-Length`;
//! * obsolete line folding (`CRLF` followed by SP/HT);
//! * ambiguous whitespace (tabs in the request line, whitespace in
//!   field names, more than one leading SP, trailing whitespace, tabs
//!   in field values);
//! * bare `LF`, stray `CR`;
//! * chunk extensions and any non-`chunked` transfer coding;
//! * oversized request line / header block / header count / chunk /
//!   total body, enforced while streaming.
//!
//! See [`ErrorKind`] for the complete error catalogue.
//!
//! [`ErrorKind`]: error::ErrorKind

pub mod error;
pub mod framer;
pub mod parser;
pub mod server;

pub use error::{ErrorKind, FrameError};
pub use framer::{Framer, Limits};
pub use parser::{Framing, Header, Request};
