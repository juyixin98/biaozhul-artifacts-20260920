//! LEB128（unsigned base-128）变长整数：小端，每字节低 7 位载荷，最高位表示后续还有字节。
//!
//! u64 最长编码为 10 字节；超过即判为非法。

use crate::error::{Error, Result};

/// 将 `v` 追加编码到 `out`，返回写入的字节数。
pub fn write_u64(out: &mut Vec<u8>, mut v: u64) -> usize {
    let start = out.len();
    loop {
        let mut byte = (v & 0x7f) as u8;
        v >>= 7;
        if v != 0 {
            byte |= 0x80;
        }
        out.push(byte);
        if v == 0 {
            break;
        }
    }
    out.len() - start
}

/// 从 `buf[*pos..]` 读取一个 u64，推进 `*pos`。
///
/// 规则：
/// - 最多 10 字节；
/// - 第 10 字节只允许最低 1 位有载荷（否则溢出 u64）；
/// - 非终止字节后必须还有数据（截断即错）。
pub fn read_u64(buf: &[u8], pos: &mut usize) -> Result<u64> {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    for i in 0..10u32 {
        if *pos >= buf.len() {
            return Err(Error::UnexpectedEof {
                wanted: 1,
                got: buf.len().saturating_sub(*pos),
            });
        }
        let byte = buf[*pos];
        *pos += 1;
        let payload = u64::from(byte & 0x7f);
        if i == 9 {
            // 最后一个字节：只能有 1 位载荷，且必须是终止字节。
            if byte & 0x80 != 0 || payload > 1 {
                return Err(Error::BadVarint);
            }
        }
        result |= payload
            .checked_mul(1u64.checked_shl(shift).ok_or(Error::BadVarint)?)
            .ok_or(Error::BadVarint)?;
        if byte & 0x80 == 0 {
            return Ok(result);
        }
        shift += 7;
    }
    // 连续 10 个续位字节仍未结束。
    Err(Error::BadVarint)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_basic() {
        for v in [
            0u64,
            1,
            63,
            64,
            127,
            128,
            255,
            16383,
            16384,
            u32::MAX as u64,
            u64::MAX,
        ] {
            let mut buf = Vec::new();
            let n = write_u64(&mut buf, v);
            assert_eq!(n, buf.len());
            let mut pos = 0;
            assert_eq!(read_u64(&buf, &mut pos).unwrap(), v);
            assert_eq!(pos, buf.len());
        }
    }

    #[test]
    fn known_encodings() {
        let mut buf = Vec::new();
        write_u64(&mut buf, 0);
        assert_eq!(buf, vec![0x00]);
        buf.clear();
        write_u64(&mut buf, 127);
        assert_eq!(buf, vec![0x7f]);
        buf.clear();
        write_u64(&mut buf, 128);
        assert_eq!(buf, vec![0x80, 0x01]);
        buf.clear();
        write_u64(&mut buf, 300);
        assert_eq!(buf, vec![0xac, 0x02]);
    }

    #[test]
    fn rejects_truncated() {
        assert!(matches!(
            read_u64(&[], &mut 0),
            Err(Error::UnexpectedEof { .. })
        ));
        assert!(matches!(
            read_u64(&[0x80], &mut 0),
            Err(Error::UnexpectedEof { .. })
        ));
    }

    #[test]
    fn rejects_overflow() {
        // 10 字节，终止字节载荷为 2（需要 65 位）。
        let bad = vec![0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x02];
        assert!(matches!(read_u64(&bad, &mut 0), Err(Error::BadVarint)));
        // 11 个续位字节。
        let bad2 = vec![0x80; 11];
        assert!(matches!(read_u64(&bad2, &mut 0), Err(Error::BadVarint)));
    }
}
