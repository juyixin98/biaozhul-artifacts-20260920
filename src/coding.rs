//! Binary codecs used by the disk format.
//!
//! All multi-byte fixed-width integers are little-endian. Lengths inside blocks
//! are 32-bit varints (as in LevelDB's block format); block handles and the
//! footer use 64-bit varints.
//!
//! Checksums are CRC32C (Castagnoli), stored **masked** so that a block which
//! itself happens to contain an unmasked CRC cannot be confused with its own
//! checksum; the mask follows LevelDB's `Mask(...)`/`Unmask(...)`.

use crate::error::{Error, Result};

// ---------------------------------------------------------------------------
// Fixed-width little-endian helpers
// ---------------------------------------------------------------------------

pub fn put_u16_le(out: &mut Vec<u8>, v: u16) {
    out.extend_from_slice(&v.to_le_bytes());
}

pub fn put_u32_le(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_le_bytes());
}

pub fn put_u64_le(out: &mut Vec<u8>, v: u64) {
    out.extend_from_slice(&v.to_le_bytes());
}

/// Read a little-endian u32 from `buf` (assumed long enough by the caller).
pub fn decode_u32_le(buf: &[u8]) -> u32 {
    u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]])
}

/// Read a little-endian u64 from `buf` (assumed long enough by the caller).
pub fn decode_u64_le(buf: &[u8]) -> u64 {
    u64::from_le_bytes([
        buf[0], buf[1], buf[2], buf[3], buf[4], buf[5], buf[6], buf[7],
    ])
}

// ---------------------------------------------------------------------------
// Varints
// ---------------------------------------------------------------------------

/// Append `value` as a 64-bit varint (at most 10 bytes).
pub fn put_varint64(out: &mut Vec<u8>, mut value: u64) {
    loop {
        let mut b = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            b |= 0x80;
        }
        out.push(b);
        if value == 0 {
            break;
        }
    }
}

/// Append `value` as a 32-bit varint (at most 5 bytes).
pub fn put_varint32(out: &mut Vec<u8>, value: u32) {
    put_varint64(out, value as u64);
}

/// Decode a 64-bit varint starting at `buf[offset]`.
///
/// On success returns `(value, bytes_consumed)`. Overlong encodings (extra
/// all-zero groups) and encodings exceeding 64 bits are rejected.
pub fn decode_varint64(buf: &[u8], offset: usize) -> Result<(u64, usize)> {
    let mut result: u64 = 0;
    let mut shift = 0u32;
    let mut pos = offset;
    loop {
        if pos >= buf.len() {
            return Err(Error::corruption("truncated varint"));
        }
        if shift >= 64 {
            return Err(Error::corruption("varint too long"));
        }
        let b = buf[pos];
        pos += 1;
        let chunk = (b & 0x7f) as u64;
        if shift == 63 && chunk > 1 {
            return Err(Error::corruption("varint overflows 64 bits"));
        }
        result |= chunk << shift;
        if b & 0x80 == 0 {
            // Reject overlong encoding: a final continuation group must carry
            // at least one payload bit.
            if pos - offset > 1 && chunk == 0 {
                return Err(Error::corruption("overlong varint"));
            }
            return Ok((result, pos - offset));
        }
        shift += 7;
    }
}

/// Decode a 32-bit varint; rejects encodings whose value overflows u32.
pub fn decode_varint32(buf: &[u8], offset: usize) -> Result<(u32, usize)> {
    let (v, n) = decode_varint64(buf, offset)?;
    if v > u32::MAX as u64 {
        return Err(Error::corruption("varint overflows 32 bits"));
    }
    Ok((v as u32, n))
}

// ---------------------------------------------------------------------------
// CRC32C (Castagnoli), software table implementation
// ---------------------------------------------------------------------------

const CRC32C_POLY: u32 = 0x82f63b78; // reversed Castagnoli polynomial

struct CrcTable {
    entries: [u32; 256],
}

impl CrcTable {
    fn build() -> CrcTable {
        let mut entries = [0u32; 256];
        for i in 0..256u32 {
            let mut crc = i;
            for _ in 0..8 {
                crc = (crc >> 1) ^ (CRC32C_POLY & ((crc & 1).wrapping_neg()));
            }
            entries[i as usize] = crc;
        }
        CrcTable { entries }
    }
}

fn crc_table() -> &'static [u32; 256] {
    use std::sync::OnceLock;
    static TABLE: OnceLock<CrcTable> = OnceLock::new();
    &TABLE.get_or_init(CrcTable::build).entries
}

/// Compute CRC32C over `data` (initial value 0, no final XOR, matching the
/// LevelDB `crc32c::Value` convention).
pub fn crc32c(data: &[u8]) -> u32 {
    crc32c_extend(0, data)
}

/// Continue a CRC32C computation with `data`.
pub fn crc32c_extend(initial: u32, data: &[u8]) -> u32 {
    let table = crc_table();
    let mut crc = initial;
    for &b in data {
        crc = table[((crc ^ b as u32) & 0xff) as usize] ^ (crc >> 8);
    }
    crc
}

/// Deliberate non-linear mask so a raw CRC embedded in payload bytes cannot be
/// mistaken for the stored checksum.
pub const CRC_MASK_DELTA: u32 = 0xa282_ead8;

pub fn mask_crc(crc: u32) -> u32 {
    crc.rotate_right(15).wrapping_add(CRC_MASK_DELTA)
}

pub fn unmask_crc(masked: u32) -> u32 {
    let rot = masked.wrapping_sub(CRC_MASK_DELTA);
    rot.rotate_left(15)
}

// ---------------------------------------------------------------------------
// Hex helpers (HTTP layer exchanges binary keys/values as hex strings)
// ---------------------------------------------------------------------------

pub fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

pub fn from_hex(s: &str) -> Result<Vec<u8>> {
    if !s.len().is_multiple_of(2) {
        return Err(Error::invalid_argument("hex string has odd length"));
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let bytes = s.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        let hi = hex_digit(bytes[i])?;
        let lo = hex_digit(bytes[i + 1])?;
        out.push((hi << 4) | lo);
        i += 2;
    }
    Ok(out)
}

fn hex_digit(b: u8) -> Result<u8> {
    match b {
        b'0'..=b'9' => Ok(b - b'0'),
        b'a'..=b'f' => Ok(b - b'a' + 10),
        b'A'..=b'F' => Ok(b - b'A' + 10),
        _ => Err(Error::invalid_argument(format!(
            "invalid hex digit: {}",
            b as char
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn varint_roundtrip() {
        for v in [
            0u64,
            1,
            127,
            128,
            16383,
            16384,
            u32::MAX as u64,
            u64::MAX,
            1u64 << 63,
        ] {
            let mut buf = Vec::new();
            put_varint64(&mut buf, v);
            let (got, n) = decode_varint64(&buf, 0).unwrap();
            assert_eq!(got, v);
            assert_eq!(n, buf.len());
        }
    }

    #[test]
    fn varint_rejects_truncated_and_overlong() {
        assert!(decode_varint64(&[0x80], 0).is_err());
        // 0 encoded with an extra zero continuation group.
        assert!(decode_varint64(&[0x80, 0x00], 0).is_err());
        // 10 groups carrying 65 bits.
        assert!(decode_varint64(
            &[0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x02],
            0
        )
        .is_err());
    }

    #[test]
    fn crc32c_known_vectors() {
        // LevelDB convention: init = 0, no final XOR (unlike the classical
        // CRC32 framing).
        assert_eq!(crc32c(b""), 0x0000_0000);
        assert_eq!(crc32c(b"123456789"), 0x58e3_fa20);
        // The classical framed value (init/final 0xffff_ffff) is 0xe306_9283;
        // document the difference so nobody "fixes" the convention.
        assert_ne!(crc32c(b"123456789"), 0xe306_9283);
        // Extending must equal computing over the concatenation.
        let a = crc32c(b"hello ");
        assert_eq!(crc32c_extend(a, b"world"), crc32c(b"hello world"));
    }

    #[test]
    fn crc_mask_roundtrip() {
        for c in [0u32, 1, 0xdead_beef, u32::MAX] {
            assert_eq!(unmask_crc(mask_crc(c)), c);
        }
        // The mask changes the value in the common case.
        assert_ne!(mask_crc(0xdead_beef), 0xdead_beef);
    }

    #[test]
    fn hex_roundtrip() {
        for bytes in [vec![], vec![0x00], vec![0xff, 0x10, 0xab]] {
            assert_eq!(from_hex(&to_hex(&bytes)).unwrap(), bytes);
        }
        assert!(from_hex("zz").is_err());
        assert!(from_hex("abc").is_err());
    }
}
