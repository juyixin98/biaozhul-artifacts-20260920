//! Byte-level character predicates and text helpers (RFC 7230).

/// RFC 7230 `tchar`: legal bytes inside a method or a header field name.
pub fn is_tchar(b: u8) -> bool {
    matches!(
        b,
        b'!' | b'#'
            | b'$'
            | b'%'
            | b'&'
            | b'\''
            | b'*'
            | b'+'
            | b'-'
            | b'.'
            | b'^'
            | b'_'
            | b'`'
            | b'|'
            | b'~'
    ) || b.is_ascii_alphanumeric()
}

/// RFC 7230 `VCHAR`: any visible US-ASCII character.
pub fn is_vchar(b: u8) -> bool {
    (0x21..=0x7E).contains(&b)
}

/// RFC 7230 `obs-text`: non-ASCII bytes are tolerated inside field values
/// (the parser treats the stream as bytes, not UTF-8).
pub fn is_obs_text(b: u8) -> bool {
    b >= 0x80
}

/// Legal byte inside a header field *value* (after trimming OWS):
/// the value grammar is `*( SP / HTAB / field-vchar )` where
/// `field-vchar = VCHAR / obs-text`. CR/LF are never legal here — that is
/// what rejects folded lines and bare line endings smuggled in a value.
pub fn is_field_value_byte(b: u8) -> bool {
    b == b' ' || b == b'\t' || is_vchar(b) || is_obs_text(b)
}

/// Legal byte inside a request target: `VCHAR` minus nothing extra
/// (origin-form / absolute-form / asterisk all stay within visible ASCII).
pub fn is_request_target_byte(b: u8) -> bool {
    is_vchar(b)
}

pub fn is_ascii_hexdig(b: u8) -> bool {
    b.is_ascii_hexdigit()
}

/// Case-insensitive equality for ASCII header names.
pub fn eq_ignore_ascii_case(a: &[u8], b: &[u8]) -> bool {
    a.eq_ignore_ascii_case(b)
}

/// Trim RFC 7230 OWS (`*( SP / HTAB )`) from both ends of a field value.
pub fn trim_ows(v: &[u8]) -> &[u8] {
    let is_ows = |b: &u8| *b == b' ' || *b == b'\t';
    let start = v.iter().position(|b| !is_ows(b)).unwrap_or(v.len());
    let end = v
        .iter()
        .rposition(|b| !is_ows(b))
        .map(|i| i + 1)
        .unwrap_or(start);
    &v[start..end]
}
