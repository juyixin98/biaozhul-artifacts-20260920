//! 摘要工具：SHA-256 计算与规范化。

use sha2::{Digest, Sha256};

use crate::error::{AppError, AppResult};

/// 计算内容的完整摘要标识：`sha256:<64 位小写十六进制>`。
pub fn sha256_digest(content: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content);
    let bytes = hasher.finalize();
    format!("sha256:{}", hex::encode(bytes))
}

/// 取出十六进制部分（小写），前缀大小写不敏感。
pub fn hex_part(digest: &str) -> &str {
    if let Some(rest) = digest
        .get(0..7)
        .filter(|p| p.eq_ignore_ascii_case("sha256:"))
    {
        let _ = rest;
        &digest[7..]
    } else {
        digest
    }
}

/// 把客户端传入的摘要（可带/不带 `sha256:` 前缀，允许大写）
/// 规范化为 `sha256:<lower-hex>`；非法返回 400。
pub fn normalize_digest(input: &str) -> AppResult<String> {
    let trimmed = input.trim();
    let hex = hex_part(trimmed).trim().to_ascii_lowercase();
    if hex.len() != 64 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(AppError::bad_request(format!(
            "invalid sha256 digest: {input:?}; expected 64 hex chars optionally prefixed by 'sha256:'"
        )));
    }
    Ok(format!("sha256:{hex}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_prefix_case_and_whitespace() {
        let d = sha256_digest(b"abc");
        let raw = hex_part(&d);
        assert_eq!(normalize_digest(&format!("SHA256:{raw}")).unwrap(), d);
        assert_eq!(normalize_digest(&raw.to_uppercase()).unwrap(), d);
        assert_eq!(normalize_digest(&format!("  {d}  ")).unwrap(), d);
    }

    #[test]
    fn rejects_bad_digest() {
        assert!(normalize_digest("sha256:nope").is_err());
        assert!(normalize_digest("zz").is_err());
    }

    #[test]
    fn known_vector_abc() {
        // sha256("abc") 标准测试向量
        assert_eq!(
            sha256_digest(b"abc"),
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
