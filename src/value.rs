use std::fmt;

/// Limits applied while parsing. All fields are configurable.
#[derive(Debug, Clone, Copy)]
pub struct Config {
    /// Maximum nesting depth of arrays. A top-level value is depth 1;
    /// an array inside it is depth 2, and so on.
    pub max_depth: usize,
    /// Maximum number of unparsed bytes held in the parser buffer.
    /// Because an incomplete message stays in the buffer, this also caps
    /// the total byte size of any single message.
    pub max_buffer_bytes: usize,
    /// Maximum length (in bytes) of a single bulk string payload.
    pub max_bulk_len: usize,
    /// Maximum element count of a single array.
    pub max_array_len: usize,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            max_depth: 64,
            max_buffer_bytes: 16 * 1024 * 1024,
            max_bulk_len: 512 * 1024 * 1024,
            max_array_len: 1024 * 1024,
        }
    }
}

/// A parsed RESP2 value.
///
/// Null and empty are distinct: a null bulk string is
/// `BulkString(None)` while an empty one is `BulkString(Some(vec![]))`;
/// likewise for arrays.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    /// `+OK\r\n`
    SimpleString(String),
    /// `-ERR something\r\n`
    Error(String),
    /// `:42\r\n`
    Integer(i64),
    /// `$n\r\n<bytes>\r\n`; `None` for `$-1\r\n` (null).
    BulkString(Option<Vec<u8>>),
    /// `*n\r\n<elements>`; `None` for `*-1\r\n` (null).
    Array(Option<Vec<Value>>),
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Value::SimpleString(s) => write!(f, "simple-string({:?})", s),
            Value::Error(s) => write!(f, "error({:?})", s),
            Value::Integer(i) => write!(f, "integer({})", i),
            Value::BulkString(None) => write!(f, "bulk-string(null)"),
            Value::BulkString(Some(b)) => write!(f, "bulk-string({} bytes)", b.len()),
            Value::Array(None) => write!(f, "array(null)"),
            Value::Array(Some(items)) => write!(f, "array({} elements)", items.len()),
        }
    }
}

/// Re-encode a value to its canonical RESP2 wire form.
pub fn encode(v: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    encode_into(v, &mut out);
    out
}

fn encode_into(v: &Value, out: &mut Vec<u8>) {
    match v {
        Value::SimpleString(s) => {
            out.push(b'+');
            out.extend_from_slice(s.as_bytes());
            out.extend_from_slice(b"\r\n");
        }
        Value::Error(s) => {
            out.push(b'-');
            out.extend_from_slice(s.as_bytes());
            out.extend_from_slice(b"\r\n");
        }
        Value::Integer(i) => {
            out.push(b':');
            out.extend_from_slice(i.to_string().as_bytes());
            out.extend_from_slice(b"\r\n");
        }
        Value::BulkString(None) => out.extend_from_slice(b"$-1\r\n"),
        Value::BulkString(Some(b)) => {
            out.push(b'$');
            out.extend_from_slice(b.len().to_string().as_bytes());
            out.extend_from_slice(b"\r\n");
            out.extend_from_slice(b);
            out.extend_from_slice(b"\r\n");
        }
        Value::Array(None) => out.extend_from_slice(b"*-1\r\n"),
        Value::Array(Some(items)) => {
            out.push(b'*');
            out.extend_from_slice(items.len().to_string().as_bytes());
            out.extend_from_slice(b"\r\n");
            for item in items {
                encode_into(item, out);
            }
        }
    }
}
