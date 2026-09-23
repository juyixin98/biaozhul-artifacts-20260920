//! A tiny incremental cursor over a byte slice.
//!
//! This is the project's hand-written "incremental byte parsing" primitive:
//! it never allocates, never panics on bad input, and reports a uniform
//! [`NeedMore`] error whenever a read would run past the end. The higher
//! layers translate that into the precise public [`ParseError`] variant,
//! distinguishing a truncated fixed field from a length prefix that overruns
//! its container.

use crate::error::Truncated;

/// Returned when a read needs more bytes than the slice currently holds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct NeedMore(pub Truncated);

impl NeedMore {
    fn field() -> Self {
        NeedMore(Truncated::Field)
    }
}

pub(crate) struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn is_empty(&self) -> bool {
        self.pos == self.buf.len()
    }

    pub fn read_u8(&mut self) -> Result<u8, NeedMore> {
        let v = self
            .buf
            .get(self.pos)
            .copied()
            .ok_or_else(NeedMore::field)?;
        self.pos += 1;
        Ok(v)
    }

    pub fn read_u16(&mut self) -> Result<u16, NeedMore> {
        if self.remaining() < 2 {
            return Err(NeedMore::field());
        }
        let v = u16::from_be_bytes([self.buf[self.pos], self.buf[self.pos + 1]]);
        self.pos += 2;
        Ok(v)
    }

    pub fn read_u24(&mut self) -> Result<u32, NeedMore> {
        if self.remaining() < 3 {
            return Err(NeedMore::field());
        }
        let v = u32::from_be_bytes([
            0,
            self.buf[self.pos],
            self.buf[self.pos + 1],
            self.buf[self.pos + 2],
        ]);
        self.pos += 3;
        Ok(v)
    }

    /// Consume exactly `n` bytes.
    pub fn take(&mut self, n: usize) -> Result<&'a [u8], NeedMore> {
        if self.remaining() < n {
            return Err(NeedMore::field());
        }
        let out = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    /// All unconsumed bytes.
    pub fn rest(self) -> &'a [u8] {
        &self.buf[self.pos..]
    }
}
