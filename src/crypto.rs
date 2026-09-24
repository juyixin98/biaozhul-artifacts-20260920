//! Ed25519 签名 / 验签与 DSSE 信封的构造。

use crate::types::{Envelope, Signature, Statement};
use anyhow::{anyhow, Context, Result};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey, Verifier, VerifyingKey};
use sha2::{Digest, Sha256};

/// 计算原始字节的 SHA-256 摘要。
pub fn sha256(data: &[u8]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().into()
}

/// 构建器持有的签名身份（私钥仅在“出具证明”侧使用；
/// 验证服务只加载公钥）。
pub struct SignerIdentity {
    pub keyid: String,
    signing_key: SigningKey,
}

impl SignerIdentity {
    /// 从 32 字节种子生成确定性密钥（夹具用，避免依赖随机数）。
    pub fn from_seed(keyid: impl Into<String>, seed: [u8; 32]) -> Self {
        SignerIdentity {
            keyid: keyid.into(),
            signing_key: SigningKey::from_bytes(&seed),
        }
    }

    pub fn verifying_key(&self) -> VerifyingKey {
        self.signing_key.verifying_key()
    }

    pub fn public_key_bytes(&self) -> [u8; 32] {
        self.signing_key.verifying_key().to_bytes()
    }

    /// 对陈述签名并产出 DSSE 风格信封。
    pub fn sign_statement(&self, statement: &Statement) -> Result<Envelope> {
        let payload_bytes = statement.canonical_bytes()?;
        let mut envelope = Envelope {
            payload_type: Envelope::PAYLOAD_TYPE.to_string(),
            payload: B64.encode(payload_bytes),
            signatures: vec![],
        };
        let message = envelope.pae()?;
        let signature = self.signing_key.sign(&message);
        envelope.signatures.push(Signature {
            keyid: self.keyid.clone(),
            sig: B64.encode(signature.to_bytes()),
        });
        Ok(envelope)
    }
}

/// 验证服务侧的公钥注册表：keyid（= 构建器 id）→ 公钥。
#[derive(Default)]
pub struct KeyRegistry {
    keys: std::collections::HashMap<String, VerifyingKey>,
}

impl KeyRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&mut self, keyid: impl Into<String>, key: VerifyingKey) {
        self.keys.insert(keyid.into(), key);
    }

    pub fn contains(&self, keyid: &str) -> bool {
        self.keys.contains_key(keyid)
    }

    /// 已注册公钥的 keyid 列表（排序后，便于展示）。
    pub fn registered_keyids(&self) -> Vec<String> {
        let mut ids: Vec<String> = self.keys.keys().cloned().collect();
        ids.sort();
        ids
    }

    /// 用注册表中的公钥校验信封签名。
    /// 返回 (keyid, 解析出的陈述)。
    pub fn verify_envelope(&self, envelope: &Envelope) -> Result<(String, Statement)> {
        let sig_entry = envelope
            .signatures
            .first()
            .ok_or_else(|| anyhow!("envelope carries no signatures"))?;
        if envelope.signatures.len() > 1 {
            return Err(anyhow!("multiple signatures not supported by demo"));
        }
        let public_key = self
            .keys
            .get(&sig_entry.keyid)
            .ok_or_else(|| anyhow!("no public key registered for keyid `{}`", sig_entry.keyid))?;

        let sig_bytes = B64
            .decode(&sig_entry.sig)
            .context("signature is not valid base64")?;
        let sig_array: [u8; 64] = sig_bytes
            .as_slice()
            .try_into()
            .map_err(|_| anyhow!("ed25519 signature must be 64 bytes"))?;
        let signature = ed25519_dalek::Signature::from_bytes(&sig_array);

        let message = envelope.pae().context("failed to compute PAE")?;
        public_key
            .verify(&message, &signature)
            .map_err(|e| anyhow!("signature verification failed: {e}"))?;

        let payload_bytes = B64
            .decode(&envelope.payload)
            .context("payload is not valid base64")?;
        let statement: Statement =
            serde_json::from_slice(&payload_bytes).context("payload is not a valid Statement")?;
        Ok((sig_entry.keyid.clone(), statement))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{canonical_json, Artifact, BuildProvenance, DigestSet, SourceCommit};

    fn demo_statement(output_hex: &str) -> Statement {
        let mut digest = [0u8; 32];
        hex::decode_to_slice(output_hex, &mut digest).unwrap();
        Statement {
            statement_type: Statement::STATEMENT_TYPE.to_string(),
            subject: vec![Artifact {
                name: "bin/x".into(),
                digest: DigestSet::new(digest),
            }],
            predicate_type: Statement::PREDICATE_TYPE.to_string(),
            predicate: BuildProvenance {
                builder_id: "b1".into(),
                source_commit: SourceCommit {
                    repository: "https://repo.example.com".into(),
                    ref_commit: "0".repeat(40),
                },
                materials: vec![],
                output: Artifact {
                    name: "bin/x".into(),
                    digest: DigestSet::new(digest),
                },
                built_at: String::new(),
            },
        }
    }

    #[test]
    fn sign_and_verify_roundtrip() {
        let signer = SignerIdentity::from_seed("b1", [7u8; 32]);
        let mut registry = KeyRegistry::new();
        registry.insert("b1", signer.verifying_key());

        let st = demo_statement(&"a".repeat(64));
        let envelope = signer.sign_statement(&st).unwrap();
        let (keyid, parsed) = registry.verify_envelope(&envelope).unwrap();
        assert_eq!(keyid, "b1");
        assert_eq!(parsed, st);
    }

    #[test]
    fn verify_rejects_tampered_payload() {
        let signer = SignerIdentity::from_seed("b1", [7u8; 32]);
        let mut registry = KeyRegistry::new();
        registry.insert("b1", signer.verifying_key());

        let st = demo_statement(&"a".repeat(64));
        let mut envelope = signer.sign_statement(&st).unwrap();
        // 对 base64 payload 做一次字节翻转后重新编码。
        let mut bytes = base64::engine::general_purpose::STANDARD
            .decode(&envelope.payload)
            .unwrap();
        let last = bytes.len() - 2;
        bytes[last] ^= 0x01;
        envelope.payload = base64::engine::general_purpose::STANDARD.encode(bytes);
        assert!(registry.verify_envelope(&envelope).is_err());
    }

    #[test]
    fn verify_rejects_foreign_key() {
        let signer_a = SignerIdentity::from_seed("a", [1u8; 32]);
        let signer_b = SignerIdentity::from_seed("b", [2u8; 32]);
        let mut registry = KeyRegistry::new();
        registry.insert("a", signer_a.verifying_key());

        let st = demo_statement(&"a".repeat(64));
        let envelope = signer_b.sign_statement(&st).unwrap(); // keyid="b" 未注册
        assert!(registry.verify_envelope(&envelope).is_err());

        // 用 a 的注册表验证 b 签的名（伪造 keyid）也必须失败。
        let mut envelope_b = signer_b.sign_statement(&st).unwrap();
        envelope_b.signatures[0].keyid = "a".into();
        assert!(registry.verify_envelope(&envelope_b).is_err());
    }

    #[test]
    fn canonical_json_is_field_order_independent() {
        let st1 = demo_statement(&"a".repeat(64));
        // 反序列化-重排-再序列化不改变规范字节。
        let value = serde_json::to_value(&st1).unwrap();
        let canonical = canonical_json(&value).unwrap();
        let reparsed: serde_json::Value = serde_json::from_slice(&canonical).unwrap();
        assert_eq!(canonical_json(&reparsed).unwrap(), canonical);
    }
}
