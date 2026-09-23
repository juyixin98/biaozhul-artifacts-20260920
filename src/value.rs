//! RESP2 value model.

/// A parsed RESP2 value.
///
/// The null distinction required by the task is modeled explicitly:
///
/// * [`Value::Null`] is the RESP2 null — a `$-1\r\n` bulk (or `*-1\r\n` null
///   array, which RESP2 treats as the same nil).
/// * [`Value::Bulk(b"")`](Value::Bulk) is an *empty* bulk string `$0\r\n\r\n` —
///   present, but zero bytes of payload. These are different on the wire and
///   different in this enum.
///
/// Bulk payloads are kept as `Vec<u8>`: they are binary-safe and may contain
/// arbitrary bytes, including embedded `\r\n`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    /// `+OK\r\n` — UTF-8 simple string.
    Simple(String),
    /// `-ERR ...\r\n` — UTF-8 error reply, kept as `(kind, message)` where
    /// `kind` is the first whitespace-delimited word (e.g. `ERR`).
    Error(String),
    /// `:<i64>\r\n`
    Integer(i64),
    /// `$<n>\r\n<n bytes>\r\n` — binary-safe. `Bulk(vec![])` is the empty string.
    Bulk(Vec<u8>),
    /// `$-1\r\n` (also `*-1\r\n`). Distinct from `Bulk(vec![])`.
    Null,
    /// `*<n>\r\n` followed by `n` values. May nest.
    Array(Vec<Value>),
}

impl Value {
    /// `true` only for [`Value::Null`].
    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    /// `true` for `Bulk(b"")` — an empty string that is *not* a null.
    pub fn is_empty_bulk(&self) -> bool {
        matches!(self, Value::Bulk(b) if b.is_empty())
    }

    /// Bulk payload bytes, if this value is a bulk string.
    pub fn as_bytes(&self) -> Option<&[u8]> {
        match self {
            Value::Bulk(b) => Some(b),
            _ => None,
        }
    }

    /// Bulk payload interpreted as UTF-8 (used by the demo server).
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Simple(s) | Value::Error(s) => Some(s),
            Value::Bulk(b) => std::str::from_utf8(b).ok(),
            _ => None,
        }
    }

    pub fn as_integer(&self) -> Option<i64> {
        match self {
            Value::Integer(i) => Some(*i),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Value::Array(items) => Some(items),
            _ => None,
        }
    }

    /// Encode this value back to RESP2 wire bytes.
    ///
    /// Simple strings / errors must not contain `\r` or `\n`; returns `None`
    /// if they do (callers should send such data as a bulk instead).
    pub fn encode(&self, out: &mut Vec<u8>) -> Option<()> {
        match self {
            Value::Simple(s) => {
                if s.as_bytes().contains(&b'\r') || s.as_bytes().contains(&b'\n') {
                    return None;
                }
                out.push(b'+');
                out.extend_from_slice(s.as_bytes());
                out.extend_from_slice(b"\r\n");
            }
            Value::Error(s) => {
                if s.as_bytes().contains(&b'\r') || s.as_bytes().contains(&b'\n') {
                    return None;
                }
                out.push(b'-');
                out.extend_from_slice(s.as_bytes());
                out.extend_from_slice(b"\r\n");
            }
            Value::Integer(i) => {
                out.push(b':');
                out.extend_from_slice(i.to_string().as_bytes());
                out.extend_from_slice(b"\r\n");
            }
            Value::Bulk(b) => {
                out.push(b'$');
                out.extend_from_slice(b.len().to_string().as_bytes());
                out.extend_from_slice(b"\r\n");
                out.extend_from_slice(b);
                out.extend_from_slice(b"\r\n");
            }
            Value::Null => out.extend_from_slice(b"$-1\r\n"),
            Value::Array(items) => {
                out.push(b'*');
                out.extend_from_slice(items.len().to_string().as_bytes());
                out.extend_from_slice(b"\r\n");
                for item in items {
                    item.encode(out)?;
                }
            }
        }
        Some(())
    }

    /// Convenience wrapper around [`Value::encode`].
    pub fn to_wire(&self) -> Option<Vec<u8>> {
        let mut out = Vec::new();
        self.encode(&mut out).map(|()| out)
    }
}
