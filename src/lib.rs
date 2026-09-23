//! # tls-record-observer
//!
//! A hand-written, **incremental** byte parser that observes a single
//! direction of a TLS 1.0–1.3 TCP stream.
//!
//! Scope on purpose:
//!
//! * parses **record headers only** (RFC 8446 §5.1) — payloads are never
//!   decrypted;
//! * reassembles handshake messages **across records** and parses a
//!   **plaintext `ClientHello`** (handshake_type 1);
//! * extracts **SNI** and **ALPN**, classifies **GREASE** (RFC 8701), and
//!   records every other extension as opaque data;
//! * stops dissection the moment the layer is encrypted, so ciphertext is
//!   never mistaken for a handshake message.
//!
//! It does **not** implement the TLS handshake, key exchange, certificate
//! validation, or any decryption.

mod cursor;

pub mod client_hello;
pub mod error;
pub mod observer;
pub mod record;

pub use client_hello::{is_grease_u16, ClientHello, UnknownExtension};
pub use error::{Layer, LengthMismatchKind, ParseError, Truncated, Warning};
pub use observer::{observe, Limits, Observation, ObservedRecord, Observer};

/// Lowercase hex encoding (kept internal to stay dependency-free).
pub(crate) fn to_hex(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(H[(b >> 4) as usize] as char);
        out.push(H[(b & 0x0f) as usize] as char);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grease_classification() {
        for v in [0x0a0a, 0x1a1a, 0x2a2a, 0x3a3a, 0xfafa, 0xaaaa] {
            assert!(is_grease_u16(v), "{v:#06x} should be GREASE");
        }
        for v in [0x0000, 0x0001, 0x1301, 0x001a, 0x1a0a, 0xabab] {
            assert!(!is_grease_u16(v), "{v:#06x} must not be GREASE");
        }
    }

    #[test]
    fn hex_encoding() {
        assert_eq!(to_hex(&[0x00, 0xff, 0x0a, 0xA0]), "00ff0aa0");
        assert_eq!(to_hex(&[]), "");
    }
}
