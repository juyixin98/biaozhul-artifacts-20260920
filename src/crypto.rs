//! HMAC-SHA256 请求签名：真实密码学运算（hmac + sha2 crate），恒定时间比较（subtle）。
//!
//! 签名串（以 `\n` 分隔）：
//! ```text
//! POST
//! <实际请求路径，含 query>
//! <X-Sim-At 头，wall 模式下为空串>
//! <原始请求体字节>
//! ```
//! 客户端计算 hex(HMAC-SHA256(secret, 签名串))，放入 `X-Signature: hex=...`。

use base64::Engine as _;
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

fn mac_for(secret: &[u8]) -> HmacSha256 {
    HmacSha256::new_from_slice(secret).expect("HMAC accepts keys of any size")
}

/// 计算签名（hex 小写）。
pub fn sign_hex(secret: &[u8], method: &str, path: &str, sim_at: &str, body: &[u8]) -> String {
    let mut mac = mac_for(secret);
    mac.update(method.as_bytes());
    mac.update(b"\n");
    mac.update(path.as_bytes());
    mac.update(b"\n");
    mac.update(sim_at.as_bytes());
    mac.update(b"\n");
    mac.update(body);
    hex::encode(mac.finalize().into_bytes())
}

/// 校验客户端签名。接受 `hex=<sig>` 或裸 `<sig>`。比较恒定时间。
pub fn verify_hex(
    secret: &[u8],
    provided: &str,
    method: &str,
    path: &str,
    sim_at: &str,
    body: &[u8],
) -> bool {
    let provided = provided.trim();
    let provided = provided
        .strip_prefix("hex=")
        .or_else(|| provided.strip_prefix("sha256="))
        .unwrap_or(provided);
    let provided_bytes = match hex::decode(provided) {
        Ok(b) if b.len() == 32 => b,
        _ => return false,
    };
    let expected = sign_hex(secret, method, path, sim_at, body);
    let expected_bytes = hex::decode(expected).expect("our own hex is valid");
    subtle::ConstantTimeEq::ct_eq(&provided_bytes[..], &expected_bytes[..]).into()
}

/// 解析 base64/hex 密钥（仅用于让配置能承载任意二进制；默认开发密钥是普通 UTF-8 字符串）。
/// - 以 `hex:` 开头 → 十六进制解码
/// - 以 `b64:` 开头 → 标准 base64 解码
/// - 其他 → 直接按 UTF-8 字节使用
pub fn decode_key(spec: &str) -> Result<Vec<u8>, String> {
    if let Some(h) = spec.strip_prefix("hex:") {
        hex::decode(h).map_err(|e| format!("invalid hex key: {e}"))
    } else if let Some(b) = spec.strip_prefix("b64:") {
        base64::engine::general_purpose::STANDARD
            .decode(b)
            .map_err(|e| format!("invalid base64 key: {e}"))
    } else {
        Ok(spec.as_bytes().to_vec())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_then_verify_roundtrip() {
        let key = b"dev-remote-secret";
        let sig = sign_hex(key, "POST", "/v1/sources/remote/commands", "1000", b"{}");
        assert!(verify_hex(
            key,
            &format!("hex={sig}"),
            "POST",
            "/v1/sources/remote/commands",
            "1000",
            b"{}"
        ));
    }

    #[test]
    fn tampered_body_or_path_is_rejected() {
        let key = b"k";
        let sig = sign_hex(key, "POST", "/p", "", b"body");
        assert!(!verify_hex(key, &sig, "POST", "/p", "", b"tampered"));
        assert!(!verify_hex(key, &sig, "POST", "/other", "", b"body"));
        assert!(!verify_hex(key, "hex=00", "POST", "/p", "", b"body"));
        assert!(!verify_hex(key, "not-hex-at-all", "POST", "/p", "", b"body"));
    }

    #[test]
    fn key_encodings() {
        assert_eq!(decode_key("plain").unwrap(), b"plain");
        assert_eq!(decode_key("hex:00ff").unwrap(), vec![0x00, 0xff]);
        assert!(decode_key("hex:zz").is_err());
    }
}
