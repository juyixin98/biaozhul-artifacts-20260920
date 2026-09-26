//! # BSE1 wire format (streaming codec)
//!
//! A message on the wire is a sequence of **fields**. Each field is:
//!
//! ```text
//! +-------------------+--------------------------------------------+
//! | tag (varint)      | payload, whose shape depends on wire type  |
//! +-------------------+--------------------------------------------+
//! ```
//!
//! The tag is `(field_number << 3) | wire_type` encoded as a LEB128
//! unsigned varint. Field numbers start at 1.
//!
//! | wire type | name   | payload                                  |
//! |-----------|--------|------------------------------------------|
//! | 0         | VARINT | LEB128 varint (signed ints use zig-zag) |
//! | 1         | I64    | 8 little-endian bytes (f64)             |
//! | 2         | LEN    | varint length `n`, then `n` bytes       |
//! | 3         | I32    | 4 little-endian bytes (f32)             |
//!
//! LEN payloads carry `string`, `bytes`, embedded messages and
//! **packed** repeated scalar fields.
//!
//! Fields may appear in any order and may be repeated. There is no
//! end-of-message marker: an embedded message is exactly the `n` bytes
//! declared by its LEN header; a top-level message ends at the stream EOF
//! (or at the payload boundary of the [`Envelope`] wrapper).
//!
//! ## Streaming and limits
//!
//! [`WireReader`] pulls bytes lazily from any [`std::io::Read`] and never
//! materialises the whole stream. Each LEN value is buffered individually
//! but its size is capped by [`Limits::max_value_len`], the total number
//! of wire bytes a single decode may consume is capped by
//! [`Limits::max_output`], and nesting depth is capped by
//! [`Limits::max_depth`].

use std::io::{Read, Write};

use crate::error::{Error, Result};

/// Wire type bits carried in the low three bits of a tag.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum WireType {
    /// LEB128 varint.
    Varint = 0,
    /// 8 fixed little-endian bytes.
    I64 = 1,
    /// Varint length followed by that many bytes.
    Len = 2,
    /// 4 fixed little-endian bytes.
    I32 = 3,
}

impl WireType {
    fn from_bits(bits: u8) -> Result<WireType> {
        match bits {
            0 => Ok(WireType::Varint),
            1 => Ok(WireType::I64),
            2 => Ok(WireType::Len),
            3 => Ok(WireType::I32),
            other => Err(Error::UnknownWireType(other)),
        }
    }
}

/// Resource limits for a single decode.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// Maximum declared length of a single LEN value (bytes).
    pub max_value_len: u64,
    /// Maximum number of wire bytes one decode may consume in total.
    pub max_output: u64,
    /// Maximum nesting depth of embedded messages.
    pub max_depth: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_value_len: 16 * 1024 * 1024,
            max_output: 64 * 1024 * 1024,
            max_depth: 32,
        }
    }
}

/// Largest legal field number (`2^29 - 1`, as in Protocol Buffers).
pub const MAX_FIELD_NUMBER: u32 = (1 << 29) - 1;

/// Encode a tag from field number and wire type.
pub fn encode_tag(field: u32, wt: WireType) -> u64 {
    ((field as u64) << 3) | (wt as u64)
}

/// Split a tag value into field number and wire type.
pub fn decode_tag(tag: u64) -> Result<(u32, WireType)> {
    let field = tag >> 3;
    if field == 0 || field > MAX_FIELD_NUMBER as u64 {
        return Err(Error::InvalidFieldNumber(field));
    }
    Ok((field as u32, WireType::from_bits((tag & 0x7) as u8)?))
}

/// Zig-zag encode a signed 32-bit integer.
pub fn zigzag_encode32(n: i32) -> u32 {
    ((n << 1) ^ (n >> 31)) as u32
}

/// Zig-zag decode a signed 32-bit integer.
pub fn zigzag_decode32(u: u32) -> i32 {
    (u >> 1) as i32 ^ -((u & 1) as i32)
}

/// Zig-zag encode a signed 64-bit integer.
pub fn zigzag_encode64(n: i64) -> u64 {
    ((n << 1) ^ (n >> 63)) as u64
}

/// Zig-zag decode a signed 64-bit integer.
pub fn zigzag_decode64(u: u64) -> i64 {
    (u >> 1) as i64 ^ -((u & 1) as i64)
}

/// Write an unsigned LEB128 varint to `out`. Returns the number of bytes
/// written (at most 10).
pub fn write_varint(out: &mut Vec<u8>, mut value: u64) -> usize {
    let start = out.len();
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        out.push(byte);
        if value == 0 {
            break;
        }
    }
    out.len() - start
}

// ---------------------------------------------------------------------------
// Streaming writer
// ---------------------------------------------------------------------------

/// Streaming encoder over any [`Write`] implementor.
pub struct WireWriter<W: Write> {
    inner: W,
}

impl<W: Write> WireWriter<W> {
    /// Wrap an output stream.
    pub fn new(inner: W) -> Self {
        WireWriter { inner }
    }

    /// Flush and return the wrapped writer.
    pub fn into_inner(mut self) -> Result<W> {
        self.inner.flush()?;
        Ok(self.inner)
    }

    /// Write raw bytes (used to forward captured unknown fields).
    pub fn raw(&mut self, bytes: &[u8]) -> Result<()> {
        self.inner.write_all(bytes)?;
        Ok(())
    }

    /// Write a field tag.
    pub fn tag(&mut self, field: u32, wt: WireType) -> Result<()> {
        let mut buf = Vec::with_capacity(5);
        write_varint(&mut buf, encode_tag(field, wt));
        self.inner.write_all(&buf)?;
        Ok(())
    }

    /// Write a VARINT payload.
    pub fn varint(&mut self, value: u64) -> Result<()> {
        let mut buf = Vec::with_capacity(10);
        write_varint(&mut buf, value);
        self.inner.write_all(&buf)?;
        Ok(())
    }

    /// Write an I32 payload.
    pub fn fixed32(&mut self, bytes: [u8; 4]) -> Result<()> {
        self.inner.write_all(&bytes)?;
        Ok(())
    }

    /// Write an I64 payload.
    pub fn fixed64(&mut self, bytes: [u8; 8]) -> Result<()> {
        self.inner.write_all(&bytes)?;
        Ok(())
    }

    /// Write a LEN payload: length prefix followed by `data`.
    pub fn len_bytes(&mut self, data: &[u8]) -> Result<()> {
        let mut buf = Vec::with_capacity(10);
        write_varint(&mut buf, data.len() as u64);
        self.inner.write_all(&buf)?;
        self.inner.write_all(data)?;
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Streaming reader
// ---------------------------------------------------------------------------

/// A field that the active schema did not know about.
///
/// The payload is stored **verbatim** (everything after the tag, including
/// the LEN length prefix when applicable) so the field can be forwarded
/// byte-for-byte without understanding its content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawField {
    /// Field number as it appeared on the wire.
    pub field: u32,
    /// Wire type as it appeared on the wire.
    pub wire_type: WireType,
    /// Raw payload bytes after the tag.
    pub payload: Vec<u8>,
}

/// A decoded tag; the reader is positioned at the first payload byte.
#[derive(Debug, Clone, Copy)]
pub struct PositionedTag {
    pub field: u32,
    pub wire_type: WireType,
}

/// Map an I/O error so a short read becomes the crate's `UnexpectedEof`
/// (a *clean* top-level EOF is detected one level up, before any byte is
/// consumed); other I/O errors are wrapped as such.
fn io_eof(e: std::io::Error) -> Error {
    match e.kind() {
        std::io::ErrorKind::UnexpectedEof => Error::UnexpectedEof { needed: None },
        _ => Error::Io(e.to_string()),
    }
}

/// Streaming, bounded decoder over any [`Read`] implementor.
///
/// A reader covers exactly one message. It is constructed over a source
/// for the top-level message (reading until EOF) or over a bounded buffer
/// for an embedded message (reading until the declared byte count).
pub struct WireReader<'a> {
    src: Box<dyn Read + 'a>,
    /// Bytes left in a bounded message; `None` means read until EOF.
    remaining: Option<u64>,
    limits: Limits,
    /// Current nesting depth (0 at the top level).
    depth: u32,
    /// Total wire bytes charged against `limits.max_output`.
    consumed: u64,
    /// Whether reads in this reader charge the output budget. Readers
    /// created over already-buffered LEN bytes do not, because the parent
    /// reader charged those bytes when it buffered them.
    counts: bool,
}

impl<'a> WireReader<'a> {
    /// Begin reading a top-level message that extends until EOF.
    pub fn new<R: Read + 'a>(src: R, limits: Limits) -> Self {
        WireReader {
            src: Box::new(src),
            remaining: None,
            limits,
            depth: 0,
            consumed: 0,
            counts: true,
        }
    }

    /// Reader over an already-buffered, length-bounded byte slice (an
    /// embedded message or a packed payload). The bytes were charged by
    /// the reader that produced them, so this reader only enforces the
    /// boundary and parses; it does not charge the budget again.
    pub fn buffered(raw: Vec<u8>, limits: Limits, depth: u32) -> Self {
        let len = raw.len() as u64;
        WireReader {
            src: Box::new(std::io::Cursor::new(raw)),
            remaining: Some(len),
            limits,
            depth,
            consumed: 0,
            counts: false,
        }
    }

    /// Top-level reader over exactly `len` bytes of a live stream. Unlike
    /// [`WireReader::buffered`], the bytes are pulled lazily and charged
    /// against `limits.max_output` as they are read, so an envelope is
    /// decoded without first buffering the whole payload.
    fn bounded_stream<R: Read + 'a>(src: R, limits: Limits, len: u64) -> Self {
        WireReader {
            src: Box::new(Limited::new(src, len)),
            remaining: Some(len),
            limits,
            depth: 0,
            consumed: 0,
            counts: true,
        }
    }

    /// Read a BSE1 envelope header and return a reader positioned at the
    /// first payload byte, bounded to the declared payload length. The
    /// payload is streamed, not buffered.
    pub fn from_envelope<R: Read + 'a>(src: R, limits: Limits) -> Result<Self> {
        let mut src = src;
        let header = read_envelope_header(&mut src)?;
        if header.payload_len > limits.max_output {
            return Err(Error::OutputLimitExceeded {
                limit: limits.max_output,
            });
        }
        Ok(WireReader::bounded_stream(src, limits, header.payload_len))
    }

    /// Configured limits.
    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// True when positioned at the end of a bounded message.
    pub fn at_end(&self) -> bool {
        self.remaining == Some(0)
    }

    fn charge(&mut self, n: u64) -> Result<u64> {
        if !self.counts {
            return Ok(self.consumed);
        }
        self.consumed = self.consumed.saturating_add(n);
        if self.consumed > self.limits.max_output {
            return Err(Error::OutputLimitExceeded {
                limit: self.limits.max_output,
            });
        }
        Ok(self.consumed)
    }

    fn read_byte_inner(&mut self) -> Result<u8> {
        if let Some(left) = self.remaining {
            if left == 0 {
                return Err(Error::UnexpectedEof { needed: Some(1) });
            }
        }
        let mut byte = [0u8; 1];
        self.src.read_exact(&mut byte).map_err(io_eof)?;
        self.charge(1)?;
        if let Some(left) = &mut self.remaining {
            *left -= 1;
        }
        Ok(byte[0])
    }

    /// Read one unsigned varint (max 10 bytes, 64 bits).
    pub fn read_varint(&mut self) -> Result<u64> {
        let first = self.read_byte_inner()?;
        let mut result: u64 = 0;
        let mut byte = first;
        for shift in 0..10u32 {
            if shift == 9 && (byte & 0x80) != 0 {
                return Err(Error::InvalidVarint);
            }
            result |= ((byte & 0x7f) as u64) << (shift * 7);
            if (byte & 0x80) == 0 {
                return Ok(result);
            }
            byte = self.read_byte_inner()?;
        }
        Err(Error::InvalidVarint)
    }

    /// Read the next field tag. Returns `Ok(None)` at a clean end of
    /// message (bounded boundary reached, or EOF at top level).
    pub fn next_tag(&mut self) -> Result<Option<PositionedTag>> {
        if self.remaining == Some(0) {
            return Ok(None);
        }
        // A clean EOF *before the first tag byte* terminates a top-level
        // message. EOF partway through a tag is truncation and errors.
        let first = match self.read_byte_inner() {
            Ok(b) => b,
            Err(Error::UnexpectedEof { .. }) if self.remaining.is_none() => return Ok(None),
            Err(e) => return Err(e),
        };
        // Re-derive the varint including the already-read first byte.
        let mut result: u64 = (first & 0x7f) as u64;
        if (first & 0x80) == 0 {
            return self.finish_tag(result);
        }
        for shift in 1..10u32 {
            let byte = self.read_byte_inner()?;
            if shift == 9 && (byte & 0x80) != 0 {
                return Err(Error::InvalidVarint);
            }
            result |= ((byte & 0x7f) as u64) << (shift * 7);
            if (byte & 0x80) == 0 {
                return self.finish_tag(result);
            }
        }
        Err(Error::InvalidVarint)
    }

    fn finish_tag(&self, tag: u64) -> Result<Option<PositionedTag>> {
        let (field, wire_type) = decode_tag(tag)?;
        Ok(Some(PositionedTag { field, wire_type }))
    }

    /// Read a fixed 4-byte payload.
    pub fn read_fixed32(&mut self) -> Result<[u8; 4]> {
        let mut buf = [0u8; 4];
        self.read_n(&mut buf)?;
        Ok(buf)
    }

    /// Read a fixed 8-byte payload.
    pub fn read_fixed64(&mut self) -> Result<[u8; 8]> {
        let mut buf = [0u8; 8];
        self.read_n(&mut buf)?;
        Ok(buf)
    }

    fn read_n(&mut self, buf: &mut [u8]) -> Result<()> {
        let n = buf.len() as u64;
        if let Some(left) = self.remaining {
            if n > left {
                return Err(Error::UnexpectedEof {
                    needed: Some((n - left) as usize),
                });
            }
        }
        self.src.read_exact(buf).map_err(io_eof)?;
        self.charge(n)?;
        if let Some(left) = &mut self.remaining {
            *left -= n;
        }
        Ok(())
    }

    /// Read a LEN header: the declared length, validated against the
    /// per-value limit and current boundary. Positioned at first payload.
    pub fn read_len_header(&mut self) -> Result<u64> {
        let len = self.read_varint()?;
        if len > self.limits.max_value_len {
            return Err(Error::LengthExceedsLimit {
                declared: len,
                limit: self.limits.max_value_len,
            });
        }
        if let Some(left) = self.remaining {
            if len > left {
                return Err(Error::LengthOutOfBounds {
                    declared: len,
                    remaining: left,
                });
            }
        }
        Ok(len)
    }

    /// Read exactly `len` LEN payload bytes.
    pub fn read_len_payload(&mut self, len: u64) -> Result<Vec<u8>> {
        let mut buf = vec![0u8; len as usize];
        self.read_n(&mut buf)?;
        Ok(buf)
    }

    /// Read a complete LEN value (header + payload).
    pub fn read_len(&mut self) -> Result<Vec<u8>> {
        let len = self.read_len_header()?;
        self.read_len_payload(len)
    }

    /// Read an embedded message LEN value and return a sub-reader scoped to
    /// exactly those bytes, enforcing the nesting limit. The bytes are
    /// buffered individually (their size is capped by `max_value_len`).
    pub fn read_message(&mut self) -> Result<WireReader<'static>> {
        if self.depth + 1 > self.limits.max_depth {
            return Err(Error::NestingTooDeep {
                limit: self.limits.max_depth,
            });
        }
        let len = self.read_len_header()?;
        let payload = self.read_len_payload(len)?;
        Ok(WireReader::buffered(payload, self.limits, self.depth + 1))
    }

    /// Skip the value of a field after its tag, capturing the raw payload
    /// (everything after the tag) for unknown-field forwarding.
    pub fn skip_value(&mut self, wt: WireType) -> Result<Vec<u8>> {
        match wt {
            WireType::Varint => {
                let mut raw = Vec::with_capacity(4);
                loop {
                    let byte = self.read_byte_inner()?;
                    raw.push(byte);
                    if (byte & 0x80) == 0 {
                        break;
                    }
                }
                Ok(raw)
            }
            WireType::I32 => Ok(self.read_fixed32()?.to_vec()),
            WireType::I64 => Ok(self.read_fixed64()?.to_vec()),
            WireType::Len => {
                // Capture the length prefix verbatim, then the payload.
                let mut prefix = Vec::new();
                loop {
                    let byte = self.read_byte_inner()?;
                    prefix.push(byte);
                    if (byte & 0x80) == 0 {
                        break;
                    }
                }
                let (len, _) = parse_varint(&prefix);
                if len > self.limits.max_value_len {
                    return Err(Error::LengthExceedsLimit {
                        declared: len,
                        limit: self.limits.max_value_len,
                    });
                }
                let payload = self.read_len_payload(len)?;
                prefix.extend_from_slice(&payload);
                Ok(prefix)
            }
        }
    }
}

/// Parse a varint out of captured raw bytes. Returns (value, byte count).
pub fn parse_varint(bytes: &[u8]) -> (u64, usize) {
    let mut result: u64 = 0;
    for (i, byte) in bytes.iter().enumerate().take(10) {
        result |= ((byte & 0x7f) as u64) << (i * 7);
        if (byte & 0x80) == 0 {
            return (result, i + 1);
        }
    }
    (0, 0)
}

// ---------------------------------------------------------------------------
// Envelope
// ---------------------------------------------------------------------------

/// Magic prefix of an envelope: ASCII `BSE1`.
pub const ENVELOPE_MAGIC: [u8; 4] = *b"BSE1";
/// Current envelope/wire format version.
pub const FORMAT_VERSION: u8 = 1;

/// The envelope wraps a top-level message so files are self-describing:
///
/// ```text
/// "BSE1" | version: u8 | flags: u8 | payload_len: u32 LE | payload
/// ```
#[derive(Debug, Clone)]
pub struct Envelope {
    pub version: u8,
    pub flags: u8,
    pub payload: Vec<u8>,
}

/// The validated fixed-size envelope header, positioned before the payload.
#[derive(Debug, Clone, Copy)]
pub struct EnvelopeHeader {
    pub version: u8,
    pub flags: u8,
    pub payload_len: u64,
}

/// Write an envelope around an already-encoded message payload.
pub fn write_envelope<W: Write>(out: &mut W, payload: &[u8]) -> Result<()> {
    let len = u32::try_from(payload.len()).map_err(|_| Error::LengthExceedsLimit {
        declared: payload.len() as u64,
        limit: u32::MAX as u64,
    })?;
    out.write_all(&ENVELOPE_MAGIC)?;
    out.write_all(&[FORMAT_VERSION, 0])?;
    out.write_all(&len.to_le_bytes())?;
    out.write_all(payload)?;
    out.flush()?;
    Ok(())
}

/// Read and validate only the fixed 10-byte envelope header, leaving the
/// source positioned at the first payload byte.
pub fn read_envelope_header(src: &mut dyn Read) -> Result<EnvelopeHeader> {
    let mut magic = [0u8; 4];
    src.read_exact(&mut magic).map_err(|e| match e.kind() {
        std::io::ErrorKind::UnexpectedEof => Error::UnexpectedEof { needed: None },
        _ => Error::Io(e.to_string()),
    })?;
    if magic != ENVELOPE_MAGIC {
        return Err(Error::InvalidInput(
            "not a BSE1 envelope: bad magic bytes".into(),
        ));
    }
    let mut header = [0u8; 2];
    src.read_exact(&mut header).map_err(io_eof)?;
    let version = header[0];
    if version != FORMAT_VERSION {
        return Err(Error::InvalidInput(format!(
            "unsupported format version {version}; supported: {FORMAT_VERSION}"
        )));
    }
    let flags = header[1];
    if flags != 0 {
        return Err(Error::InvalidInput(format!(
            "unknown envelope flags 0x{flags:02x}"
        )));
    }
    let mut len_buf = [0u8; 4];
    src.read_exact(&mut len_buf).map_err(io_eof)?;
    Ok(EnvelopeHeader {
        version,
        flags,
        payload_len: u32::from_le_bytes(len_buf) as u64,
    })
}

/// Read and validate an envelope, buffering its payload. Prefer
/// [`WireReader::from_envelope`] for streaming decodes; this convenience
/// is used by callers that want the raw bytes.
pub fn read_envelope(src: &mut dyn Read, limits: Limits) -> Result<Envelope> {
    let header = read_envelope_header(src)?;
    if header.payload_len > limits.max_output {
        return Err(Error::OutputLimitExceeded {
            limit: limits.max_output,
        });
    }
    let mut payload = vec![0u8; header.payload_len as usize];
    src.read_exact(&mut payload).map_err(io_eof)?;
    Ok(Envelope {
        version: header.version,
        flags: header.flags,
        payload,
    })
}

// ---------------------------------------------------------------------------
// Bounded adapter for streaming top-level decodes
// ---------------------------------------------------------------------------

/// A `Read` adapter that yields at most `limit` bytes from the underlying
/// reader. A read that reaches the limit reports
/// [`std::io::ErrorKind::UnexpectedEof`], so a short/truncated envelope is
/// detected lazily without reading ahead or buffering the whole payload.
struct Limited<R> {
    inner: R,
    remaining: u64,
}

impl<R: Read> Limited<R> {
    fn new(inner: R, limit: u64) -> Self {
        Limited {
            inner,
            remaining: limit,
        }
    }
}

impl<R: Read> Read for Limited<R> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if self.remaining == 0 {
            return Ok(0);
        }
        let max = (buf.len() as u64).min(self.remaining) as usize;
        let n = self.inner.read(&mut buf[..max])?;
        self.remaining = self.remaining.saturating_sub(n as u64);
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn varint_round_trips_known_values() {
        for value in [0u64, 1, 127, 128, 300, 16384, u32::MAX as u64, u64::MAX] {
            let mut buf = Vec::new();
            write_varint(&mut buf, value);
            let mut cursor = std::io::Cursor::new(buf);
            let mut reader = WireReader::new(&mut cursor, Limits::default());
            assert_eq!(reader.read_varint().unwrap(), value, "value {value}");
        }
    }

    #[test]
    fn varint_rejects_11th_continuation_byte() {
        // 10 bytes each with continuation set, then an 11th.
        let bad = vec![0x80u8; 11];
        let mut cursor = std::io::Cursor::new(bad);
        let mut reader = WireReader::new(&mut cursor, Limits::default());
        assert!(matches!(reader.read_varint(), Err(Error::InvalidVarint)));
    }

    #[test]
    fn zigzag_known_vectors() {
        assert_eq!(zigzag_encode32(0), 0);
        assert_eq!(zigzag_encode32(-1), 1);
        assert_eq!(zigzag_encode32(1), 2);
        assert_eq!(zigzag_encode32(-2), 3);
        assert_eq!(zigzag_decode32(zigzag_encode32(i32::MIN)), i32::MIN);
        assert_eq!(zigzag_decode64(zigzag_encode64(i64::MIN)), i64::MIN);
    }

    #[test]
    fn tag_field_zero_is_rejected() {
        // Tag 0 -> field number 0.
        assert!(matches!(decode_tag(0), Err(Error::InvalidFieldNumber(0))));
    }

    #[test]
    fn unknown_wire_type_is_rejected() {
        // field 1, wire type 7.
        assert!(matches!(
            decode_tag((1 << 3) | 7),
            Err(Error::UnknownWireType(7))
        ));
    }

    #[test]
    fn empty_top_level_message_is_clean_eof() {
        let empty: Vec<u8> = Vec::new();
        let mut cursor = std::io::Cursor::new(empty);
        let mut reader = WireReader::new(&mut cursor, Limits::default());
        assert!(reader.next_tag().unwrap().is_none());
    }

    #[test]
    fn truncated_tag_is_an_error() {
        // Continuation bit set but no following byte.
        let bad = vec![0x80u8];
        let mut cursor = std::io::Cursor::new(bad);
        let mut reader = WireReader::new(&mut cursor, Limits::default());
        assert!(reader.next_tag().is_err());
    }

    #[test]
    fn len_header_lying_about_remaining_is_rejected() {
        let mut bytes = Vec::new();
        // field 1, LEN; declare 10 bytes, supply 2.
        write_varint(&mut bytes, encode_tag(1, WireType::Len));
        write_varint(&mut bytes, 10);
        bytes.extend_from_slice(b"ab");
        let mut cursor = std::io::Cursor::new(bytes);
        let mut reader = WireReader::new(&mut cursor, Limits::default());
        let _tag = reader.next_tag().unwrap().unwrap();
        let len = reader.read_len_header().unwrap();
        assert_eq!(len, 10);
        assert!(reader.read_len_payload(len).is_err());
    }

    #[test]
    fn envelope_round_trips_and_rejects_bad_magic() {
        let payload = vec![1u8, 2, 3, 4];
        let mut out = Vec::new();
        write_envelope(&mut out, &payload).unwrap();
        assert_eq!(&out[..4], b"BSE1");

        let mut cursor = std::io::Cursor::new(out);
        let env = read_envelope(&mut cursor, Limits::default()).unwrap();
        assert_eq!(env.payload, payload);

        let mut bad: Vec<u8> = b"XXXX".to_vec();
        bad.extend_from_slice(&[1, 0, 0, 0, 0, 0]);
        let mut cursor = std::io::Cursor::new(bad);
        assert!(read_envelope(&mut cursor, Limits::default()).is_err());
    }
}
