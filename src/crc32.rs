//! CRC-32（IEEE 802.3 多项式 0xEDB88320 反射形式）。
//!
//! 用于磁盘页的损坏/撕裂写检测。自包含实现，避免引入额外依赖。
//! 表在首次使用时懒加载计算。

use std::sync::OnceLock;

fn table() -> &'static [u32; 256] {
    static TABLE: OnceLock<[u32; 256]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        let mut i = 0usize;
        while i < 256 {
            let mut c = i as u32;
            let mut k = 0;
            while k < 8 {
                c = if c & 1 != 0 {
                    0xEDB88320 ^ (c >> 1)
                } else {
                    c >> 1
                };
                k += 1;
            }
            t[i] = c;
            i += 1;
        }
        t
    })
}

/// 以初始值 0xFFFF_FFFF 计算并取反，与 zlib/PNG 的 CRC-32 一致。
pub fn crc32(data: &[u8]) -> u32 {
    let t = table();
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc = t[((crc ^ b as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::crc32;

    #[test]
    fn known_vectors() {
        // 标准 CRC-32 测试向量
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"123456789"), 0xCBF43926);
        assert_eq!(crc32(b"hello"), 0x3610a686);
    }
}
