//! 真实密码学操作：HMAC-SHA256 请求签名（协调器与目标桩共享密钥），
//! 以及目标链交易哈希（SHA-256 域分离）。非模拟、非占位。

use hmac::{Hmac, Mac};
use serde_json::Value;
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

pub fn hmac_hex(key: &str, message: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(key.as_bytes()).expect("HMAC accepts any key length");
    mac.update(message.as_bytes());
    hex::encode(mac.finalize().into_bytes())
}

/// 常量时间校验签名。
pub fn verify(key: &str, message: &str, signature_hex: &str) -> bool {
    let Ok(expected) = hex::decode(signature_hex) else {
        return false;
    };
    let Ok(mut mac) = HmacSha256::new_from_slice(key.as_bytes()) else {
        return false;
    };
    mac.update(message.as_bytes());
    mac.verify_slice(&expected).is_ok()
}

/// POST /submit 的待签名规范串（字段顺序固定，payload 为紧凑 JSON，字节确定）。
pub fn submit_canonical(
    relay_id: &str,
    fence: i32,
    nonce: i64,
    channel_id: &str,
    commit_id: &str,
    payload: &Value,
) -> String {
    let compact = serde_json::to_string(payload).expect("Value always serializes");
    format!(
        "POST\n/submit\n{relay_id}\n{fence}\n{nonce}\n{channel_id}\n{commit_id}\n{compact}"
    )
}

/// GET /receipts 的待签名规范串（带时间戳，允许 ±300s 时钟偏差）。
pub fn receipt_canonical(commit_id: &str, ts_unix: i64) -> String {
    format!("GET\n/receipts\n{commit_id}\n{ts_unix}")
}

/// 目标链风格的交易哈希：对提交要素做域分离 SHA-256，确定性且可复现。
pub fn tx_hash(channel_id: &str, nonce: i64, commit_id: &str, payload: &Value) -> String {
    let compact = serde_json::to_string(payload).expect("Value always serializes");
    let mut hasher = Sha256::new();
    hasher.update(b"relay-coord-target:v1\n");
    hasher.update(channel_id.as_bytes());
    hasher.update(b"\n");
    hasher.update(nonce.to_string().as_bytes());
    hasher.update(b"\n");
    hasher.update(commit_id.as_bytes());
    hasher.update(b"\n");
    hasher.update(compact.as_bytes());
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sign_and_verify_roundtrip() {
        let key = "test-secret";
        let p = json!({"a": 1});
        let c = submit_canonical("r1", 3, 7, "ch", "c-1", &p);
        let sig = hmac_hex(key, &c);
        assert!(verify(key, &c, &sig));
        // 篡改任一字段即验签失败
        assert!(!verify(key, &submit_canonical("r2", 3, 7, "ch", "c-1", &p), &sig));
        assert!(!verify(key, &submit_canonical("r1", 4, 7, "ch", "c-1", &p), &sig));
        assert!(!verify(key, &submit_canonical("r1", 3, 8, "ch", "c-1", &p), &sig));
        assert!(!verify("wrong", &c, &sig));
        assert!(!verify(key, &c, "not-hex"));
    }

    #[test]
    fn tx_hash_is_deterministic_and_distinct() {
        let p = json!({"x": 1});
        let h1 = tx_hash("ch", 0, "c-1", &p);
        let h2 = tx_hash("ch", 0, "c-1", &p);
        assert_eq!(h1, h2);
        assert_ne!(h1, tx_hash("ch", 1, "c-1", &p));
        assert_eq!(h1.len(), 64);
    }
}
