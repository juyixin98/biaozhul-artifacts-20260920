//! GREASE（RFC 8701）值识别。
//!
//! GREASE 是客户端随机填入的保留值，用来防止中间盒僵化（middlebox ossification）。
//! 观察器必须把 GREASE 与真正“未知”的协议元素区分开：
//! GREASE 重复出现是正常的，不能当作重复扩展/未知密码套件报错。

/// 判断一个 16 位值是否为 GREASE（RFC 8701 §3.1 的表，适用于版本、扩展、
/// 密码套件高低字节、组、签名算法等所有 16 位槽位）。
pub fn is_grease(v: u16) -> bool {
    matches!(
        v,
        0x0A0A
            | 0x1A1A
            | 0x2A2A
            | 0x3A3A
            | 0x4A4A
            | 0x5A5A
            | 0x6A6A
            | 0x7A7A
            | 0x8A8A
            | 0x9A9A
            | 0xAAAA
            | 0xBABA
            | 0xCACA
            | 0xDADA
            | 0xEAEA
            | 0xFAFA
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_all_grease_slots() {
        let mut high = 0x0Au16;
        while high <= 0xFA {
            // GREASE 值高低字节相同：0x0A0A, 0x1A1A, …, 0xFAFA。
            let v = (high << 8) | high;
            assert!(is_grease(v), "{v:#06x} should be GREASE");
            high += 0x10;
        }
        // 全空间穷举：恰好 16 个 GREASE 值。
        let count = (0..=u16::MAX).filter(|&v| is_grease(v)).count();
        assert_eq!(count, 16);
    }

    #[test]
    fn non_grease_values_ignored() {
        // 真实扩展/套件：SNI=0、ALPN=16、supported_versions=43、TLS_AES_128_GCM=0x1301。
        for v in [0x0000, 0x0010, 0x002b, 0x1301, 0x002f, 0xffff] {
            assert!(!is_grease(v), "{v:#06x} must not be GREASE");
        }
        // 形似但字节不匹配的值。
        for v in [0x0A0B, 0x1B1A, 0x2A3A, 0x000A] {
            assert!(!is_grease(v));
        }
    }
}
