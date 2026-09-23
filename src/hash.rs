//! 可注入的哈希函数。
//!
//! 可扩展哈希只依赖 `u32` 散列值的位。为了在测试/演示中**确定性地复现碰撞**，
//! 这里提供三种模式：
//!
//! - [`HashKind::Fx`]：默认。一个快速、稳定（跨版本不变）的非加密散列，
//!   取自 rustc 的 FxHash 思路；
//! - [`HashKind::Const`]：所有键都散列到同一个值。用于“全碰撞”验收：
//!   桶满后分裂永远无法分离记录，必须返回明确的容量错误；
//! - [`HashKind::LowMod`]：`hash = 低质量混合(k) mod n`。用于制造
//!   “大量碰撞但最终可分裂分离”的受控场景（n 很小，如 2/3/4）。
//!
//! 哈希模式记录在索引文件头中：打开已有文件时必须与创建时一致，
//! 否则报错（防止用错散列导致目录定位错误）。

use crate::errors::{IndexError, Result};

/// 哈希模式（持久化在文件头中）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HashKind {
    /// 快速确定性散列。
    Fx,
    /// 全碰撞：所有键返回固定值（默认 0）。
    Const(u32),
    /// `mix(k) mod n`，n >= 1。
    LowMod(u32),
}

impl HashKind {
    /// 3 字节文件头编码：`[tag, a, b]`，参数按小端拼成 u16。
    pub(crate) fn encode(&self) -> [u8; 3] {
        match *self {
            HashKind::Fx => [0, 0, 0],
            HashKind::Const(v) => {
                let [a, b] = (v as u16).to_le_bytes();
                [1, a, b]
            }
            HashKind::LowMod(n) => {
                let [a, b] = (n as u16).to_le_bytes();
                [2, a, b]
            }
        }
    }

    pub(crate) fn decode(bytes: [u8; 3]) -> Result<HashKind> {
        let param = u16::from_le_bytes([bytes[1], bytes[2]]);
        match bytes[0] {
            0 => Ok(HashKind::Fx),
            1 => Ok(HashKind::Const(param as u32)),
            2 if param >= 1 => Ok(HashKind::LowMod(param as u32)),
            _ => Err(IndexError::Corrupt(format!("未知哈希模式编码 {:?}", bytes))),
        }
    }

    /// 启动参数解析（`--hash` 的值）。
    ///
    /// 语法：`fx` | `const[:值]` | `mod:n`。
    pub fn parse(spec: &str) -> Result<HashKind> {
        let spec = spec.trim();
        if spec.eq_ignore_ascii_case("fx") {
            return Ok(HashKind::Fx);
        }
        if let Some(v) = spec
            .split_once(':')
            .filter(|(k, _)| k.eq_ignore_ascii_case("const"))
            .and_then(|(_, v)| v.parse::<u32>().ok())
        {
            return Ok(HashKind::Const(v));
        }
        if spec.eq_ignore_ascii_case("const") {
            return Ok(HashKind::Const(0));
        }
        if let Some(n) = spec
            .strip_prefix("mod:")
            .or_else(|| spec.strip_prefix("Mod:"))
            .and_then(|v| v.parse::<u32>().ok())
            .filter(|&n| n >= 1)
        {
            return Ok(HashKind::LowMod(n));
        }
        Err(IndexError::Corrupt(format!(
            "无法解析哈希模式 '{spec}'，可选：fx | const[:值] | mod:n（n>=1）"
        )))
    }

    /// 人类可读名称（用于统计接口与错误信息）。
    pub fn describe(&self) -> String {
        match self {
            HashKind::Fx => "fx".to_string(),
            HashKind::Const(v) => format!("const:{v}"),
            HashKind::LowMod(n) => format!("mod:{n}"),
        }
    }
}

/// 键哈希器接口。实现 [`std::hash::Hasher`] 太宽泛，这里只需要键字节 -> u32。
pub trait KeyHasher: Send + Sync {
    fn hash(&self, key: &[u8]) -> u32;
    fn kind(&self) -> HashKind;
}

/// 为 [`HashKind`] 构造一个零成本的具体哈希器。
pub fn hasher_for(kind: HashKind) -> Box<dyn KeyHasher> {
    match kind {
        HashKind::Fx => Box::new(FxHasher),
        HashKind::Const(v) => Box::new(ConstHasher(v)),
        HashKind::LowMod(n) => Box::new(LowModHasher(n)),
    }
}

/// 稳定的 Fx 风格散列（与 rustc FxHasher 相同常数）。
pub struct FxHasher;

impl KeyHasher for FxHasher {
    fn hash(&self, key: &[u8]) -> u32 {
        // FxHash：8 字节块吃 u64 常数，尾部字节按 FxHash 尾部规则处理。
        const K: u64 = 0x51_7c_c1_b7_27_22_0a_95;
        let mut h: u64 = 0;
        let mut rest = key;
        while rest.len() >= 8 {
            let mut buf = [0u8; 8];
            buf.copy_from_slice(&rest[..8]);
            h = (h.rotate_left(5) ^ u64::from_le_bytes(buf)).wrapping_mul(K);
            rest = &rest[8..];
        }
        if !rest.is_empty() {
            // 尾部：逐字节累积，保持确定性
            let mut tail = 0u64;
            for &b in rest {
                tail = tail.rotate_left(8) | b as u64;
            }
            h = (h.rotate_left(5) ^ tail).wrapping_mul(K);
        }
        // FxHash 对空输入也给出确定性值
        let r = (h as u32) ^ ((h >> 32) as u32);
        if r == 0 {
            1 // 避免与 Const(0) 完全同值带来的目录退化（极小概率）
        } else {
            r
        }
    }
    fn kind(&self) -> HashKind {
        HashKind::Fx
    }
}

/// 全碰撞哈希器。
pub struct ConstHasher(pub u32);

impl KeyHasher for ConstHasher {
    fn hash(&self, _key: &[u8]) -> u32 {
        self.0
    }
    fn kind(&self) -> HashKind {
        HashKind::Const(self.0)
    }
}

/// `mix mod n` 哈希器：受控碰撞、但最终可以靠继续分裂分离。
///
/// 余数 `r = fx(k) mod n` 映射到散列值**顶端 w = ceil(log2(n)) 位**
/// （高位决定目录槽，因此余数相同的键长期聚集在同一子树、小 n 下迫使目录
/// 倍增和局部深链）；其余 `32-w` 位完整保留 fx 原始熵，使同类不同键在
/// 通常一两次额外分裂后即可分离。
///
/// 与 [`ConstHasher`] 的区别：const 下全部 32 位相同，分裂永远无法分离；
/// LowMod 只让**高位前缀**碰撞，用于复现“大量碰撞但可恢复”的真实场景。
pub struct LowModHasher(pub u32);

impl KeyHasher for LowModHasher {
    fn hash(&self, key: &[u8]) -> u32 {
        let n = self.0;
        let raw = FxHasher.hash(key);
        // 覆盖 n 个类所需的最小位数。
        let w = u32::BITS - (n - 1).leading_zeros();
        let low_bits = 32 - w;
        let class = (raw % n) as u64;
        // 把类均摊到 2^w 个顶端编码上（n 非 2 的幂时部分编码留空）。
        let top = if w == 0 {
            0
        } else {
            (((class << w) / n as u64) as u32) << low_bits
        };
        let low_mask = if low_bits >= 32 {
            u32::MAX
        } else {
            (1u32 << low_bits) - 1
        };
        top | (raw & low_mask)
    }
    fn kind(&self) -> HashKind {
        HashKind::LowMod(self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn const_hasher_collides_everything() {
        let h = ConstHasher(7);
        assert_eq!(h.hash(b"a"), 7);
        assert_eq!(h.hash(b"anything"), 7);
    }

    #[test]
    fn lowmod_clusters_high_bits_but_keeps_low_entropy() {
        // n=2：bit31 把键分成两类；同类的低位仍有充分差异（再分裂可分离）。
        let h = LowModHasher(2);
        let mut hi = 0;
        let mut lo = 0;
        let mut same_class_low: std::collections::HashSet<u32> = std::collections::HashSet::new();
        for k in 0..2000u32 {
            let v = h.hash(format!("k{k}").as_bytes());
            if v & 0x8000_0000 != 0 {
                hi += 1;
            } else {
                lo += 1;
                same_class_low.insert(v & 0x7FFF_FFFF);
            }
        }
        assert!(hi > 0 && lo > 0, "两个余数类都应出现");
        assert!(
            same_class_low.len() > 2,
            "同类键的低位应保留熵，而不是全碰撞"
        );
        // 类标签必须与余数一致
        for k in 0..500u32 {
            let key = format!("x{k}");
            let raw = FxHasher.hash(key.as_bytes());
            let v = h.hash(key.as_bytes());
            assert_eq!((v >> 31) as u64, (raw % 2) as u64);
        }
    }

    #[test]
    fn fx_is_deterministic_and_spread() {
        let h = FxHasher;
        let a = h.hash(b"hello");
        let b = h.hash(b"hello");
        let c = h.hash(b"world");
        assert_eq!(a, b);
        assert_ne!(a, c);
    }

    #[test]
    fn kind_roundtrip() {
        for k in [HashKind::Fx, HashKind::Const(42), HashKind::LowMod(3)] {
            assert_eq!(HashKind::decode(k.encode()).unwrap(), k);
        }
        assert!(HashKind::decode([2, 0, 0]).is_err()); // mod:0 非法
    }

    #[test]
    fn parse_specs() {
        assert_eq!(HashKind::parse("fx").unwrap(), HashKind::Fx);
        assert_eq!(HashKind::parse("const").unwrap(), HashKind::Const(0));
        assert_eq!(HashKind::parse("const:9").unwrap(), HashKind::Const(9));
        assert_eq!(HashKind::parse("mod:2").unwrap(), HashKind::LowMod(2));
        assert!(HashKind::parse("mod:0").is_err());
        assert!(HashKind::parse("bogus").is_err());
    }
}
