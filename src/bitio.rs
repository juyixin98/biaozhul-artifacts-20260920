//! LSB-first bit stream primitives.
//!
//! Bits are packed least-significant-bit first: the first bit written lands in
//! bit 0 of byte 0, the ninth bit lands in bit 0 of byte 1, and so on. A value
//! occupying `n` bits is stored with its least significant bit first.
//!
//! Both sides use a 128-bit accumulator so that reading or writing a full
//! 64-bit value never requires a shift by 64 (or more) on a 64-bit word.

use crate::error::Error;

/// Appends fixed-width values to a byte stream, LSB-first.
#[derive(Default)]
pub struct BitWriter {
    buf: u128,
    nbits: u32,
    out: Vec<u8>,
}

impl BitWriter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Append the low `bits` bits of `value`. `bits` must be in 0..=64.
    pub fn push(&mut self, value: u64, bits: u32) {
        debug_assert!(bits <= 64);
        let masked = if bits == 64 {
            value
        } else {
            value & ((1u64 << bits) - 1)
        };
        self.buf |= (masked as u128) << self.nbits;
        self.nbits += bits;
        while self.nbits >= 8 {
            self.out.push(self.buf as u8);
            self.buf >>= 8;
            self.nbits -= 8;
        }
    }

    /// Flush, zero-padding the final partial byte.
    pub fn finish(mut self) -> Vec<u8> {
        if self.nbits > 0 {
            self.out.push(self.buf as u8);
        }
        self.out
    }
}

/// Reads fixed-width values from a byte slice, LSB-first.
pub struct BitReader<'a> {
    data: &'a [u8],
    pos: usize,
    buf: u128,
    nbits: u32,
}

impl<'a> BitReader<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            pos: 0,
            buf: 0,
            nbits: 0,
        }
    }

    /// Read `bits` bits (0..=64). Returns [`Error::Truncated`] if the slice
    /// does not contain enough whole bytes.
    pub fn read(&mut self, bits: u32) -> Result<u64, Error> {
        debug_assert!(bits <= 64);
        while self.nbits < bits {
            let byte = *self.data.get(self.pos).ok_or(Error::Truncated)?;
            self.pos += 1;
            self.buf |= (byte as u128) << self.nbits;
            self.nbits += 8;
        }
        let mask = if bits == 64 {
            u128::from(u64::MAX)
        } else {
            (1u128 << bits) - 1
        };
        let value = (self.buf & mask) as u64;
        self.buf >>= bits;
        self.nbits -= bits;
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(values: &[(u64, u32)]) {
        let mut w = BitWriter::new();
        for &(v, bits) in values {
            w.push(v, bits);
        }
        let bytes = w.finish();
        let mut r = BitReader::new(&bytes);
        for &(v, bits) in values {
            let want = if bits == 64 { v } else { v & ((1u64 << bits) - 1) };
            assert_eq!(r.read(bits).unwrap(), want, "value {v:#x} at {bits} bits");
        }
    }

    #[test]
    fn mixed_widths() {
        roundtrip(&[(1, 1), (0, 3), (0xabcd, 16), (7, 3), (0, 0), (u64::MAX, 64), (5, 9)]);
    }

    #[test]
    fn full_width_64() {
        roundtrip(&[(u64::MAX, 64), (0, 64), (0x0123_4567_89ab_cdef, 64)]);
    }

    #[test]
    fn zero_width_only() {
        let mut w = BitWriter::new();
        for _ in 0..10 {
            w.push(0, 0);
        }
        assert!(w.finish().is_empty());
    }

    #[test]
    fn reader_reports_truncation() {
        let mut r = BitReader::new(&[0xff]);
        assert!(matches!(r.read(16), Err(Error::Truncated)));
    }
}
