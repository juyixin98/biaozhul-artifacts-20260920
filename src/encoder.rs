//! 32 位整数区间算术编码器。
//!
//! 算法：Witten–Neal–Cleary 整数算术编码，进位采用 bits-plus-follow 方案。
//! 区间 [low, high] 用 u64 保存但始终限制在低 32 位；乘法中间结果最大
//! 2^32 * 2^14 = 2^46，不会溢出 u64。

use std::io::Write;

use crate::bitio::BitWriter;
use crate::model::{AdaptiveModel, EOF_SYMBOL};
use crate::Error;

/// 码值位宽。
pub(crate) const CODE_VALUE_BITS: u32 = 32;
/// 区间上界初值（2^32 - 1）。
pub(crate) const TOP_VALUE: u64 = (1 << CODE_VALUE_BITS) - 1;
/// 第一四分之一。
pub(crate) const FIRST_QTR: u64 = 1 << (CODE_VALUE_BITS - 2);
/// 二分之一。
pub(crate) const HALF: u64 = 1 << (CODE_VALUE_BITS - 1);
/// 第三四分之一。
pub(crate) const THIRD_QTR: u64 = 3 << (CODE_VALUE_BITS - 2);

/// 算术编码器。逐符号喂入，最后必须调用 [`Encoder::finish`]。
pub struct Encoder<W: Write> {
    low: u64,
    high: u64,
    bits_to_follow: u64,
    model: AdaptiveModel,
    out: BitWriter<W>,
    max_output_bytes: u64,
}

impl<W: Write> Encoder<W> {
    pub fn new(out: W, max_output_bytes: u64) -> Self {
        Self {
            low: 0,
            high: TOP_VALUE,
            bits_to_follow: 0,
            model: AdaptiveModel::new(),
            out: BitWriter::new(out),
            max_output_bytes,
        }
    }

    /// 编码一个字节。
    pub fn encode_byte(&mut self, byte: u8) -> Result<(), Error> {
        self.encode_symbol(u16::from(byte))
    }

    fn encode_symbol(&mut self, sym: u16) -> Result<(), Error> {
        let total = u64::from(self.model.total());
        let cum_low = u64::from(self.model.cum_freq_of(sym));
        let freq = u64::from(self.model.freq_of(sym));
        let range = self.high - self.low + 1;
        self.high = self.low + (range * (cum_low + freq)) / total - 1;
        self.low = self.low + (range * cum_low) / total;
        self.renormalize()?;
        self.model.update(sym);
        Ok(())
    }

    /// 归一化：把已确定的最高位（及其进位跟随位）写出。
    fn renormalize(&mut self) -> Result<(), Error> {
        loop {
            if self.high < HALF {
                self.emit_bit_plus_follow(false)?;
            } else if self.low >= HALF {
                self.emit_bit_plus_follow(true)?;
                self.low -= HALF;
                self.high -= HALF;
            } else if self.low >= FIRST_QTR && self.high < THIRD_QTR {
                // E3 条件：进位未决，记录跟随位，映射掉中间一半。
                self.bits_to_follow += 1;
                self.low -= FIRST_QTR;
                self.high -= FIRST_QTR;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
        }
        Ok(())
    }

    /// 写出一个比特，并写出全部未决的反向跟随位（进位处理）。
    fn emit_bit_plus_follow(&mut self, bit: bool) -> Result<(), Error> {
        self.emit_bit(bit)?;
        while self.bits_to_follow > 0 {
            self.emit_bit(!bit)?;
            self.bits_to_follow -= 1;
        }
        Ok(())
    }

    fn emit_bit(&mut self, bit: bool) -> Result<(), Error> {
        self.out.write_bit(bit).map_err(Error::io)?;
        if self.out.bytes_written() > self.max_output_bytes {
            return Err(Error::OutputLimitExceeded);
        }
        Ok(())
    }

    /// 编码 EOF 终止符并收尾，返回 (重标定次数, 输出字节数)。
    ///
    /// 收尾规则（与解码器约定一致，见 FORMAT.md）：补记一个跟随位后，
    /// 按 low 所在半区写出最后一个有效比特及其跟随位，最后把不足
    /// 一字节的部分补 0。
    pub fn finish(mut self) -> Result<(u64, u64), Error> {
        self.encode_symbol(EOF_SYMBOL)?;
        self.bits_to_follow += 1;
        if self.low < FIRST_QTR {
            self.emit_bit_plus_follow(false)?;
        } else {
            self.emit_bit_plus_follow(true)?;
        }
        self.out.flush().map_err(Error::io)?;
        Ok((self.model.rescale_count(), self.out.bytes_written()))
    }
}
