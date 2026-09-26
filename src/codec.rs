//! Low-level binary primitives shared by every encoding layer.
//!
//! All integers use little-endian byte order. Lengths use canonical
//! unsigned LEB128 ("varint"): 7 payload bits per byte, high bit set when
//! more bytes follow, at most 10 bytes for a `u64`.
//!
//! Two front ends are provided:
//!
//! * [`Writer`] / [`Reader`] operate on a growable buffer / a borrowed byte
//!   slice and never touch `std::io`.
//! * [`IoEncoder`] / [`IoDecoder`] wrap any [`std::io::Write`] /
//!   [`std::io::Read`]. Decoding pulls exactly the bytes a self-delimiting
//!   stream asks for — a declared length is always validated against the
//!   configured limit *before* its payload is buffered, so a malicious
//!   length can never force a huge allocation.

use crate::error::{RbError, RbResult};

/// Maximum number of bytes a canonical u64 varint may occupy.
pub const MAX_VARINT_LEN: usize = 10;

/// Encode `value` as LEB128 into `out`.
pub fn write_varint(out: &mut Vec<u8>, mut value: u64) {
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
}

/// Append the LEB128 encoding of `value` to any [`std::io::Write`].
pub fn io_write_varint<W: std::io::Write + ?Sized>(
    w: &mut W,
    mut value: u64,
) -> std::io::Result<()> {
    let mut buf = [0u8; MAX_VARINT_LEN];
    let mut len = 0;
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        buf[len] = byte;
        len += 1;
        if value == 0 {
            break;
        }
    }
    w.write_all(&buf[..len])
}

/// Read a varint from a byte slice, advancing `pos`.
///
/// Returns [`RbError::UnexpectedEof`] on truncation and
/// [`RbError::InvalidInput`] on non-canonical or overlong encodings.
pub fn read_varint(buf: &[u8], pos: &mut usize) -> RbResult<u64> {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    for i in 0..MAX_VARINT_LEN {
        if *pos >= buf.len() {
            return Err(RbError::UnexpectedEof {
                what: "varint",
                needed: 1,
                available: 0,
            });
        }
        let byte = buf[*pos];
        *pos += 1;
        let payload = u64::from(byte & 0x7f);
        if shift >= 64 {
            return Err(RbError::InvalidInput("varint too long".into()));
        }
        if i == MAX_VARINT_LEN - 1 {
            // 10th byte may only carry the single remaining bit.
            if payload > 1 {
                return Err(RbError::InvalidInput("varint overflow".into()));
            }
        }
        result |= payload
            .checked_shl(shift)
            .ok_or_else(|| RbError::InvalidInput("varint overflow".into()))?;
        if byte & 0x80 == 0 {
            // Canonicality: a multi-byte group must not end on a byte whose
            // payload (and all prior payload) is zero in a way that allowed
            // a shorter encoding — the last byte itself must be nonzero.
            if i > 0 && payload == 0 {
                return Err(RbError::InvalidInput("non-canonical varint".into()));
            }
            return Ok(result);
        }
        shift += 7;
    }
    Err(RbError::InvalidInput("varint too long".into()))
}

/// Read a varint from any [`std::io::Read`], one byte at a time.
pub fn io_read_varint<R: std::io::Read + ?Sized>(r: &mut R) -> RbResult<u64> {
    let mut result: u64 = 0;
    for i in 0..MAX_VARINT_LEN {
        let mut byte = [0u8; 1];
        r.read_exact(&mut byte).map_err(|e| {
            if e.kind() == std::io::ErrorKind::UnexpectedEof {
                RbError::UnexpectedEof {
                    what: "varint",
                    needed: 1,
                    available: 0,
                }
            } else {
                RbError::Other(e.to_string())
            }
        })?;
        let byte = byte[0];
        let payload = u64::from(byte & 0x7f);
        if i == MAX_VARINT_LEN - 1 && payload > 1 {
            return Err(RbError::InvalidInput("varint overflow".into()));
        }
        result |= payload
            .checked_shl(i as u32 * 7)
            .ok_or_else(|| RbError::InvalidInput("varint overflow".into()))?;
        if byte & 0x80 == 0 {
            if i > 0 && payload == 0 {
                return Err(RbError::InvalidInput("non-canonical varint".into()));
            }
            return Ok(result);
        }
    }
    Err(RbError::InvalidInput("varint too long".into()))
}

/// Growable byte sink with little-endian helpers.
#[derive(Debug, Default)]
pub struct Writer {
    buf: Vec<u8>,
}

impl Writer {
    /// New, empty writer.
    pub fn new() -> Self {
        Writer { buf: Vec::new() }
    }

    /// Pre-allocate capacity hint.
    pub fn with_capacity(cap: usize) -> Self {
        Writer {
            buf: Vec::with_capacity(cap),
        }
    }

    /// Write a raw byte.
    pub fn u8(&mut self, v: u8) {
        self.buf.push(v);
    }

    /// Write a u16 little-endian.
    pub fn u16(&mut self, v: u16) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }

    /// Write a u32 little-endian.
    pub fn u32(&mut self, v: u32) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }

    /// Write a u64 little-endian.
    pub fn u64(&mut self, v: u64) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }

    /// Write raw bytes.
    pub fn bytes(&mut self, b: &[u8]) {
        self.buf.extend_from_slice(b);
    }

    /// Write a LEB128 length.
    pub fn len_varint(&mut self, v: u64) {
        write_varint(&mut self.buf, v);
    }

    /// Consume the writer and return the assembled bytes.
    pub fn into_bytes(self) -> Vec<u8> {
        self.buf
    }

    /// Current encoded length in bytes.
    pub fn byte_len(&self) -> usize {
        self.buf.len()
    }
}

/// Borrowed, position-tracking byte reader with bounds-checked helpers.
#[derive(Debug)]
pub struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    /// Wrap a byte slice.
    pub fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }

    /// Bytes consumed so far.
    pub fn position(&self) -> usize {
        self.pos
    }

    /// Bytes still unread.
    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    fn take(&mut self, n: usize, what: &'static str) -> RbResult<&'a [u8]> {
        if self.remaining() < n {
            return Err(RbError::UnexpectedEof {
                what,
                needed: n as u64,
                available: self.remaining() as u64,
            });
        }
        let slice = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(slice)
    }

    /// Read a raw byte.
    pub fn u8(&mut self) -> RbResult<u8> {
        Ok(self.take(1, "u8")?[0])
    }

    /// Read a u16 little-endian.
    pub fn u16(&mut self) -> RbResult<u16> {
        let s = self.take(2, "u16")?;
        Ok(u16::from_le_bytes([s[0], s[1]]))
    }

    /// Read a u32 little-endian.
    pub fn u32(&mut self) -> RbResult<u32> {
        let s = self.take(4, "u32")?;
        Ok(u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
    }

    /// Read a u64 little-endian.
    pub fn u64(&mut self) -> RbResult<u64> {
        let s = self.take(8, "u64")?;
        Ok(u64::from_le_bytes([
            s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7],
        ]))
    }

    /// Read `n` raw bytes.
    pub fn take_n(&mut self, n: usize, what: &'static str) -> RbResult<&'a [u8]> {
        self.take(n, what)
    }

    /// Read a LEB128 length and reject it up front when it exceeds `limit`.
    ///
    /// `what` names the length for error reporting ("array element count",
    /// "bitmap run word count", …). Crucially the caller can use the
    /// returned value to size its next `take_n` — oversized declarations
    /// never turn into oversized allocations.
    pub fn bounded_varint(&mut self, limit: u64, what: &'static str) -> RbResult<u64> {
        let v = read_varint(self.buf, &mut self.pos)?;
        if v > limit {
            return Err(RbError::LengthExceeded {
                what,
                declared: v,
                limit,
            });
        }
        Ok(v)
    }
}

/// Streaming encoder over any [`std::io::Write`].
pub struct IoEncoder<W: std::io::Write> {
    inner: W,
}

impl<W: std::io::Write> IoEncoder<W> {
    /// Wrap a writer.
    pub fn new(inner: W) -> Self {
        IoEncoder { inner }
    }

    /// Write a raw byte.
    pub fn u8(&mut self, v: u8) -> RbResult<()> {
        self.inner
            .write_all(&[v])
            .map_err(|e| RbError::Other(e.to_string()))
    }

    /// Write a u16 little-endian.
    pub fn u16(&mut self, v: u16) -> RbResult<()> {
        self.inner
            .write_all(&v.to_le_bytes())
            .map_err(|e| RbError::Other(e.to_string()))
    }

    /// Write a u32 little-endian.
    pub fn u32(&mut self, v: u32) -> RbResult<()> {
        self.inner
            .write_all(&v.to_le_bytes())
            .map_err(|e| RbError::Other(e.to_string()))
    }

    /// Write raw bytes.
    pub fn bytes(&mut self, b: &[u8]) -> RbResult<()> {
        self.inner
            .write_all(b)
            .map_err(|e| RbError::Other(e.to_string()))
    }

    /// Write a LEB128 length.
    pub fn len_varint(&mut self, v: u64) -> RbResult<()> {
        io_write_varint(&mut self.inner, v).map_err(|e| RbError::Other(e.to_string()))
    }

    /// Flush the underlying writer.
    pub fn flush(&mut self) -> RbResult<()> {
        self.inner
            .flush()
            .map_err(|e| RbError::Other(e.to_string()))
    }

    /// Return the wrapped writer.
    pub fn into_inner(self) -> W {
        self.inner
    }
}

/// Streaming decoder over any [`std::io::Read`].
///
/// Fixed-width reads go through a tiny stack buffer; variable-length
/// payloads are the caller's responsibility to bound via
/// [`IoDecoder::bounded_varint`] before requesting exactly that many bytes.
pub struct IoDecoder<R: std::io::Read> {
    inner: R,
}

impl<R: std::io::Read> IoDecoder<R> {
    /// Wrap a reader.
    pub fn new(inner: R) -> Self {
        IoDecoder { inner }
    }

    fn read_exact(&mut self, buf: &mut [u8], what: &'static str) -> RbResult<()> {
        self.inner.read_exact(buf).map_err(|e| {
            if e.kind() == std::io::ErrorKind::UnexpectedEof {
                RbError::UnexpectedEof {
                    what,
                    needed: buf.len() as u64,
                    available: 0,
                }
            } else {
                RbError::Other(e.to_string())
            }
        })
    }

    /// Read a raw byte.
    pub fn u8(&mut self) -> RbResult<u8> {
        let mut b = [0u8; 1];
        self.read_exact(&mut b, "u8")?;
        Ok(b[0])
    }

    /// Read a u16 little-endian.
    pub fn u16(&mut self) -> RbResult<u16> {
        let mut b = [0u8; 2];
        self.read_exact(&mut b, "u16")?;
        Ok(u16::from_le_bytes(b))
    }

    /// Read a u32 little-endian.
    pub fn u32(&mut self) -> RbResult<u32> {
        let mut b = [0u8; 4];
        self.read_exact(&mut b, "u32")?;
        Ok(u32::from_le_bytes(b))
    }

    /// Read exactly `dst.len()` bytes into `dst`.
    pub fn take_into(&mut self, dst: &mut [u8], what: &'static str) -> RbResult<()> {
        self.read_exact(dst, what)
    }

    /// Read a LEB128 length and reject it when it exceeds `limit`, before
    /// any payload-sized allocation can happen.
    pub fn bounded_varint(&mut self, limit: u64, what: &'static str) -> RbResult<u64> {
        let v = io_read_varint(&mut self.inner)?;
        if v > limit {
            return Err(RbError::LengthExceeded {
                what,
                declared: v,
                limit,
            });
        }
        Ok(v)
    }
}
