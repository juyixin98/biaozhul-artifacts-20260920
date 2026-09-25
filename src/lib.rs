//! 自适应算术编码（adaptive arithmetic coding）纯后端库。
//!
//! - order-0 自适应频率模型，重标定规则固定（见 [`model`]）
//! - 32 位整数区间算术编码，进位采用 bits-plus-follow 方案
//! - 终止符（EOF 符号）由编码器显式编码、解码器显式识别
//! - 流式处理：编解码均为 O(1) 内存（固定 8 KiB I/O 缓冲 + 约 2 KiB 模型表）
//! - 通过 [`Limits`] 限制输入长度与解码输出长度
//!
//! 比特级格式规范见仓库根目录 `FORMAT.md`。

mod bitio;
mod decoder;
mod encoder;
mod error;
mod model;

pub use error::Error;
pub use model::{AdaptiveModel, EOF_SYMBOL, MAX_TOTAL_FREQ, NUM_SYMBOLS};

use std::io::{Read, Write};

/// 内部 I/O 缓冲大小（字节）。编解码内存占用与数据长度无关。
const IO_BUF_SIZE: usize = 8192;

/// 资源限制。所有限制都是闭区间上限：超过即报错。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// 编码输入的最大字节数。
    pub max_input_bytes: u64,
    /// 编码输出 / 解码输出的最大字节数。
    pub max_output_bytes: u64,
    /// 解码时输入耗尽后允许补读的零比特数上限，超过即判定截断流。
    /// 合法流该值至多为 32（解码器预载寄存器宽度），默认 64 留有余量。
    pub max_zero_bits: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_input_bytes: 64 * 1024 * 1024,
            max_output_bytes: 64 * 1024 * 1024,
            max_zero_bits: 64,
        }
    }
}

/// 一次编码或解码的统计信息。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CodecStats {
    /// 编码：消费的明文字节数；解码：从压缩流读取的字节数（含缓冲预读）。
    pub input_bytes: u64,
    /// 编码：产出的压缩字节数；解码：产出的明文字节数。
    pub output_bytes: u64,
    /// 本次处理中模型发生重标定（频率折半）的次数。
    pub rescale_count: u64,
}

/// 流式编码：`input` 中的全部字节编码为算术码流写入 `output`。
///
/// 输入按 8 KiB 分块读取，内存占用 O(1)。超过 `limits` 即返回错误。
pub fn encode<R: Read, W: Write>(
    mut input: R,
    output: W,
    limits: &Limits,
) -> Result<CodecStats, Error> {
    let mut encoder = encoder::Encoder::new(output, limits.max_output_bytes);
    let mut buf = [0u8; IO_BUF_SIZE];
    let mut input_bytes = 0u64;
    loop {
        let n = input.read(&mut buf).map_err(Error::io)?;
        if n == 0 {
            break;
        }
        input_bytes += n as u64;
        if input_bytes > limits.max_input_bytes {
            return Err(Error::InputLimitExceeded);
        }
        for &byte in &buf[..n] {
            encoder.encode_byte(byte)?;
        }
    }
    let (rescale_count, output_bytes) = encoder.finish()?;
    Ok(CodecStats {
        input_bytes,
        output_bytes,
        rescale_count,
    })
}

/// 流式解码：从 `input` 读取算术码流，解出的明文写入 `output`。
///
/// 解码在解出 EOF 符号时停止；输出长度受 `limits.max_output_bytes` 限制，
/// 输入耗尽后补读的零比特数受 `limits.max_zero_bits` 限制（超出判为截断流）。
pub fn decode<R: Read, W: Write>(
    input: R,
    mut output: W,
    limits: &Limits,
) -> Result<CodecStats, Error> {
    let mut decoder = decoder::Decoder::new(input, limits.max_zero_bits)?;
    let mut output_bytes = 0u64;
    loop {
        match decoder.next_byte()? {
            None => break,
            Some(byte) => {
                output_bytes += 1;
                if output_bytes > limits.max_output_bytes {
                    return Err(Error::OutputLimitExceeded);
                }
                output.write_all(&[byte]).map_err(Error::io)?;
            }
        }
    }
    output.flush().map_err(Error::io)?;
    Ok(CodecStats {
        input_bytes: decoder.bytes_read(),
        output_bytes,
        rescale_count: decoder.rescale_count(),
    })
}
