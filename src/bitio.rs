//! MSB-first bit-level I/O over byte streams.

use crate::error::{Error, Result};
use std::io::{Read, Write};

const READ_BUF: usize = 8192;

/// Writes codes MSB-first; the final partial byte is zero-padded on `finish`.
pub struct BitWriter<W: Write> {
    w: W,
    cur: u8,
    nbits: u8,
}

impl<W: Write> BitWriter<W> {
    pub fn new(w: W) -> Self {
        BitWriter { w, cur: 0, nbits: 0 }
    }

    /// Append the low `len` bits of `code`, most-significant bit first.
    pub fn write_code(&mut self, code: u64, len: u8) -> Result<()> {
        for i in (0..len).rev() {
            let bit = ((code >> i) & 1) as u8;
            self.cur = (self.cur << 1) | bit;
            self.nbits += 1;
            if self.nbits == 8 {
                self.w.write_all(&[self.cur])?;
                self.cur = 0;
                self.nbits = 0;
            }
        }
        Ok(())
    }

    /// Zero-pad the final partial byte, flush, and return the writer.
    pub fn finish(mut self) -> Result<W> {
        if self.nbits > 0 {
            self.cur <<= 8 - self.nbits;
            self.w.write_all(&[self.cur])?;
        }
        self.w.flush()?;
        Ok(self.w)
    }
}

/// Reads bits MSB-first from a byte stream with a bounded internal buffer.
pub struct BitReader<R: Read> {
    r: R,
    buf: [u8; READ_BUF],
    pos: usize,
    len: usize,
    cur: u8,
    nbits_left: u8,
    eof: bool,
}

impl<R: Read> BitReader<R> {
    pub fn new(r: R) -> Self {
        BitReader {
            r,
            buf: [0u8; READ_BUF],
            pos: 0,
            len: 0,
            cur: 0,
            nbits_left: 0,
            eof: false,
        }
    }

    /// Read one bit; `Ok(None)` means the stream is exhausted.
    pub fn read_bit(&mut self) -> Result<Option<u64>> {
        if self.nbits_left == 0 && !self.fill()? {
            return Ok(None);
        }
        let bit = (self.cur >> 7) as u64;
        self.cur <<= 1;
        self.nbits_left -= 1;
        Ok(Some(bit))
    }

    fn fill(&mut self) -> Result<bool> {
        loop {
            if self.pos < self.len {
                self.cur = self.buf[self.pos];
                self.pos += 1;
                self.nbits_left = 8;
                return Ok(true);
            }
            if self.eof {
                return Ok(false);
            }
            let n = self.r.read(&mut self.buf)?;
            if n == 0 {
                self.eof = true;
                return Ok(false);
            }
            self.pos = 0;
            self.len = n;
        }
    }

    /// Strict end-of-stream check: remaining bits in the current byte must be
    /// zero padding and the underlying stream must hold no further bytes.
    pub fn finish_check(mut self) -> Result<()> {
        if self.nbits_left > 0 {
            let remaining = self.cur >> (8 - self.nbits_left);
            if remaining != 0 {
                return Err(Error::TrailingData);
            }
        }
        if self.pos < self.len {
            return Err(Error::TrailingData);
        }
        if !self.eof {
            let mut b = [0u8; 1];
            if self.r.read(&mut b)? > 0 {
                return Err(Error::TrailingData);
            }
        }
        Ok(())
    }
}
