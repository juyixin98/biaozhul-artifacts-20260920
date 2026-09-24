//! 记录的二进制格式与 CRC 校验。
//!
//! 磁盘布局(小端):
//!
//! ```text
//! +-----------+-----------+-----------+-----------+===========+
//! | magic u32 | len   u32 | seq   u64 | crc   u32 | payload   |
//! | "SGL1"    | payload长 | 序号      | CRC32     | len 字节  |
//! +-----------+-----------+-----------+-----------+===========+
//! |---------------- 20 字节头部 ----------------|
//! ```
//!
//! CRC32 覆盖 `seq(8 字节 LE) || payload`,可检测序号与数据被篡改/损坏。

/// 记录魔数,用于识别错位或垃圾数据。
pub const MAGIC: u32 = 0x5347_4C31; // "SGL1"
/// 固定头部长度:magic(4) + len(4) + seq(8) + crc(4)。
pub const HEADER_LEN: usize = 20;
/// 单条记录 payload 上限,防止损坏的长度字段触发超大内存分配。
pub const MAX_PAYLOAD: usize = 8 * 1024 * 1024;

/// 计算一条记录的 CRC32:覆盖 seq(LE) 与 payload。
pub fn record_crc(seq: u64, payload: &[u8]) -> u32 {
    let mut h = crc32fast::Hasher::new();
    h.update(&seq.to_le_bytes());
    h.update(payload);
    h.finalize()
}

/// 编码一条记录为 `header || payload`。
pub fn encode(seq: u64, payload: &[u8]) -> Vec<u8> {
    let crc = record_crc(seq, payload);
    let mut out = Vec::with_capacity(HEADER_LEN + payload.len());
    out.extend_from_slice(&MAGIC.to_le_bytes());
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    out.extend_from_slice(&seq.to_le_bytes());
    out.extend_from_slice(&crc.to_le_bytes());
    out.extend_from_slice(payload);
    out
}
