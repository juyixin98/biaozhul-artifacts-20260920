//! CRC-32 (IEEE 802.3, 与 zlib/PNG 的 `CRC-32` 相同, 多项式 0xEDB88320 反射形式)。
//!
//! 自实现以减少外部依赖; 行为对照标准校验向量 `b"123456789" -> 0xCBF43926`。

#[rustfmt::skip]
const TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut crc = i as u32;
        let mut j = 0;
        while j < 8 {
            if crc & 1 != 0 {
                crc = (crc >> 1) ^ 0xEDB88320;
            } else {
                crc >>= 1;
            }
            j += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
};

/// 计算 `data` 的 CRC-32。可对帧的多个连续部分分段后再 `feed`。
///
/// `feed(0, data)`: 进入 feed 后先 `0 ^ 0xFFFF_FFFF` 得到标准初值,
/// 处理完再终态化, 正好等于 IEEE CRC-32。
pub fn crc32(data: &[u8]) -> u32 {
    feed(0, data)
}

/// 续算: 允许把 (len||seq) 头与 payload 分两块更新同一个 CRC。
pub fn feed(mut crc: u32, data: &[u8]) -> u32 {
    crc ^= 0xFFFF_FFFF;
    for &b in data {
        crc = (crc >> 8) ^ TABLE[((crc ^ b as u32) & 0xFF) as usize];
    }
    crc ^ 0xFFFF_FFFF
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn check_standard_vector() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn empty_is_ffffffff() {
        assert_eq!(crc32(b""), 0);
    }

    #[test]
    fn segmented_matches_contiguous() {
        let whole = crc32(b"hello segmented world");
        let mut c = crc32(b"hello ");
        c = feed(c, b"segmented ");
        c = feed(c, b"world");
        assert_eq!(c, whole);
    }
}
