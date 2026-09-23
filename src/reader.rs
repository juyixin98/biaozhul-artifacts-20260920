//! 增量字节读取器（游标）。
//!
//! [`Reader`] 在一段字节上移动游标，读取大端整数和长度前缀块。
//! 关键设计：越界时区分两种语义——
//! * 顶层读取越过**本次喂入数据**的末尾 → [`ParseError::Truncated`]（字节以后还可能到）；
//! * 读取越过一个**已声明长度的嵌套块**（sub-reader）边界 →
//!   [`ParseError::LengthMismatch`]（报文结构本身矛盾，补字节也无意义）。
//!
//! 注意 `Reader` 本身不知道自己是在“真实时缓冲”上还是“声明块”上移动：
//! [`Reader::sub`] 构造有界 sub-reader 时会把缺字节报成 `LengthMismatch`，
//! 而顶层 reader 的缺字节报成 `Truncated`。

use crate::error::ParseError;

#[derive(Debug)]
pub struct Reader<'a> {
    buf: &'a [u8],
    /// 当前游标位置。
    pos: usize,
    /// 触发越界时产生的错误类型。
    eof: EofKind,
}

#[derive(Debug, Clone, Copy)]
enum EofKind {
    /// 顶层流：`what` 描述所在层。
    Truncated(&'static str),
    /// 有界嵌套块：长度前缀声明的字节数大于外层实际剩余。
    LengthMismatch {
        field: &'static str,
        declared: usize,
        /// 构造 sub-reader 时外层实际提供的字节数。
        available: usize,
    },
}

impl<'a> Reader<'a> {
    /// 在整段字节上构造顶层 reader；越界报 `Truncated { what }`。
    pub fn new(buf: &'a [u8], what: &'static str) -> Self {
        Reader {
            buf,
            pos: 0,
            eof: EofKind::Truncated(what),
        }
    }

    fn eof_err(&self, need: usize) -> ParseError {
        match self.eof {
            EofKind::Truncated(what) => ParseError::Truncated { what },
            EofKind::LengthMismatch {
                field,
                declared,
                available,
            } => {
                // 在块内继续读时，实际可用只可能是 available 的子集；
                // 用“声明值 vs 块内实际能给到的值”描述不一致。
                let _ = need;
                ParseError::LengthMismatch {
                    field,
                    declared,
                    actual: available.min(self.pos + need),
                }
            }
        }
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn is_empty(&self) -> bool {
        self.remaining() == 0
    }

    pub fn position(&self) -> usize {
        self.pos
    }

    /// 取走 n 个字节；不足时按 reader 种类报截断或长度不符。
    pub fn take(&mut self, n: usize) -> Result<&'a [u8], ParseError> {
        if self.remaining() < n {
            return Err(self.eof_err(n));
        }
        let out = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    pub fn u8(&mut self) -> Result<u8, ParseError> {
        Ok(self.take(1)?[0])
    }

    pub fn u16(&mut self) -> Result<u16, ParseError> {
        let b = self.take(2)?;
        Ok(u16::from_be_bytes([b[0], b[1]]))
    }

    pub fn u24(&mut self) -> Result<u32, ParseError> {
        let b = self.take(3)?;
        Ok(u32::from_be_bytes([0, b[0], b[1], b[2]]))
    }

    pub fn u32(&mut self) -> Result<u32, ParseError> {
        let b = self.take(4)?;
        Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }

    /// 取走定长 32 字节随机数。
    pub fn array32(&mut self) -> Result<[u8; 32], ParseError> {
        let b = self.take(32)?;
        let mut out = [0u8; 32];
        out.copy_from_slice(b);
        Ok(out)
    }

    /// 取走一个 u8 长度前缀字节串。
    pub fn vec_u8_len(&mut self, field: &'static str) -> Result<&'a [u8], ParseError> {
        let n = self.u8()? as usize;
        self.take_bounded(n, field)
    }

    /// 取走一个 u16 长度前缀字节串。
    pub fn vec_u16_len(&mut self, field: &'static str) -> Result<&'a [u8], ParseError> {
        let n = self.u16()? as usize;
        self.take_bounded(n, field)
    }

    /// 取走“声明长度 = n”的原始字节；若外层装不下则报长度不符
    /// （顶层 reader 上同样报长度不符：长度前缀本身已完整读到，
    /// 只是它声明的载荷越界，这属于结构矛盾而非单纯截断）。
    fn take_bounded(&mut self, n: usize, field: &'static str) -> Result<&'a [u8], ParseError> {
        if self.remaining() < n {
            return Err(ParseError::LengthMismatch {
                field,
                declared: n,
                actual: self.remaining(),
            });
        }
        let out = &self.buf[self.pos..self.pos + n];
        self.pos += n;
        Ok(out)
    }

    /// 打开一个 u16 长度前缀的嵌套块，在其上执行 `f`。
    /// sub-reader 中任何越界都被翻译成对应字段的 [`ParseError::LengthMismatch`]。
    pub fn sub_u16<T>(
        &mut self,
        field: &'static str,
        f: impl FnOnce(&mut Reader<'a>) -> Result<T, ParseError>,
    ) -> Result<T, ParseError> {
        let n = self.u16()? as usize;
        self.enter(n, field, f)
    }

    /// 打开一个 u8 长度前缀的嵌套块。
    pub fn sub_u8<T>(
        &mut self,
        field: &'static str,
        f: impl FnOnce(&mut Reader<'a>) -> Result<T, ParseError>,
    ) -> Result<T, ParseError> {
        let n = self.u8()? as usize;
        self.enter(n, field, f)
    }

    /// 在已知长度的块上打开 sub-reader（用于长度前缀来自 u24 等非标准位置）。
    pub fn enter<T>(
        &mut self,
        n: usize,
        field: &'static str,
        f: impl FnOnce(&mut Reader<'a>) -> Result<T, ParseError>,
    ) -> Result<T, ParseError> {
        if self.remaining() < n {
            return Err(ParseError::LengthMismatch {
                field,
                declared: n,
                actual: self.remaining(),
            });
        }
        let mut sub = Reader {
            buf: &self.buf[self.pos..self.pos + n],
            pos: 0,
            eof: EofKind::LengthMismatch {
                field,
                declared: n,
                available: n,
            },
        };
        let result = f(&mut sub)?;
        self.pos += n;
        Ok(result)
    }

    /// 要求块已全部消费，否则报长度不符（尾部多余字节）。
    pub fn expect_empty(&self, field: &'static str) -> Result<(), ParseError> {
        if self.remaining() != 0 {
            return Err(ParseError::LengthMismatch {
                field,
                declared: self.buf.len(),
                actual: self.buf.len() - self.remaining(),
            });
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn top_level_underflow_is_truncated() {
        let mut r = Reader::new(&[0x01, 0x02], "record");
        assert!(r.u8().is_ok());
        let err = r.u16().unwrap_err();
        assert!(err.is_truncated(), "expected Truncated, got {err:?}");
    }

    #[test]
    fn bounded_block_overflow_is_length_mismatch() {
        // 顶层 4 字节，u16 长度前缀声明 10 字节载荷。
        let mut r = Reader::new(&[0x00, 0x0A, 1, 2], "x");
        let err = r.vec_u16_len("field_x").unwrap_err();
        assert!(
            matches!(
                err,
                ParseError::LengthMismatch {
                    field: "field_x",
                    declared: 10,
                    actual: 2
                }
            ),
            "got {err:?}"
        );
    }

    #[test]
    fn sub_reader_internal_underflow_is_mismatch() {
        // 外层声明块 3 字节，块内却要读 u16 长度前缀（2B）+2B 载荷。
        let data = [[0x00u8, 0x03u8].as_slice(), &[0x00, 0x05, 0x01]].concat();
        let mut top = Reader::new(&data, "top");
        let err = top
            .sub_u16("outer", |r| r.vec_u16_len("inner"))
            .unwrap_err();
        assert!(matches!(
            err,
            ParseError::LengthMismatch { field: "inner", .. }
        ));
    }

    #[test]
    fn expect_empty_detects_tail_garbage() {
        let mut r = Reader::new(&[1, 2, 3], "x");
        r.u8().unwrap();
        assert!(r.expect_empty("block").is_err());
    }
}
