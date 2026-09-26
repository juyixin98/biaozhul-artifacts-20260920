//! LEB128 unsigned varints and protobuf-style zig-zag coding.
//!
//! Hand-written, no dependencies. A u64 varint is at most 10 bytes; the 10th
//! byte may carry only one bit of payload.

use std::io::{self, Read, Write};

use crate::error::{Error, Result};

/// Read an unsigned LEB128 varint. Returns `(value, bytes_consumed)`.
///
/// A clean EOF before the first byte is reported by the caller via
/// [`Read::read_exact`] semantics; a varint longer than 10 bytes or a 10th
/// byte with out-of-range payload is a wire error.
pub fn read_uvarint<R: Read>(r: &mut R) -> Result<(u64, usize)> {
    let mut x: u64 = 0;
    for i in 0..10u32 {
        let b = read_byte(r)?;
        if i == 9 && b > 1 {
            return Err(Error::Wire("varint overflow (u64)".into()));
        }
        x |= u64::from(b & 0x7f) << (7 * i);
        if b & 0x80 == 0 {
            return Ok((x, (i + 1) as usize));
        }
    }
    Err(Error::Wire("varint longer than 10 bytes".into()))
}

/// Continue reading a varint whose first byte has already been consumed.
/// Returns `(value, total_bytes_including_first)`.
pub fn continue_uvarint<R: Read>(r: &mut R, first: u8) -> Result<(u64, usize)> {
    let mut x = u64::from(first & 0x7f);
    if first & 0x80 == 0 {
        return Ok((x, 1));
    }
    for i in 1..10u32 {
        let b = read_byte(r)?;
        if i == 9 && b > 1 {
            return Err(Error::Wire("varint overflow (u64)".into()));
        }
        x |= u64::from(b & 0x7f) << (7 * i);
        if b & 0x80 == 0 {
            return Ok((x, (i + 1) as usize));
        }
    }
    Err(Error::Wire("varint longer than 10 bytes".into()))
}

/// Write an unsigned LEB128 varint; returns the number of bytes written.
pub fn write_uvarint<W: Write>(w: &mut W, mut v: u64) -> io::Result<usize> {
    let mut n = 0;
    loop {
        let mut b = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            b |= 0x80;
        }
        w.write_all(&[b])?;
        n += 1;
        if v == 0 {
            break;
        }
    }
    Ok(n)
}

/// Number of bytes an encoded uvarint occupies.
pub fn uvarint_len(mut v: u64) -> usize {
    let mut n = 1;
    while v >= 0x80 {
        v >>= 7;
        n += 1;
    }
    n
}

/// Zig-zag encode a signed integer: `0,-1,1,-2,2,... -> 0,1,2,3,4,...`
pub fn zigzag_encode(i: i64) -> u64 {
    ((i as u64) << 1) ^ ((i >> 63) as u64)
}

/// Inverse of [`zigzag_encode`].
pub fn zigzag_decode(v: u64) -> i64 {
    (v >> 1) as i64 ^ -((v & 1) as i64)
}

fn read_byte<R: Read>(r: &mut R) -> Result<u8> {
    let mut b = 0u8;
    r.read_exact(std::slice::from_mut(&mut b))?;
    Ok(b)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn roundtrip_unsigned() {
        for v in [0u64, 1, 127, 128, 16383, 16384, u64::MAX, u64::MAX - 1] {
            let mut buf = Vec::new();
            write_uvarint(&mut buf, v).unwrap();
            assert_eq!(uvarint_len(v), buf.len());
            let (got, n) = read_uvarint(&mut Cursor::new(&buf)).unwrap();
            assert_eq!(got, v);
            assert_eq!(n, buf.len());
        }
    }

    #[test]
    fn roundtrip_signed() {
        for v in [0i64, -1, 1, -2, 2, -63, 63, -64, 64, i64::MIN, i64::MAX] {
            let z = zigzag_encode(v);
            assert_eq!(zigzag_decode(z), v);
        }
    }

    #[test]
    fn rejects_overflow() {
        // 10 bytes, last byte carrying 2 bits.
        let bad = [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x02];
        assert!(read_uvarint(&mut Cursor::new(bad)).is_err());
    }

    #[test]
    fn rejects_truncated() {
        let bad = [0x80u8, 0x80];
        assert!(read_uvarint(&mut Cursor::new(bad)).is_err());
    }
}
