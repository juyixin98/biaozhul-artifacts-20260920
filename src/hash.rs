//! SHA-256 内容寻址工具。
//!
//! 摘要序列化为小写十六进制字符串（64 字符），与 REAPI v2 的
//! `Digest{ hash, size_bytes }` 概念一致；本原型哈希算法固定为 SHA-256。

use sha2::{Digest, Sha256};

/// 对一段字节计算摘要。
pub fn digest_bytes(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

/// 判断字符串是否为合法的小写十六进制 SHA-256 摘要。
pub fn is_valid_hash(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vector_empty() {
        // SHA-256("")
        assert_eq!(
            digest_bytes(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn known_vector_abc() {
        assert_eq!(
            digest_bytes(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn hash_validation() {
        assert!(is_valid_hash(&digest_bytes(b"x")));
        assert!(!is_valid_hash("short"));
        // 大写不合法，避免同一对象出现两种键
        assert!(!is_valid_hash(
            "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
        ));
    }
}
