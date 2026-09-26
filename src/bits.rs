//! MSB-first 位流读写。
//!
//! 所有整数字段均以大端（高位在前）写入，霍夫曼码字也按高位在前（canonical
//! Huffman 的惯例）发射。读取器只做**定界**：知道自身总位数，任何越过末尾的
//! 读取都会返回 [`Error::TruncatedStream`]，从而让调用方区分“截断攻击”与
//! “正常的尾部填充”。

use crate::error::{Error, Result};

/// 位写入器：向字节缓冲区追加位。
#[derive(Debug, Default)]
pub struct BitWriter {
    buf: Vec<u8>,
    current: u8,
    /// current 中已填充的位数 (0..8)。
    filled: u32,
    total_bits: u64,
}

impl BitWriter {
    /// 空写入器。
    pub fn new() -> Self {
        Self::default()
    }

    /// 带容量预估创建。
    pub fn with_capacity(cap: usize) -> Self {
        Self {
            buf: Vec::with_capacity(cap),
            current: 0,
            filled: 0,
            total_bits: 0,
        }
    }

    /// 追加单个位（`bit & 1`）。
    pub fn write_bit(&mut self, bit: u8) {
        if bit & 1 != 0 {
            self.current |= 1u8.wrapping_shl(7 - self.filled);
        }
        self.filled += 1;
        self.total_bits += 1;
        if self.filled == 8 {
            self.buf.push(self.current);
            self.current = 0;
            self.filled = 0;
        }
    }

    /// 追加 `value` 的低 `bits` 位，高位在前。`bits` 最多 64。
    pub fn write_bits(&mut self, value: u64, bits: u32) {
        debug_assert!(bits <= 64);
        for i in (0..bits).rev() {
            self.write_bit(((value >> i) & 1) as u8);
        }
    }

    /// 追加一整个字节。
    pub fn write_u8(&mut self, byte: u8) {
        self.write_bits(byte as u64, 8);
    }

    /// 以大端追加 16 位。
    pub fn write_u16(&mut self, value: u16) {
        self.write_bits(value as u64, 16);
    }

    /// 以大端追加 32 位。
    pub fn write_u32(&mut self, value: u32) {
        self.write_bits(value as u64, 32);
    }

    /// 以大端追加 64 位。
    pub fn write_u64(&mut self, value: u64) {
        self.write_bits(value, 64);
    }

    /// 直接追加整段字节（不改变位对齐，调用时必须位于字节边界）。
    pub fn write_bytes(&mut self, bytes: &[u8]) {
        debug_assert_eq!(self.filled, 0, "write_bytes requires byte alignment");
        self.buf.extend_from_slice(bytes);
        self.total_bits += (bytes.len() as u64) * 8;
    }

    /// 已写入位数。
    pub fn total_bits(&self) -> u64 {
        self.total_bits
    }

    /// 完成写入：末字节不足 8 位时补 0，并返回 `(字节, 总位数)`。
    pub fn finish(mut self) -> (Vec<u8>, u64) {
        if self.filled > 0 {
            self.buf.push(self.current);
        }
        (self.buf, self.total_bits)
    }
}

/// 位读取器：在定长字节片上逐位读取，越界即报 [`Error::TruncatedStream`]。
#[derive(Debug)]
pub struct BitReader<'a> {
    data: &'a [u8],
    bit_pos: u64,
    total_bits: u64,
}

impl<'a> BitReader<'a> {
    /// 以精确位数 `total_bits` 创建读取器；`total_bits` 不得超过
    /// `data.len() * 8`。
    pub fn new(data: &'a [u8], total_bits: u64) -> Result<Self> {
        if total_bits > (data.len() as u64) * 8 {
            return Err(Error::TruncatedStream);
        }
        Ok(Self {
            data,
            bit_pos: 0,
            total_bits,
        })
    }

    /// 剩余可读位数。
    pub fn remaining(&self) -> u64 {
        self.total_bits - self.bit_pos
    }

    /// 已读位数。
    pub fn position(&self) -> u64 {
        self.bit_pos
    }

    /// 总位数。
    pub fn total_bits(&self) -> u64 {
        self.total_bits
    }

    /// 读单位；越界报错。
    pub fn read_bit(&mut self) -> Result<u8> {
        if self.bit_pos >= self.total_bits {
            return Err(Error::TruncatedStream);
        }
        let byte_index = (self.bit_pos >> 3) as usize;
        let bit_index = (self.bit_pos & 7) as u32;
        let bit = (self.data[byte_index] >> (7 - bit_index)) & 1;
        self.bit_pos += 1;
        Ok(bit)
    }

    /// 读 `bits` 位（最多 56，避免位移溢出问题），高位在前。
    pub fn read_bits(&mut self, bits: u32) -> Result<u64> {
        debug_assert!(bits <= 56);
        if bits == 0 {
            return Ok(0);
        }
        if self.remaining() < bits as u64 {
            return Err(Error::TruncatedStream);
        }
        let mut value = 0u64;
        for _ in 0..bits {
            value = (value << 1) | self.read_bit()? as u64;
        }
        Ok(value)
    }

    /// 读字节（需剩余 8 位）。
    pub fn read_u8(&mut self) -> Result<u8> {
        Ok(self.read_bits(8)? as u8)
    }

    /// 读 16 位大端。
    pub fn read_u16(&mut self) -> Result<u16> {
        Ok(self.read_bits(16)? as u16)
    }

    /// 读 32 位大端。
    pub fn read_u32(&mut self) -> Result<u32> {
        Ok(self.read_bits(32)? as u32)
    }

    /// 读 64 位大端（拆成两段，规避 64 位移越界）。
    pub fn read_u64(&mut self) -> Result<u64> {
        let hi = self.read_bits(32)?;
        let lo = self.read_bits(32)?;
        Ok((hi << 32) | lo)
    }

    /// 跳过 `n` 位。
    pub fn skip(&mut self, n: u64) -> Result<()> {
        if self.remaining() < n {
            return Err(Error::TruncatedStream);
        }
        self.bit_pos += n;
        Ok(())
    }

    /// 要求已读完整数个字节；否则报格式错误（用于字节对齐字段）。
    pub fn require_byte_aligned(&self) -> Result<()> {
        if self.bit_pos & 7 != 0 {
            return Err(Error::InvalidLengthTable("field not byte aligned"));
        }
        Ok(())
    }
}
