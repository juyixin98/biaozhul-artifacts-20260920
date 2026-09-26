//! CRC-32（IEEE 802.3 多项式 `0xEDB88320` 反射形式），用于段帧完整性校验。
//!
//! 纯软件查表实现，表在常量上下文中构建，无三方依赖。

const POLY: u32 = 0xEDB8_8320;

const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut crc = i as u32;
        let mut j = 0;
        while j < 8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ POLY
            } else {
                crc >> 1
            };
            j += 1;
        }
        table[i] = crc;
        i += 1;
    }
    table
}

const TABLE: [u32; 256] = build_table();

/// 计算 `data` 的 CRC-32（初始值 0xFFFF_FFFF，结果取反）。
pub fn checksum(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        let idx = ((crc ^ u32::from(b)) & 0xff) as usize;
        crc = (crc >> 8) ^ TABLE[idx];
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        // CRC-32/ISO-HDLC 标准向量。
        assert_eq!(checksum(b""), 0x0000_0000);
        assert_eq!(checksum(b"123456789"), 0xCBF4_3926);
        assert_eq!(checksum(b"a"), 0xE8B7_BE43);
    }

    #[test]
    fn detects_flip() {
        let a = checksum(b"hello world");
        let mut v = b"hello world".to_vec();
        v[0] = b'j';
        assert_ne!(a, checksum(&v));
    }
}
