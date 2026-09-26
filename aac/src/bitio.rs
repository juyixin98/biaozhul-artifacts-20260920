//! Bit-at-a-time I/O over byte streams, MSB-first.
//!
//! The arithmetic coder emits and consumes bits through these adapters so the
//! same core works over in-memory buffers and arbitrary `Read`/`Write`
//! streams. Bits are packed big-endian (the first emitted bit is the most
//! significant bit of the first output byte). The final, partially filled byte
//! is padded with zero bits; [`BitWriter::finish`] reports how many padding
//! bits were appended so the container header can record it.

use std::io::{self, Read, Write};

/// Writes individual bits, MSB-first, into a byte-oriented [`Write`].
pub struct BitWriter<W: Write> {
    inner: W,
    /// Accumulator for bits not yet flushed.
    current: u8,
    /// Number of valid bits already in `current` (0..8).
    filled: u32,
    /// Total number of payload bits written (excluding padding).
    bits_written: u64,
}

impl<W: Write> BitWriter<W> {
    /// Wrap an output byte stream.
    pub fn new(inner: W) -> Self {
        BitWriter {
            inner,
            current: 0,
            filled: 0,
            bits_written: 0,
        }
    }

    /// Append one bit (only bit 0 of `bit` is used).
    #[inline]
    pub fn write_bit(&mut self, bit: u8) -> io::Result<()> {
        if bit & 1 != 0 {
            self.current |= 1 << (7 - self.filled);
        }
        self.filled += 1;
        self.bits_written += 1;
        if self.filled == 8 {
            self.inner.write_all(core::slice::from_ref(&self.current))?;
            self.current = 0;
            self.filled = 0;
        }
        Ok(())
    }

    /// Number of payload bits emitted so far.
    #[inline]
    pub fn bits_written(&self) -> u64 {
        self.bits_written
    }

    /// Flush a partial final byte with zero padding.
    /// Returns `(bits_written, padding_bits)`.
    pub fn finish(mut self) -> io::Result<(W, u64, u32)> {
        let padding = if self.filled > 0 {
            let pad = 8 - self.filled;
            self.inner.write_all(core::slice::from_ref(&self.current))?;
            pad
        } else {
            0
        };
        self.inner.flush()?;
        Ok((self.inner, self.bits_written, padding))
    }
}

/// Reads individual bits, MSB-first, from a byte-oriented [`Read`].
///
/// Beyond end of stream the reader supplies zero bits forever; the arithmetic
/// decoder treats that as the standard "infinite zero tail" and relies on the
/// EOF symbol to tell where real data ends. [`BitReader::bits_read`] together
/// with [`BitReader::eof_reached`] lets callers distinguish genuine stream
/// bytes from the virtual zero tail.
pub struct BitReader<R: Read> {
    inner: R,
    current: u8,
    /// Next bit position to consume within `current` (0 = MSB .. 7 = LSB).
    position: u32,
    /// Whether `current` holds a real byte.
    have_byte: bool,
    /// Set once the underlying reader returned EOF.
    eof: bool,
    bits_read: u64,
    /// Total bytes taken from the underlying reader.
    bytes_read: u64,
    /// Zero bits served from the virtual tail after the underlying EOF.
    virtual_bits: u64,
}

impl<R: Read> BitReader<R> {
    /// Wrap an input byte stream.
    pub fn new(inner: R) -> Self {
        BitReader {
            inner,
            current: 0,
            position: 0,
            have_byte: false,
            eof: false,
            bits_read: 0,
            bytes_read: 0,
            virtual_bits: 0,
        }
    }

    /// Read one bit; past EOF this always returns 0.
    #[inline]
    pub fn read_bit(&mut self) -> io::Result<u8> {
        if !self.have_byte {
            if self.eof {
                self.bits_read += 1;
                self.virtual_bits += 1;
                return Ok(0);
            }
            let mut buf = [0u8; 1];
            match self.inner.read(&mut buf)? {
                0 => {
                    self.eof = true;
                    self.bits_read += 1;
                    self.virtual_bits += 1;
                    return Ok(0);
                }
                _ => {
                    self.current = buf[0];
                    self.have_byte = true;
                    self.position = 0;
                    self.bytes_read += 1;
                }
            }
        }
        let bit = (self.current >> (7 - self.position)) & 1;
        self.position += 1;
        self.bits_read += 1;
        if self.position == 8 {
            self.have_byte = false;
        }
        Ok(bit)
    }

    /// Number of payload bits consumed so far (including virtual zero bits).
    #[inline]
    pub fn bits_read(&self) -> u64 {
        self.bits_read
    }

    /// Number of real bytes pulled from the underlying stream.
    #[inline]
    pub fn bytes_read(&self) -> u64 {
        self.bytes_read
    }

    /// Whether the underlying byte stream has already returned EOF.
    #[inline]
    pub fn eof_reached(&self) -> bool {
        self.eof
    }

    /// Number of bits served from the all-zero virtual tail (i.e. bits read
    /// beyond the last real byte). Used to detect truncated coded streams.
    #[inline]
    pub fn virtual_bits(&self) -> u64 {
        self.virtual_bits
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_bits_and_padding() {
        let mut buf = Vec::new();
        {
            let mut w = BitWriter::new(&mut buf);
            for b in [1, 0, 1, 1, 0, 1, 0, 0, 1, 1] {
                w.write_bit(b).unwrap();
            }
            let (_, bits, pad) = w.finish().unwrap();
            assert_eq!(bits, 10);
            assert_eq!(pad, 6);
        }
        assert_eq!(buf, vec![0b10110100, 0b11000000]);

        let mut r = BitReader::new(&buf[..]);
        let mut got = Vec::new();
        for _ in 0..10 {
            got.push(r.read_bit().unwrap());
        }
        assert_eq!(got, vec![1, 0, 1, 1, 0, 1, 0, 0, 1, 1]);
        // virtual zero tail
        for _ in 0..20 {
            assert_eq!(r.read_bit().unwrap(), 0);
        }
        assert!(r.eof_reached());
    }
}
