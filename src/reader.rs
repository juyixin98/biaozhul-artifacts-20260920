//! 增量字节读取器：所有解析都在不可变的完整报文 `&[u8]` 上按游标推进，
//! 不借助任何现成 DNS 协议解析库。
//!
//! 读取器内部使用**相对完整报文的绝对偏移**，并带一个可选上界 `end`
//! （例如某条资源记录 RDLENGTH 划定的范围）。压缩指针可指向完整报文
//! 内、上界之外的更早位置（RFC 1035 允许），因此底层缓冲永不截断。

use crate::error::{DnsError, Result};

#[derive(Debug, Clone)]
pub struct Reader<'a> {
    /// 完整报文。
    buf: &'a [u8],
    /// 本子读取器的起点（绝对偏移）。
    start: usize,
    /// 当前游标（绝对偏移）。
    pos: usize,
    /// 本子读取器的上界（绝对偏移，不含）。
    end: usize,
}

impl<'a> Reader<'a> {
    /// 以完整报文创建读取器。
    pub fn new(buf: &'a [u8]) -> Self {
        Self {
            buf,
            start: 0,
            pos: 0,
            end: buf.len(),
        }
    }

    /// 当前游标（相对完整报文的绝对偏移）。
    pub fn position(&self) -> usize {
        self.pos
    }

    /// 距离上界还剩多少字节可读。
    pub fn remaining(&self) -> usize {
        self.end.saturating_sub(self.pos)
    }

    /// 在本子读取器范围内是否已读完。
    pub fn is_empty(&self) -> bool {
        self.pos >= self.end
    }

    /// 完整报文缓冲（压缩指针按绝对偏移随机访问时使用）。
    pub fn full_buffer(&self) -> &'a [u8] {
        self.buf
    }

    fn ensure(&self, need: usize) -> Result<()> {
        match self.pos.checked_add(need) {
            Some(p) if p <= self.end => Ok(()),
            _ => Err(DnsError::UnexpectedEof {
                offset: self.pos,
                len: self.buf.len(),
            }),
        }
    }

    /// 读取一个字节并推进游标。
    pub fn u8(&mut self) -> Result<u8> {
        self.ensure(1)?;
        let v = self.buf[self.pos];
        self.pos += 1;
        Ok(v)
    }

    /// 读取网络字节序（大端）u16。
    pub fn u16(&mut self) -> Result<u16> {
        self.ensure(2)?;
        let v = u16::from_be_bytes([self.buf[self.pos], self.buf[self.pos + 1]]);
        self.pos += 2;
        Ok(v)
    }

    /// 读取网络字节序（大端）u32。
    pub fn u32(&mut self) -> Result<u32> {
        self.ensure(4)?;
        let mut b = [0u8; 4];
        b.copy_from_slice(&self.buf[self.pos..self.pos + 4]);
        self.pos += 4;
        Ok(u32::from_be_bytes(b))
    }

    /// 读取 `n` 字节的借用切片并推进游标（不越过本子读取器上界）。
    pub fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        self.ensure(n)?;
        let s = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(s)
    }

    /// 跳过 `n` 字节。
    pub fn skip(&mut self, n: usize) -> Result<()> {
        self.ensure(n)?;
        self.pos += n;
        Ok(())
    }

    /// 把游标移动到指定绝对偏移。必须落在本子读取器范围内。
    pub fn seek_to(&mut self, absolute: usize) -> Result<()> {
        if absolute < self.start || absolute > self.end {
            return Err(DnsError::UnexpectedEof {
                offset: absolute,
                len: self.buf.len(),
            });
        }
        self.pos = absolute;
        Ok(())
    }

    /// 派生出共享同一完整报文的子读取器：
    /// 范围为 `[offset, offset+len)`，越界则报 [`DnsError::TruncatedRecord`]。
    pub fn sub_reader(&self, offset: usize, len: usize) -> Result<Reader<'a>> {
        let end = offset.checked_add(len).ok_or(DnsError::TruncatedRecord {
            at: offset,
            declared: len,
            actual: self.buf.len().saturating_sub(offset),
        })?;
        if end > self.buf.len() {
            return Err(DnsError::TruncatedRecord {
                at: offset,
                declared: len,
                actual: self.buf.len().saturating_sub(offset),
            });
        }
        Ok(Reader {
            buf: self.buf,
            start: offset,
            pos: offset,
            end,
        })
    }
}
