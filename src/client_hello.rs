//! Plaintext `ClientHello` (handshake_type 1) parsing and the GREASE
//! classifier (RFC 8701).
//!
//! Everything here is reachable only after record fragments have been
//! reassembled into a handshake body whose declared length the reassembler
//! has fully received. Encrypted bytes never reach this module: record
//! content_type 23 (`ApplicationData`) is counted and not dissected.

use crate::cursor::{NeedMore, Reader};
use crate::error::{Layer, LengthMismatchKind, ParseError, Truncated, Warning};

pub const HANDSHAKE_TYPE_CLIENT_HELLO: u8 = 1;

pub const EXT_SERVER_NAME: u16 = 0x0000;
pub const EXT_APPLICATION_LAYER_PROTOCOL_NEGOTIATION: u16 = 0x0010; // ALPN

pub const SERVER_NAME_TYPE_HOST_NAME: u8 = 0;

/// RFC 8701 GREASE values: each byte is identical and its low nibble is
/// `0xA` — {0x0A,1A,2A,3A,4A,5A,6A,7A,8A,9A,AA,BA,CA,DA,EA}0x.
/// Used for cipher suites, extensions, versions and named groups.
pub fn is_grease_u16(value: u16) -> bool {
    let high = (value >> 8) as u8;
    let low = (value & 0xff) as u8;
    high == low && high & 0x0f == 0x0a
}

/// An extension the observer does not parse. GREASE extensions are classified
/// separately (see [`Warning::GreaseExtension`]) and never appear here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownExtension {
    pub type_: u16,
    pub data_hex: String,
    pub data_len: usize,
}

/// The observation extracted from one plaintext ClientHello.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClientHello {
    /// `(major, minor)` from `client_version` (legacy_version in TLS 1.3).
    pub client_version: (u8, u8),
    /// The 32-byte `random`, lowercase hex.
    pub random_hex: String,
    /// `legacy_session_id`, lowercase hex (empty string for 0-length).
    pub session_id_hex: String,
    /// Non-GREASE cipher-suite values offered by the client.
    pub cipher_suites: Vec<u16>,
    /// GREASE cipher-suite values, classified per RFC 8701.
    pub grease_cipher_suites: Vec<u16>,
    pub compression_methods: Vec<u8>,
    /// First `host_name` server_name, if present and valid UTF-8.
    pub sni: Option<String>,
    /// ALPN protocol names offered (ProtocolName is opaque bytes; valid UTF-8
    /// names are rendered lossily).
    pub alpn: Vec<String>,
    /// Extensions other than SNI/ALPN/GREASE. Repeated types are listed once
    /// (a duplicate warning is recorded for subsequent occurrences).
    pub unknown_extensions: Vec<UnknownExtension>,
}

/// Parse one 4-byte handshake header: `HandshakeType` (1 byte) and
/// `length` (3 bytes, RFC-style u24).
pub(crate) fn parse_handshake_head(r: &mut Reader<'_>) -> Result<(u8, u32), ParseError> {
    let map = |_: NeedMore| ParseError::Truncated {
        layer: Layer::HandshakeHeader,
        where_: Truncated::Field,
    };
    let type_ = r.read_u8().map_err(map)?;
    let length = r.read_u24().map_err(map)?;
    Ok((type_, length))
}

/// Parse a complete ClientHello body. `body` must have exactly the length
/// declared by the handshake header (guaranteed by the reassembler).
pub(crate) fn parse_client_hello(
    body: &[u8],
    warnings: &mut Vec<Warning>,
) -> Result<ClientHello, ParseError> {
    let mut r = Reader::new(body);

    let need = |_: NeedMore| ParseError::Truncated {
        layer: Layer::HandshakeBody,
        where_: Truncated::Field,
    };

    // client_version
    let version = r.read_u16().map_err(need)?;
    let (major, minor) = ((version >> 8) as u8, (version & 0xff) as u8);
    if major != 3 || !(1..=4).contains(&minor) {
        return Err(ParseError::InvalidClientHelloVersion { major, minor });
    }

    // random: exactly 32 bytes
    let random = r.take(32).map_err(need)?;

    // legacy_session_id: u8 vector, max 32 by the TLS field rules
    let session_id = {
        let len = r.read_u8().map_err(need)? as usize;
        if len > 32 {
            return Err(ParseError::MalformedVector {
                layer: Layer::HandshakeBody,
                what: "client_hello.legacy_session_id",
                length: len,
            });
        }
        bounded_take(&mut r, len, "client_hello.legacy_session_id")?
    };

    // cipher_suites: u16 vector, must be non-empty and even-sized
    let cipher_suites_bytes = {
        let len = r.read_u16().map_err(need)? as usize;
        if len < 2 || !len.is_multiple_of(2) {
            return Err(ParseError::MalformedVector {
                layer: Layer::HandshakeBody,
                what: "client_hello.cipher_suites",
                length: len,
            });
        }
        bounded_take(&mut r, len, "client_hello.cipher_suites")?
    };

    // legacy_compression_methods: u8 vector, must be non-empty
    let compression_methods = {
        let len = r.read_u8().map_err(need)? as usize;
        if len < 1 {
            return Err(ParseError::MalformedVector {
                layer: Layer::HandshakeBody,
                what: "client_hello.legacy_compression_methods",
                length: len,
            });
        }
        bounded_take(&mut r, len, "client_hello.legacy_compression_methods")?
    };

    let mut cipher_suites = Vec::new();
    let mut grease_cipher_suites = Vec::new();
    // Even length is enforced above, so the trailing slice is empty.
    let (pairs, _rest) = cipher_suites_bytes.as_chunks::<2>();
    for pair in pairs {
        let value = u16::from_be_bytes([pair[0], pair[1]]);
        if is_grease_u16(value) {
            grease_cipher_suites.push(value);
            warnings.push(Warning::GreaseCipherSuite { value });
        } else {
            cipher_suites.push(value);
        }
    }

    // extensions: optional in TLS 1.0/1.1 practice; RFC 8446 makes the field
    // mandatory for ClientHello, but accepting an absent field keeps the
    // accepted subset wide enough for legacy captures. If the field is
    // present its declared length must cover exactly the remaining body.
    let mut sni = None;
    let mut alpn = Vec::new();
    let mut unknown_extensions = Vec::new();

    if !r.is_empty() {
        let ext_declared = r.read_u16().map_err(need)? as usize;
        let ext_actual = r.remaining();
        if ext_declared != ext_actual {
            return Err(ParseError::LengthMismatch {
                layer: Layer::HandshakeBody,
                kind: LengthMismatchKind::Nested,
                what: "client_hello.extensions",
                declared: ext_declared,
                actual: ext_actual,
            });
        }
        let ext_block = r.rest();
        parse_extensions(
            ext_block,
            &mut sni,
            &mut alpn,
            &mut unknown_extensions,
            warnings,
        )?;
    }

    Ok(ClientHello {
        client_version: (major, minor),
        random_hex: crate::to_hex(random),
        session_id_hex: crate::to_hex(session_id),
        cipher_suites,
        grease_cipher_suites,
        compression_methods: compression_methods.to_vec(),
        sni,
        alpn,
        unknown_extensions,
    })
}

/// Take `n` bytes from a body whose enclosing length is already known:
/// a shortfall means an inner length prefix overran its parent, i.e. a
/// nested length mismatch, not a truncated TCP stream.
fn bounded_take<'a>(
    r: &mut Reader<'a>,
    n: usize,
    what: &'static str,
) -> Result<&'a [u8], ParseError> {
    if r.remaining() < n {
        return Err(ParseError::LengthMismatch {
            layer: Layer::HandshakeBody,
            kind: LengthMismatchKind::Nested,
            what,
            declared: n,
            actual: r.remaining(),
        });
    }
    Ok(r.take(n).expect("bounds checked above"))
}

fn parse_extensions(
    block: &[u8],
    sni: &mut Option<String>,
    alpn: &mut Vec<String>,
    unknown: &mut Vec<UnknownExtension>,
    warnings: &mut Vec<Warning>,
) -> Result<(), ParseError> {
    let mut r = Reader::new(block);
    let mut seen: Vec<u16> = Vec::new();

    while !r.is_empty() {
        let need = |_: NeedMore| ParseError::Truncated {
            layer: Layer::Extension,
            where_: Truncated::Field,
        };
        let ext_type = r.read_u16().map_err(need)?;
        let ext_len = r.read_u16().map_err(need)? as usize;

        if ext_len > r.remaining() {
            return Err(ParseError::LengthMismatch {
                layer: Layer::Extension,
                kind: LengthMismatchKind::Nested,
                what: "extension.extension_data",
                declared: ext_len,
                actual: r.remaining(),
            });
        }
        let data = r.take(ext_len).expect("bounds checked above");

        // GREASE first: duplicate GREASE values are legitimate by design.
        if is_grease_u16(ext_type) {
            warnings.push(Warning::GreaseExtension { type_: ext_type });
            continue;
        }
        if seen.contains(&ext_type) {
            warnings.push(Warning::DuplicateExtension { type_: ext_type });
            continue;
        }
        seen.push(ext_type);

        match ext_type {
            EXT_SERVER_NAME => {
                if let Some(name) = parse_sni(data, warnings)? {
                    sni.get_or_insert(name);
                }
            }
            EXT_APPLICATION_LAYER_PROTOCOL_NEGOTIATION => {
                parse_alpn(data, alpn, warnings)?;
            }
            other => unknown.push(UnknownExtension {
                type_: other,
                data_hex: crate::to_hex(data),
                data_len: data.len(),
            }),
        }
    }
    Ok(())
}

/// RFC 6066 server_name extension.
fn parse_sni(data: &[u8], warnings: &mut Vec<Warning>) -> Result<Option<String>, ParseError> {
    let mut r = Reader::new(data);
    let list_len = r.read_u16().map_err(|_| ParseError::Truncated {
        layer: Layer::Extension,
        where_: Truncated::Field,
    })? as usize;
    if list_len != r.remaining() {
        return Err(ParseError::LengthMismatch {
            layer: Layer::Extension,
            kind: LengthMismatchKind::Nested,
            what: "server_name.server_name_list",
            declared: list_len,
            actual: r.remaining(),
        });
    }

    let mut host_name = None;
    while !r.is_empty() {
        let name_type = r.read_u8().map_err(|_| ParseError::Truncated {
            layer: Layer::Extension,
            where_: Truncated::Field,
        })?;
        let name_len = r.read_u16().map_err(|_| ParseError::Truncated {
            layer: Layer::Extension,
            where_: Truncated::Field,
        })? as usize;
        if name_len > r.remaining() {
            return Err(ParseError::LengthMismatch {
                layer: Layer::Extension,
                kind: LengthMismatchKind::Nested,
                what: "server_name.host_name",
                declared: name_len,
                actual: r.remaining(),
            });
        }
        let name_bytes = r.take(name_len).expect("bounds checked above");
        if name_type != SERVER_NAME_TYPE_HOST_NAME {
            warnings.push(Warning::UnknownServerNameType { type_: name_type });
            continue;
        }
        match std::str::from_utf8(name_bytes) {
            Ok(s) => {
                if host_name.is_none() {
                    host_name = Some(s.to_owned());
                }
            }
            Err(_) => warnings.push(Warning::InvalidSniUtf8),
        }
    }
    Ok(host_name)
}

/// RFC 7301 ALPN extension.
fn parse_alpn(
    data: &[u8],
    alpn: &mut Vec<String>,
    warnings: &mut Vec<Warning>,
) -> Result<(), ParseError> {
    let mut r = Reader::new(data);
    let list_len = r.read_u16().map_err(|_| ParseError::Truncated {
        layer: Layer::Extension,
        where_: Truncated::Field,
    })? as usize;
    if list_len != r.remaining() {
        return Err(ParseError::LengthMismatch {
            layer: Layer::Extension,
            kind: LengthMismatchKind::Nested,
            what: "alpn.protocol_name_list",
            declared: list_len,
            actual: r.remaining(),
        });
    }

    while !r.is_empty() {
        let name_len = r.read_u8().map_err(|_| ParseError::Truncated {
            layer: Layer::Extension,
            where_: Truncated::Field,
        })? as usize;
        if name_len == 0 || name_len > r.remaining() {
            // Zero-length ProtocolName is forbidden; an overrun is a nested
            // mismatch. A zero length with bytes left is a warning and skip
            // of just that entry; an overrun is structural.
            if name_len == 0 {
                warnings.push(Warning::InvalidAlpnEntry);
                continue;
            }
            return Err(ParseError::LengthMismatch {
                layer: Layer::Extension,
                kind: LengthMismatchKind::Nested,
                what: "alpn.protocol_name",
                declared: name_len,
                actual: r.remaining(),
            });
        }
        let name = r.take(name_len).expect("bounds checked above");
        alpn.push(String::from_utf8_lossy(name).into_owned());
    }
    Ok(())
}
