//! `.chfc` 分块容器格式。
//!
//! # 格式规范（版本 1）
//!
//! 所有整数字段大端；头部字段均为字节对齐，码流内部为 MSB-first 位流。
//!
//! ```text
//! 文件前奏（6 字节）
//!   0  4  magic = b"CHFC"
//!   4  1  version = 0x01
//!   5  1  global_flags：保留，必须为 0x00
//!
//! 随后是一个或多个块（block），最后一块带 BFINAL：
//!   块头（19 字节，字节对齐）
//!     0  1  block_flags：bit0=BFINAL，bit1=SINGLE，其余保留为 0
//!     1  8  original_len：本块原始字节数 u64
//!     9  8  payload_bits：本块码流有效位数 u64
//!     17 2  num_symbols：码长表条目数 u16（0..=256）
//!   码长表：num_symbols 个 (symbol:u8, length:u8)，symbol 严格升序
//!   码流：ceil(payload_bits/8) 字节
//! ```
//!
//! 特殊情形：
//! - **空输入**：恰好一个块，BFINAL=1，num_symbols=0，三個长度字段全 0，
//!   无码长表、无码流。
//! - **单符号块**：SINGLE=1，num_symbols=1，表中 length 必须为 1，
//!   且 `payload_bits == original_len`；码流由 original_len 个 `0` 位
//!   组成（规范单符号码字约定为 `0`，按字节存放时即全零字节）。
//! - 多符号块：SINGLE=0，num_symbols≥2；编码器产生的码长表总是 Kraft
//!   完备的；解码器可按策略拒绝或放行不完整码表（见
//!   [`crate::stream::IncompletePolicy`]）。
//! - 码流末字节中超出 `payload_bits` 的位为填充位，必须为 0。
//! - BFINAL 块之后不允许再有任何字节。
//!
//! 内存有界：每块原始长度与每块表大小都由 [`crate::stream::Limits`] 约束；
//! 码流以流式位读取器逐位消费，不在内存中缓冲整块码流。

use crate::error::{Error, Result};
use crate::huffman::LengthTable;
use std::io::{Read, Write};

/// 魔数。
pub const MAGIC: [u8; 4] = *b"CHFC";
/// 当前格式版本。
pub const VERSION: u8 = 1;
/// 文件前奏长度。
pub const PRELUDE_LEN: usize = 6;
/// 块头长度。
pub const BLOCK_HEADER_LEN: usize = 19;

/// block_flags：最后一块。
pub const FLAG_BFINAL: u8 = 0x01;
/// block_flags：单符号块。
pub const FLAG_SINGLE: u8 = 0x02;

/// 写文件前奏。
pub fn write_prelude<W: Write>(w: &mut W) -> Result<()> {
    w.write_all(&MAGIC).map_err(io_err)?;
    w.write_all(&[VERSION, 0x00]).map_err(io_err)
}

/// 读并校验文件前奏。
pub fn read_prelude<R: Read>(r: &mut R) -> Result<()> {
    let mut buf = [0u8; PRELUDE_LEN];
    read_exact(r, &mut buf, Error::TruncatedHeader)?;
    if buf[0..4] != MAGIC {
        return Err(Error::BadMagic);
    }
    if buf[4] != VERSION {
        return Err(Error::UnsupportedVersion(buf[4]));
    }
    if buf[5] != 0 {
        return Err(Error::ReservedBitsSet(buf[5]));
    }
    Ok(())
}

/// 块头。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockHeader {
    pub bfinal: bool,
    pub single: bool,
    pub original_len: u64,
    pub payload_bits: u64,
    pub num_symbols: u16,
}

/// 写块头。
pub fn write_block_header<W: Write>(w: &mut W, h: &BlockHeader) -> Result<()> {
    let mut flags = 0u8;
    if h.bfinal {
        flags |= FLAG_BFINAL;
    }
    if h.single {
        flags |= FLAG_SINGLE;
    }
    let mut buf = [0u8; BLOCK_HEADER_LEN];
    buf[0] = flags;
    buf[1..9].copy_from_slice(&h.original_len.to_be_bytes());
    buf[9..17].copy_from_slice(&h.payload_bits.to_be_bytes());
    buf[17..19].copy_from_slice(&h.num_symbols.to_be_bytes());
    w.write_all(&buf).map_err(io_err)
}

/// 读块头；EOF（连一个字节都读不到）由调用方通过 `eof_ok` 区分。
pub fn read_block_header<R: Read>(r: &mut R) -> Result<Option<BlockHeader>> {
    let mut first = [0u8; 1];
    match r.read_exact(&mut first) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(io_err(e)),
    }
    let mut rest = [0u8; BLOCK_HEADER_LEN - 1];
    read_exact(r, &mut rest, Error::TruncatedHeader)?;

    let flags = first[0];
    if flags & !(FLAG_BFINAL | FLAG_SINGLE) != 0 {
        return Err(Error::ReservedBitsSet(flags));
    }
    let original_len = u64::from_be_bytes(rest[0..8].try_into().unwrap());
    let payload_bits = u64::from_be_bytes(rest[8..16].try_into().unwrap());
    let num_symbols = u16::from_be_bytes(rest[16..18].try_into().unwrap());

    let header = BlockHeader {
        bfinal: flags & FLAG_BFINAL != 0,
        single: flags & FLAG_SINGLE != 0,
        original_len,
        payload_bits,
        num_symbols,
    };
    validate_block_header(&header)?;
    Ok(Some(header))
}

/// 块头字段间自洽性校验（不依赖码长表的部分）。
fn validate_block_header(h: &BlockHeader) -> Result<()> {
    if h.num_symbols > 256 {
        return Err(Error::InvalidLengthTable("num_symbols > 256"));
    }
    match h.num_symbols {
        0 => {
            if h.single {
                return Err(Error::InvalidLengthTable(
                    "SINGLE flag requires exactly one symbol",
                ));
            }
            if h.original_len != 0 || h.payload_bits != 0 {
                return Err(Error::InvalidLengthTable(
                    "empty block must have zero lengths",
                ));
            }
            if !h.bfinal {
                return Err(Error::InvalidLengthTable(
                    "empty block is only legal as the final block",
                ));
            }
        }
        1 => {
            if !h.single {
                return Err(Error::InvalidLengthTable(
                    "num_symbols=1 requires SINGLE flag",
                ));
            }
            if h.original_len != h.payload_bits {
                return Err(Error::InvalidLengthTable(
                    "single-symbol block: payload_bits must equal original_len",
                ));
            }
        }
        _ => {
            if h.single {
                return Err(Error::InvalidLengthTable(
                    "SINGLE flag requires exactly one symbol",
                ));
            }
        }
    }
    Ok(())
}

/// 写码长表（仅写存在的条目，按 symbol 升序）。
pub fn write_length_table<W: Write>(w: &mut W, lengths: &LengthTable) -> Result<()> {
    for (sym, &len) in lengths.iter().enumerate() {
        if len != 0 {
            w.write_all(&[sym as u8, len]).map_err(io_err)?;
        }
    }
    Ok(())
}

/// 读码长表。
///
/// 校验：symbol 严格升序、length∈1..=255；单符号块 length 必须为 1。
pub fn read_length_table<R: Read>(r: &mut R, num_symbols: u16) -> Result<LengthTable> {
    let n = num_symbols as usize;
    let mut bytes = vec![0u8; n.checked_mul(2).ok_or(Error::TruncatedHeader)?];
    read_exact(r, &mut bytes, Error::TruncatedHeader)?;

    let mut lengths = [0u8; 256];
    let mut prev: i16 = -1;
    for i in 0..n {
        let symbol = bytes[2 * i];
        let length = bytes[2 * i + 1];
        if symbol as i16 <= prev {
            return Err(Error::InvalidLengthTable(
                "symbols must be strictly increasing",
            ));
        }
        prev = symbol as i16;
        if length == 0 || length as u16 > crate::huffman::MAX_CODE_LEN as u16 {
            return Err(Error::InvalidLengthTable("length out of range 1..=255"));
        }
        if n == 1 && length != 1 {
            return Err(Error::InvalidLengthTable(
                "single-symbol table must use length 1",
            ));
        }
        lengths[symbol as usize] = length;
    }
    Ok(lengths)
}

/// 严格读满 `buf`，EOF 映射为给定错误。
pub(crate) fn read_exact<R: Read>(r: &mut R, buf: &mut [u8], eof: Error) -> Result<()> {
    r.read_exact(buf).map_err(|e| {
        if e.kind() == std::io::ErrorKind::UnexpectedEof {
            eof
        } else {
            io_err(e)
        }
    })
}

/// io 错误映射（保留文本，错误类型保持可克隆/可比较）。
pub(crate) fn io_err(e: std::io::Error) -> Error {
    Error::Io(e.kind().to_string())
}
