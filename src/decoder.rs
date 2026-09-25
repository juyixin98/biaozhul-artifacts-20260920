//! 32 位整数区间算术解码器，与 [`crate::encoder`] 严格镜像：
//! 相同的区间更新公式、相同的归一化条件、相同的模型更新顺序。

use std::io::Read;

use crate::bitio::BitReader;
use crate::encoder::{CODE_VALUE_BITS, FIRST_QTR, HALF, THIRD_QTR, TOP_VALUE};
use crate::model::{AdaptiveModel, EOF_SYMBOL};
use crate::Error;

/// 算术解码器。反复调用 [`Decoder::next_byte`] 直到返回 `None`（EOF 符号）。
pub struct Decoder<R: Read> {
    low: u64,
    high: u64,
    value: u64,
    model: AdaptiveModel,
    inp: BitReader<R>,
}

impl<R: Read> Decoder<R> {
    /// 新建解码器并预载 32 位码值寄存器。
    pub fn new(inp: R, max_zero_bits: u32) -> Result<Self, Error> {
        let mut reader = BitReader::new(inp, max_zero_bits);
        let mut value = 0u64;
        for _ in 0..CODE_VALUE_BITS {
            value = (value << 1) | u64::from(reader.read_bit().map_err(Error::io)?);
        }
        Ok(Self {
            low: 0,
            high: TOP_VALUE,
            value,
            model: AdaptiveModel::new(),
            inp: reader,
        })
    }

    /// 从输入流累计读取的字节数（含缓冲预读）。
    pub fn bytes_read(&self) -> u64 {
        self.inp.bytes_read()
    }

    /// 模型已发生的重标定次数。
    pub fn rescale_count(&self) -> u64 {
        self.model.rescale_count()
    }

    /// 解出下一个字节；解出 EOF 终止符时返回 `None`。
    pub fn next_byte(&mut self) -> Result<Option<u8>, Error> {
        let total = u64::from(self.model.total());
        let range = self.high - self.low + 1;
        // 不变式：low <= value <= high，故 cum < total。
        let cum = ((self.value - self.low + 1) * total - 1) / range;
        let (sym, cum_low, cum_high) = self.model.find_symbol(cum as u32);

        self.high = self.low + (range * u64::from(cum_high)) / total - 1;
        self.low = self.low + (range * u64::from(cum_low)) / total;
        self.renormalize()?;
        self.model.update(sym);

        if sym == EOF_SYMBOL {
            Ok(None)
        } else {
            Ok(Some(sym as u8))
        }
    }

    /// 与编码器镜像的归一化：条件相同，区别只在把比特读入 value。
    fn renormalize(&mut self) -> Result<(), Error> {
        loop {
            if self.high < HALF {
                // 无需调整区间，直接移位。
            } else if self.low >= HALF {
                self.low -= HALF;
                self.high -= HALF;
                self.value -= HALF;
            } else if self.low >= FIRST_QTR && self.high < THIRD_QTR {
                self.low -= FIRST_QTR;
                self.high -= FIRST_QTR;
                self.value -= FIRST_QTR;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
            self.value = (self.value << 1) | u64::from(self.inp.read_bit().map_err(Error::io)?);
        }
        Ok(())
    }
}
