//! Unsigned/signed LEB128 ("varint") primitives used by the binary format.
//!
//! See `docs/FORMAT.md` §2. Values are little-endian base-128; the high bit of
//! each byte marks a continuation. Signed integers use zig-zag encoding so that
//! small magnitudes (including negatives) stay short.

use crate::error::{Error, Result};
use std::io::{Read, Write};

/// Encode `value` as unsigned LEB128, appending to `out`.
pub fn write_uvarint(out: &mut Vec<u8>, mut value: u64) {
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

/// Encode `value` as zig-zag signed LEB128.
pub fn write_ivarint(out: &mut Vec<u8>, value: i64) {
    let zig = ((value << 1) ^ (value >> 63)) as u64;
    write_uvarint(out, zig);
}

/// Read an unsigned LEB128 from `data[*pos..]`.
///
/// Returns an error on truncation or when more than 10 bytes are consumed
/// (u64 cannot use more).
pub fn read_uvarint(data: &[u8], pos: &mut usize) -> Result<u64> {
    let mut result: u64 = 0;
    let mut shift = 0u32;
    for _ in 0..10 {
        if *pos >= data.len() {
            return Err(Error::truncated("varint"));
        }
        let byte = data[*pos];
        *pos += 1;
        let chunk = (byte & 0x7f) as u64;
        if shift >= 64 || (shift == 63 && chunk > 1) {
            return Err(Error::invalid("varint exceeds u64"));
        }
        result |= chunk << shift;
        if byte & 0x80 == 0 {
            return Ok(result);
        }
        shift += 7;
    }
    Err(Error::invalid("varint too long"))
}

/// Read a zig-zag signed LEB128.
pub fn read_ivarint(data: &[u8], pos: &mut usize) -> Result<i64> {
    let zig = read_uvarint(data, pos)?;
    Ok(((zig >> 1) as i64) ^ -((zig & 1) as i64))
}

/// Streaming variant of [`write_uvarint`]: write directly to a [`Write`]
/// without buffering the whole value.
pub fn write_uvarint_to<W: Write + ?Sized>(w: &mut W, mut value: u64) -> std::io::Result<()> {
    let mut buf = [0u8; 10];
    let mut n = 0;
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        buf[n] = byte;
        n += 1;
        if value == 0 {
            break;
        }
    }
    w.write_all(&buf[..n])
}

/// Streaming variant of [`write_ivarint`].
pub fn write_ivarint_to<W: Write + ?Sized>(w: &mut W, value: i64) -> std::io::Result<()> {
    let zig = ((value << 1) ^ (value >> 63)) as u64;
    write_uvarint_to(w, zig)
}

/// Streaming variant of [`read_uvarint`]: pull bytes one at a time from `r`.
///
/// Returns [`Error::Truncated`] on clean EOF mid-value and [`Error::Invalid`]
/// if the encoding exceeds u64.
pub fn read_uvarint_from<R: Read + ?Sized>(r: &mut R) -> Result<u64> {
    let mut result: u64 = 0;
    let mut shift = 0u32;
    let mut byte = [0u8; 1];
    for _ in 0..10 {
        match r.read(&mut byte) {
            Ok(0) => return Err(Error::truncated("varint")),
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(Error::io(e)),
        }
        let chunk = (byte[0] & 0x7f) as u64;
        if shift >= 64 || (shift == 63 && chunk > 1) {
            return Err(Error::invalid("varint exceeds u64"));
        }
        result |= chunk << shift;
        if byte[0] & 0x80 == 0 {
            return Ok(result);
        }
        shift += 7;
    }
    Err(Error::invalid("varint too long"))
}

/// Streaming variant of [`read_ivarint`].
pub fn read_ivarint_from<R: Read + ?Sized>(r: &mut R) -> Result<i64> {
    let zig = read_uvarint_from(r)?;
    Ok(((zig >> 1) as i64) ^ -((zig & 1) as i64))
}

/// Read exactly `len` bytes into a fresh `Vec`, failing fast when the stream
/// ends first.
pub fn read_vec_exact<R: Read + ?Sized>(r: &mut R, len: usize) -> Result<Vec<u8>> {
    let mut buf = vec![0u8; len];
    read_exact_or_truncated(r, &mut buf)?;
    Ok(buf)
}

/// Like [`Read::read_exact`] but maps clean EOF (even at the start) to
/// [`Error::Truncated`].
pub fn read_exact_or_truncated<R: Read + ?Sized>(r: &mut R, buf: &mut [u8]) -> Result<()> {
    match r.read_exact(buf) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
            Err(Error::truncated("byte blob"))
        }
        Err(e) => Err(Error::io(e)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_unsigned() {
        for v in [0u64, 1, 127, 128, 16383, 16384, u32::MAX as u64, u64::MAX] {
            let mut buf = Vec::new();
            write_uvarint(&mut buf, v);
            let mut pos = 0;
            assert_eq!(read_uvarint(&buf, &mut pos).unwrap(), v);
            assert_eq!(pos, buf.len());
        }
    }

    #[test]
    fn roundtrip_signed() {
        for v in [
            0i64,
            -1,
            1,
            -2,
            2,
            -63,
            63,
            -64,
            64,
            i32::MIN as i64,
            i64::MIN,
            i64::MAX,
        ] {
            let mut buf = Vec::new();
            write_ivarint(&mut buf, v);
            let mut pos = 0;
            assert_eq!(read_ivarint(&buf, &mut pos).unwrap(), v);
            assert_eq!(pos, buf.len());
        }
    }

    #[test]
    fn truncated_is_error() {
        let buf = [0x80u8]; // continuation with no follow-up
        let mut pos = 0;
        assert!(read_uvarint(&buf, &mut pos).is_err());
    }

    #[test]
    fn overlong_is_error() {
        let buf = [0x80u8; 11];
        let mut pos = 0;
        assert!(read_uvarint(&buf, &mut pos).is_err());
    }

    #[test]
    fn stream_matches_slice_roundtrip() {
        for v in [0u64, 1, 127, 128, 16383, 16384, 1234567890123, u64::MAX] {
            let mut buf = Vec::new();
            write_uvarint_to(&mut buf, v).unwrap();
            let decoded = read_uvarint_from(&mut &buf[..]).unwrap();
            assert_eq!(decoded, v);
        }
    }

    #[test]
    fn stream_eof_is_truncated() {
        let buf = [0x80u8];
        let err = read_uvarint_from(&mut &buf[..]).unwrap_err();
        assert!(matches!(err, Error::Truncated(_)));
    }
}
