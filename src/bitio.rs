//! Bit-level I/O primitives.
//!
//! Bits are packed MSB-first into bytes. Integer deltas are stored with
//! unsigned LEB128 varints *at the bit level* (no byte alignment between
//! fields), which is what makes the tag-per-point encoding below compact:
//! a 1-bit tag can sit directly next to a varint without padding.

/// Writes individual bits and varints into a byte buffer, MSB-first.
#[derive(Debug, Default, Clone)]
pub struct BitWriter {
    buf: Vec<u8>,
    /// Accumulated bits waiting to be flushed, left-aligned in the byte.
    current: u8,
    /// Number of valid bits in `current` (0..=7).
    nbits: u32,
}

impl BitWriter {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_capacity(cap: usize) -> Self {
        Self {
            buf: Vec::with_capacity(cap),
            current: 0,
            nbits: 0,
        }
    }

    /// Append the low `nbits` bits of `value` (0 <= nbits <= 64).
    /// Bits are emitted most-significant first.
    pub fn write_bits(&mut self, mut value: u64, mut nbits: u32) {
        debug_assert!(nbits <= 64);
        while nbits > 0 {
            // Space available in the current accumulator byte.
            let space = 8 - self.nbits;
            let take = nbits.min(space);
            // Extract the next `take` high bits of the remaining value.
            let shift = nbits - take;
            let part = if shift >= 64 {
                0
            } else {
                ((value >> shift) & ((1u64 << take) - 1)) as u8
            };
            self.current |= part << (space - take);
            self.nbits += take;
            nbits -= take;
            // Mask off the bits we consumed, handling shift == 64.
            value = if nbits == 0 || shift >= 64 {
                0
            } else {
                value & ((1u64 << nbits) - 1)
            };
            if self.nbits == 8 {
                self.buf.push(self.current);
                self.current = 0;
                self.nbits = 0;
            }
        }
    }

    /// Write a single bit.
    pub fn write_bit(&mut self, bit: bool) {
        self.write_bits(bit as u64, 1);
    }

    /// Write `value` as exactly 64 bits.
    pub fn write_u64(&mut self, value: u64) {
        self.write_bits(value, 64);
    }

    /// Unsigned LEB128 at the bit level: 7 payload bits per chunk, high bit
    /// of each chunk marks "more chunks follow".
    pub fn write_varint_u64(&mut self, mut value: u64) {
        loop {
            let chunk = (value & 0x7f) as u8;
            value >>= 7;
            let more = value != 0;
            self.write_bit(more);
            self.write_bits(chunk as u64, 7);
            if !more {
                break;
            }
        }
    }

    /// Flush the accumulator, padding the final byte with zero bits.
    pub fn finish(mut self) -> Vec<u8> {
        if self.nbits > 0 {
            self.buf.push(self.current);
        }
        self.buf
    }

    /// Total number of payload bits written so far.
    pub fn bit_len(&self) -> usize {
        self.buf.len() * 8 + self.nbits as usize
    }
}

/// Reads individual bits and varints from a byte buffer, MSB-first.
#[derive(Debug, Clone)]
pub struct BitReader<'a> {
    buf: &'a [u8],
    pos: usize, // byte index
    nbits: u32, // consumed bits within the current byte (0..=7)
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReadError {
    UnexpectedEof,
}

impl std::fmt::Display for ReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "unexpected end of bit stream")
    }
}

impl std::error::Error for ReadError {}

impl<'a> BitReader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Self {
            buf,
            pos: 0,
            nbits: 0,
        }
    }

    /// Read a single bit.
    pub fn read_bit(&mut self) -> Result<bool, ReadError> {
        if self.pos >= self.buf.len() {
            return Err(ReadError::UnexpectedEof);
        }
        let bit = (self.buf[self.pos] >> (7 - self.nbits)) & 1;
        self.nbits += 1;
        if self.nbits == 8 {
            self.pos += 1;
            self.nbits = 0;
        }
        Ok(bit == 1)
    }

    /// Read exactly `nbits` bits (0 <= nbits <= 64).
    pub fn read_bits(&mut self, nbits: u32) -> Result<u64, ReadError> {
        debug_assert!(nbits <= 64);
        let mut value: u64 = 0;
        for _ in 0..nbits {
            value = (value << 1) | (self.read_bit()? as u64);
        }
        Ok(value)
    }

    /// Read exactly 64 bits.
    pub fn read_u64(&mut self) -> Result<u64, ReadError> {
        self.read_bits(64)
    }

    /// Inverse of [`BitWriter::write_varint_u64`]. A malformed stream with
    /// more than ceil(64/7)=10 continuation chunks fails with Eof (or, if the
    /// payload overflows 64 bits, returns [`ReadError::UnexpectedEof`] after
    /// masking via `wrapping_shl`).
    pub fn read_varint_u64(&mut self) -> Result<u64, ReadError> {
        let mut result: u64 = 0;
        let mut shift = 0u32;
        loop {
            let more = self.read_bit()?;
            let chunk = self.read_bits(7)?;
            if shift >= 64 {
                // >10 chunks: value cannot fit in u64.
                return Err(ReadError::UnexpectedEof);
            }
            result |= chunk << shift;
            shift += 7;
            if !more {
                break;
            }
        }
        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_packing_roundtrip() {
        let mut w = BitWriter::new();
        let bits: Vec<(u64, u32)> = vec![(1, 1), (0, 1), (7, 3), (0b1010, 4), (0, 1), (3, 2)];
        for (v, n) in &bits {
            w.write_bits(*v, *n);
        }
        let bytes = w.finish();
        let mut r = BitReader::new(&bytes);
        for (v, n) in &bits {
            assert_eq!(r.read_bits(*n).unwrap(), *v);
        }
    }

    #[test]
    fn varint_roundtrip_known_values() {
        // Canonical unsigned LEB128 encodings.
        for (value, expected) in [
            (0u64, vec![0b0000_0000]),
            (1, vec![0b0000_0001]),
            (127, vec![0b0111_1111]),
            (128, vec![0b1000_0000, 0b0000_0001]),
            (300, vec![0b1010_1100, 0b0000_0010]),
            (
                u64::MAX,
                vec![0xff; 9]
                    .into_iter()
                    .chain(vec![0x01])
                    .collect::<Vec<_>>(),
            ),
        ] {
            let mut w = BitWriter::new();
            w.write_varint_u64(value);
            assert_eq!(w.finish(), expected, "encoding of {value}");
            let mut r = BitReader::new(&expected);
            assert_eq!(r.read_varint_u64().unwrap(), value);
        }
    }

    #[test]
    fn varint_roundtrip_many() {
        let mut w = BitWriter::new();
        let values = [
            0u64,
            1,
            63,
            64,
            127,
            128,
            16383,
            16384,
            u64::MAX,
            u64::MAX - 1,
        ];
        for v in values {
            w.write_varint_u64(v);
        }
        let bytes = w.finish();
        let mut r = BitReader::new(&bytes);
        for v in values {
            assert_eq!(r.read_varint_u64().unwrap(), v);
        }
    }

    #[test]
    fn u64_roundtrip() {
        let mut w = BitWriter::new();
        w.write_u64(u64::MAX);
        w.write_u64(0);
        w.write_u64(1 << 63);
        let bytes = w.finish();
        let mut r = BitReader::new(&bytes);
        assert_eq!(r.read_u64().unwrap(), u64::MAX);
        assert_eq!(r.read_u64().unwrap(), 0);
        assert_eq!(r.read_u64().unwrap(), 1 << 63);
    }

    #[test]
    fn reader_eof() {
        let bytes = vec![0u8];
        let mut r = BitReader::new(&bytes);
        assert!(r.read_u64().is_err());
    }
}
