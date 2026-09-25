//! MSB-first 比特流读写。
//!
//! 比特序：每个字节内从最高位（bit 7）到最低位（bit 0）依次填充。
//! 编码结束时最后一个字节不足 8 位的部分补 0。
//! 解码时输入耗尽后按 0 比特补读，但补读数量超过上限即报 `UnexpectedEof`
//! （由上层映射为 [`crate::Error::TruncatedStream`]）。

use std::io::{self, Read, Write};

use crate::IO_BUF_SIZE;

/// MSB-first 比特写入器。
pub struct BitWriter<W: Write> {
    out: W,
    acc: u8,
    nbits: u8,
    bytes_written: u64,
}

impl<W: Write> BitWriter<W> {
    pub fn new(out: W) -> Self {
        Self {
            out,
            acc: 0,
            nbits: 0,
            bytes_written: 0,
        }
    }

    /// 已完整写出的字节数。
    pub fn bytes_written(&self) -> u64 {
        self.bytes_written
    }

    pub fn write_bit(&mut self, bit: bool) -> io::Result<()> {
        self.acc = (self.acc << 1) | u8::from(bit);
        self.nbits += 1;
        if self.nbits == 8 {
            self.out.write_all(&[self.acc])?;
            self.bytes_written += 1;
            self.acc = 0;
            self.nbits = 0;
        }
        Ok(())
    }

    /// 收尾：最后一个字节不足 8 位时补 0 比特后写出。
    pub fn flush(&mut self) -> io::Result<()> {
        if self.nbits > 0 {
            let pad = 8 - self.nbits;
            self.acc <<= pad;
            self.out.write_all(&[self.acc])?;
            self.bytes_written += 1;
            self.acc = 0;
            self.nbits = 0;
        }
        self.out.flush()
    }
}

/// MSB-first 比特读取器，内部分块缓冲（O(1) 内存）。
pub struct BitReader<R: Read> {
    inp: R,
    chunk: [u8; IO_BUF_SIZE],
    chunk_pos: usize,
    chunk_len: usize,
    cur: u8,
    nbits: u8,
    exhausted: bool,
    zeros_served: u32,
    max_zero_bits: u32,
    bytes_read: u64,
}

impl<R: Read> BitReader<R> {
    pub fn new(inp: R, max_zero_bits: u32) -> Self {
        Self {
            inp,
            chunk: [0; IO_BUF_SIZE],
            chunk_pos: 0,
            chunk_len: 0,
            cur: 0,
            nbits: 0,
            exhausted: false,
            zeros_served: 0,
            max_zero_bits,
            bytes_read: 0,
        }
    }

    /// 从底层流累计读取的字节数（含缓冲预读）。
    pub fn bytes_read(&self) -> u64 {
        self.bytes_read
    }

    /// 读取一个比特。输入耗尽后返回 0 比特，但补读总数超过上限时报
    /// `UnexpectedEof`。
    pub fn read_bit(&mut self) -> io::Result<bool> {
        if self.nbits == 0 && !self.refill()? {
            self.zeros_served += 1;
            if self.zeros_served > self.max_zero_bits {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "truncated stream: EOF symbol not reached before input exhausted",
                ));
            }
            return Ok(false);
        }
        self.nbits -= 1;
        Ok((self.cur >> self.nbits) & 1 == 1)
    }

    /// 向当前比特寄存器装入下一个字节；返回是否成功（false 表示输入耗尽）。
    fn refill(&mut self) -> io::Result<bool> {
        while self.chunk_pos == self.chunk_len {
            if self.exhausted {
                return Ok(false);
            }
            let n = self.inp.read(&mut self.chunk)?;
            if n == 0 {
                self.exhausted = true;
                return Ok(false);
            }
            self.chunk_pos = 0;
            self.chunk_len = n;
            self.bytes_read += n as u64;
        }
        self.cur = self.chunk[self.chunk_pos];
        self.chunk_pos += 1;
        self.nbits = 8;
        Ok(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bit_roundtrip_msb_first() {
        let bits = [
            true, false, true, true, false, false, false, true, // 0xB1
            true, true, // 不满一字节
        ];
        let mut w = BitWriter::new(Vec::new());
        for &b in &bits {
            w.write_bit(b).unwrap();
        }
        w.flush().unwrap();
        assert_eq!(w.bytes_written(), 2);

        let mut r = BitReader::new(&w.out[..], 64);
        for &b in &bits {
            assert_eq!(r.read_bit().unwrap(), b);
        }
        // 末尾补的是 0 比特
        for _ in 0..5 {
            assert!(!r.read_bit().unwrap());
        }
    }

    #[test]
    fn zero_bits_after_exhaustion_are_bounded() {
        let mut r = BitReader::new(&[0xFFu8][..], 8);
        for _ in 0..8 {
            assert!(r.read_bit().unwrap());
        }
        for _ in 0..8 {
            assert!(!r.read_bit().unwrap()); // 补读 0
        }
        let err = r.read_bit().unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::UnexpectedEof);
    }
}
