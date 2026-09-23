//! 无符号 LEB128 变长整数编码。

/// 将 `v` 以 LEB128 追加到 `out`。
pub fn put_uvarint(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

/// 从 `data` 起始处解码一个 LEB128 整数，返回 `(值, 消耗字节数)`。
/// 数据不足或编码非法（超过 10 字节 / 第 10 字节溢出）时返回 `None`。
pub fn get_uvarint(data: &[u8]) -> Option<(u64, usize)> {
    let mut v: u64 = 0;
    let mut shift = 0u32;
    for (i, &b) in data.iter().enumerate() {
        if i == 10 {
            return None; // 最长 10 字节（64 位）
        }
        if b < 0x80 {
            if i == 9 && b > 1 {
                return None; // 第 10 字节只允许 0/1，否则溢出
            }
            v |= (b as u64) << shift;
            return Some((v, i + 1));
        }
        v |= ((b & 0x7F) as u64) << shift;
        shift += 7;
    }
    None // 截断
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        for v in [
            0u64,
            1,
            127,
            128,
            300,
            16_384,
            u32::MAX as u64,
            u64::MAX,
            u64::MAX - 1,
        ] {
            let mut buf = Vec::new();
            put_uvarint(&mut buf, v);
            let (decoded, n) = get_uvarint(&buf).unwrap();
            assert_eq!(decoded, v);
            assert_eq!(n, buf.len());
        }
    }

    #[test]
    fn truncated_is_rejected() {
        assert_eq!(get_uvarint(&[]), None);
        assert_eq!(get_uvarint(&[0x80]), None);
        assert_eq!(get_uvarint(&[0xFF, 0xFF]), None);
    }

    #[test]
    fn overlong_is_rejected() {
        // 11 个 0x80：超过 64 位编码上限
        assert_eq!(get_uvarint(&[0x80; 11]), None);
        // 第 10 字节 > 1：溢出
        let mut buf = vec![0xFF; 9];
        buf.push(0x02);
        assert_eq!(get_uvarint(&buf), None);
    }
}
