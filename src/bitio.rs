//! LSB-first bit-level I/O over byte slices.
//!
//! Bit order: values are written least-significant-bit first into the byte
//! stream; the first bit of a value lands in the lowest free bit of the
//! current byte. Bytes themselves are stored in stream order (the stream is
//! little-endian at every level).

use crate::error::{Error, Result};

/// Maximum bit width supported by the format.
pub const MAX_BIT_WIDTH: u32 = 64;

/// Accumulates integers of 0..=64 bits into a byte buffer, LSB-first.
#[derive(Default)]
pub struct BitWriter {
    buf: Vec<u8>,
    /// Bits already used in the last partial byte (0 means byte-aligned).
    bit_pos: u32,
}

impl BitWriter {
    pub fn new() -> Self {
        BitWriter::default()
    }

    /// Append the low `width` bits of `value`. `width` must be <= 64 and,
    /// unless `width == 64`, `value` must fit in `width` bits.
    pub fn write_bits(&mut self, value: u64, width: u32) {
        debug_assert!(width <= MAX_BIT_WIDTH);
        debug_assert!(width == MAX_BIT_WIDTH || value >> width == 0);
        let mut remaining = width;
        let mut v = value;
        while remaining > 0 {
            if self.bit_pos == 0 {
                self.buf.push(0);
            }
            let take = (8 - self.bit_pos).min(remaining); // 1..=8
            let mask = (1u64 << take) - 1; // take <= 8, never overflows
            let last = self.buf.last_mut().expect("byte just pushed");
            *last |= ((v & mask) as u8) << self.bit_pos;
            self.bit_pos = (self.bit_pos + take) % 8;
            v >>= take;
            remaining -= take;
        }
    }

    /// Number of bytes written so far (final partial byte included).
    pub fn byte_len(&self) -> usize {
        self.buf.len()
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.buf
    }
}

/// Reads integers of 0..=64 bits from a byte slice, LSB-first.
pub struct BitReader<'a> {
    data: &'a [u8],
    byte_pos: usize,
    /// Bits already consumed in the current byte (0 means byte-aligned).
    bit_pos: u32,
}

impl<'a> BitReader<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        BitReader {
            data,
            byte_pos: 0,
            bit_pos: 0,
        }
    }

    /// Read `width` bits (0..=64) as an unsigned value.
    ///
    /// Returns [`Error::InvalidBitWidth`] for widths above 64 and
    /// [`Error::UnexpectedEof`] when the slice runs out; it never shifts by
    /// more than 63 bits, so hostile widths cannot cause shift overflow.
    pub fn read_bits(&mut self, width: u32) -> Result<u64> {
        if width > MAX_BIT_WIDTH {
            return Err(Error::InvalidBitWidth(width));
        }
        let mut result: u64 = 0;
        let mut filled: u32 = 0;
        let mut remaining = width;
        while remaining > 0 {
            if self.byte_pos >= self.data.len() {
                return Err(Error::UnexpectedEof);
            }
            let take = (8 - self.bit_pos).min(remaining); // 1..=8
            let bits = (self.data[self.byte_pos] >> self.bit_pos) as u64 & ((1u64 << take) - 1);
            // filled + take <= width <= 64, and take >= 1 while remaining > 0,
            // so filled <= 63 here: the shift cannot overflow.
            result |= bits << filled;
            filled += take;
            self.bit_pos += take;
            if self.bit_pos == 8 {
                self.bit_pos = 0;
                self.byte_pos += 1;
            }
            remaining -= take;
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(values: &[(u64, u32)]) {
        let mut w = BitWriter::new();
        for &(v, width) in values {
            w.write_bits(v, width);
        }
        let bytes = w.into_bytes();
        let mut r = BitReader::new(&bytes);
        for &(v, width) in values {
            assert_eq!(r.read_bits(width).unwrap(), v, "width {width}");
        }
    }

    #[test]
    fn mixed_widths() {
        roundtrip(&[(0, 0), (1, 1), (0b101, 3), (u64::MAX, 64), (7, 3), (0, 64)]);
    }

    #[test]
    fn boundary_widths() {
        roundtrip(&[(0, 0), (u64::MAX, 64), (1, 63), ((1 << 63) | 1, 64)]);
    }

    #[test]
    fn rejects_width_above_64() {
        let data = [0xffu8; 16];
        let mut r = BitReader::new(&data);
        assert!(matches!(r.read_bits(65), Err(Error::InvalidBitWidth(65))));
        assert!(matches!(r.read_bits(u32::MAX), Err(Error::InvalidBitWidth(_))));
    }

    #[test]
    fn eof_is_an_error_not_a_panic() {
        let data = [0u8; 1];
        let mut r = BitReader::new(&data);
        assert!(matches!(r.read_bits(64), Err(Error::UnexpectedEof)));
    }
}
