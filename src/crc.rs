//! CRC-32 (IEEE 802.3) — 标准表驱动实现，与 zlib/PKZIP 的 CRC-32 结果一致。
//!
//! 多项式 0xEDB88320（反射），初值/最终异或均为 0xFFFFFFFF。
//! 纯标准库实现，避免引入外部依赖。

#[derive(Clone)]
pub struct Crc32 {
    state: u32,
}

/// 惰性构造的 256 项查表（const fn 表过于冗长，这里用 std::sync::OnceLock）。
static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();

fn table() -> &'static [u32; 256] {
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

impl Crc32 {
    pub fn new() -> Self {
        Crc32 { state: 0xFFFF_FFFF }
    }

    pub fn update(&mut self, bytes: &[u8]) {
        let t = table();
        let mut c = self.state;
        for &b in bytes {
            c = t[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
        }
        self.state = c;
    }

    pub fn finalize(self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }
}

impl Default for Crc32 {
    fn default() -> Self {
        Self::new()
    }
}

/// 便捷函数：一次性计算整段字节的 CRC-32。
pub fn checksum(data: &[u8]) -> u32 {
    let mut c = Crc32::new();
    c.update(data);
    c.finalize()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 标准校验向量：CRC32("123456789") == 0xCBF43926。
    #[test]
    fn known_vector() {
        assert_eq!(checksum(b"123456789"), 0xCBF4_3926);
        assert_eq!(checksum(b""), 0);
    }

    #[test]
    fn incremental_equals_one_shot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let mut c = Crc32::new();
        c.update(&data[..10]);
        c.update(&data[10..]);
        assert_eq!(c.finalize(), checksum(data));
    }

    #[test]
    fn detects_single_bit_flip() {
        let mut buf = vec![0xABu8; 5000];
        let good = checksum(&buf);
        buf[4242] ^= 1;
        assert_ne!(checksum(&buf), good);
    }
}
