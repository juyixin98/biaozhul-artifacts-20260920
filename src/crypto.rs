//! 规范化编码与 Ed25519 签名/验签。
//!
//! 使用成熟库：
//! - `ed25519-dalek`（RustCrypto 维护的 Ed25519 实现）负责签名算法；
//! - `sha2` 负责 SHA-256 材料/输出摘要；
//! - 规范化消息采用固定字段顺序的紧凑 JSON（serde_json 保证结构体字段
//!   按声明顺序序列化），消除空白与键序二义性。

use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use serde::Serialize;

use crate::model::Statement;

/// [`canonical_message`] 内部使用的固定顺序投影，避免被 `Statement` 字段
/// 顺序的偶然变动影响签名语义。
#[derive(Serialize)]
struct CanonicalStatement<'a> {
    builder_id: &'a str,
    source_commit: CanonicalSource<'a>,
    materials: Vec<CanonicalMaterial<'a>>,
    output: CanonicalOutput<'a>,
}

#[derive(Serialize)]
struct CanonicalSource<'a> {
    repo: &'a str,
    revision: &'a str,
}

#[derive(Serialize)]
struct CanonicalMaterial<'a> {
    uri: &'a str,
    digest: CanonicalDigest<'a>,
}

#[derive(Serialize)]
struct CanonicalOutput<'a> {
    digest: CanonicalDigest<'a>,
}

#[derive(Serialize)]
struct CanonicalDigest<'a> {
    alg: &'a str,
    hex: &'a str,
}

/// 生成需要签名/验签的规范化字节。
pub fn canonical_message(st: &Statement) -> Vec<u8> {
    let canonical = CanonicalStatement {
        builder_id: &st.builder_id,
        source_commit: CanonicalSource {
            repo: &st.source_commit.repo,
            revision: &st.source_commit.revision,
        },
        materials: st
            .materials
            .iter()
            .map(|m| CanonicalMaterial {
                uri: &m.uri,
                digest: CanonicalDigest {
                    alg: &m.digest.alg,
                    hex: &m.digest.hex,
                },
            })
            .collect(),
        output: CanonicalOutput {
            digest: CanonicalDigest {
                alg: &st.output.digest.alg,
                hex: &st.output.digest.hex,
            },
        },
    };
    serde_json::to_vec(&canonical).expect("canonical serialization is infallible")
}

/// 对证明进行签名，返回十六进制签名。`signing_key_hex` 为 32 字节种子的 hex。
pub fn sign_statement(sk_hex: &str, st: &Statement) -> Result<String, CryptoError> {
    let seed = hex::decode(sk_hex).map_err(|_| CryptoError::BadKey("签名种子不是合法 hex"))?;
    let seed: [u8; 32] = seed
        .as_slice()
        .try_into()
        .map_err(|_| CryptoError::BadKey("签名种子必须为 32 字节"))?;
    let sk = SigningKey::from_bytes(&seed);
    let sig: Signature = sk.sign(&canonical_message(st));
    Ok(hex::encode(sig.to_bytes()))
}

/// 验签。`vk_hex` 为 32 字节 Ed25519 公钥 hex。
pub fn verify_signature(vk_hex: &str, st: &Statement, sig_hex: &str) -> Result<(), CryptoError> {
    let vk_bytes = hex::decode(vk_hex).map_err(|_| CryptoError::BadKey("公钥不是合法 hex"))?;
    let vk_bytes: [u8; 32] = vk_bytes
        .as_slice()
        .try_into()
        .map_err(|_| CryptoError::BadKey("公钥必须为 32 字节"))?;
    let vk = VerifyingKey::from_bytes(&vk_bytes)
        .map_err(|_| CryptoError::BadKey("公钥不是合法的 Edwards 曲线点"))?;

    let sig_bytes = hex::decode(sig_hex).map_err(|_| CryptoError::BadSignature)?;
    let sig_bytes: [u8; 64] = sig_bytes
        .as_slice()
        .try_into()
        .map_err(|_| CryptoError::BadSignature)?;
    let sig = Signature::from_bytes(&sig_bytes);

    vk.verify(&canonical_message(st), &sig)
        .map_err(|_| CryptoError::VerifyFailed)
}

/// 计算内容摘要（hex）。当前固定使用 SHA-256。
pub fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CryptoError {
    BadKey(&'static str),
    BadSignature,
    VerifyFailed,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_then_verify_roundtrip() {
        let sk = "00".repeat(32);
        let vk_hex = hex::encode(
            SigningKey::from_bytes(&[0u8; 32])
                .verifying_key()
                .to_bytes(),
        );
        let st = crate::fixture::data::sample_statement();
        let sig = sign_statement(&sk, &st).unwrap();
        verify_signature(&vk_hex, &st, &sig).unwrap();
    }

    #[test]
    fn verify_rejects_tampered_payload() {
        let sk = "11".repeat(32);
        let vk_hex = hex::encode(
            SigningKey::from_bytes(&[0x11u8; 32])
                .verifying_key()
                .to_bytes(),
        );
        let mut st = crate::fixture::data::sample_statement();
        let sig = sign_statement(&sk, &st).unwrap();
        st.builder_id = "evil.example".to_string();
        assert_eq!(
            verify_signature(&vk_hex, &st, &sig),
            Err(CryptoError::VerifyFailed)
        );
    }

    #[test]
    fn canonical_message_is_deterministic() {
        let st = crate::fixture::data::sample_statement();
        assert_eq!(canonical_message(&st), canonical_message(&st));
        let v: serde_json::Value = serde_json::from_slice(&canonical_message(&st)).unwrap();
        assert_eq!(v["builder_id"], "trusted-builder-a");
    }
}
