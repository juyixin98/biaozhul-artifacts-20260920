//! lzsw —— 流式 LZ77 滑动窗口二进制编解码库 + JSON 控制入口。
//!
//! - 格式规范：见 `docs/FORMAT.md`
//! - 编码器 [`encoder::Encoder`]：哈希链贪心匹配，内存有界
//! - 解码器 [`decoder::Decoder`]：回距校验、重叠复制、输出预算防压缩炸弹

pub mod base64;
pub mod decoder;
pub mod encoder;
pub mod error;
pub mod format;
pub mod json;

pub use decoder::Decoder;
pub use encoder::{Encoder, EncoderConfig};
pub use error::{Error, Result};

/// 一次性压缩便捷函数。
pub fn compress(data: &[u8], window: usize) -> Result<Vec<u8>> {
    let mut enc = Encoder::new(EncoderConfig {
        window,
        ..EncoderConfig::default()
    })?;
    let mut out = Vec::new();
    enc.feed(data, &mut out)?;
    enc.finish(&mut out)?;
    Ok(out)
}

/// 一次性解压便捷函数（输出预算 `max_output`）。
pub fn decompress(data: &[u8], max_output: u64) -> Result<Vec<u8>> {
    let mut dec = Decoder::new(max_output);
    let mut out = Vec::new();
    dec.feed(data, &mut out)?;
    dec.finish()?;
    Ok(out)
}
