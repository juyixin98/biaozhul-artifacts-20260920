//! 本地夹具加载：策略配置、构建器公钥注册表、材料库。
//!
//! 全部身份与材料均来自仓库内 `fixtures/` 目录，不访问任何外部服务。

use crate::crypto::KeyRegistry;
use crate::policy::{MaterialStore, Policy, Verifier};
use anyhow::{Context, Result};
use ed25519_dalek::VerifyingKey;
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

#[derive(Debug, Deserialize)]
struct ConfigFile {
    trusted_builders: Vec<String>,
    allowed_material_prefixes: Vec<String>,
    allowed_source_repositories: Vec<String>,
    /// keyid（构建器 id）-> 32 字节 Ed25519 公钥（hex）。
    keys: HashMap<String, String>,
}

/// 从夹具目录组装验证器（含策略、公钥注册表、本地材料库）。
pub fn load_fixtures(fixtures_dir: PathBuf) -> Result<Verifier> {
    let config_path = fixtures_dir.join("config.json");
    let config: ConfigFile = serde_json::from_slice(
        &std::fs::read(&config_path)
            .with_context(|| format!("read {}", config_path.display()))?,
    )
    .context("parse fixtures/config.json")?;

    let mut keys = KeyRegistry::new();
    for (keyid, pub_hex) in &config.keys {
        let raw = hex::decode(pub_hex).context("public key must be hex")?;
        let arr: [u8; 32] = raw
            .as_slice()
            .try_into()
            .map_err(|_| anyhow::anyhow!("public key for `{keyid}` must be 32 bytes"))?;
        let vk = VerifyingKey::from_bytes(&arr)
            .map_err(|e| anyhow::anyhow!("invalid public key for `{keyid}`: {e}"))?;
        keys.insert(keyid.clone(), vk);
    }

    let policy = Policy {
        trusted_builders: config.trusted_builders.into_iter().collect::<HashSet<_>>(),
        allowed_material_prefixes: config.allowed_material_prefixes,
        allowed_source_repositories: config
            .allowed_source_repositories
            .into_iter()
            .collect::<HashSet<_>>(),
    };

    let materials = MaterialStore::load_from_dir(&fixtures_dir.join("materials"))?;

    Ok(Verifier {
        keys,
        policy,
        materials,
        fixtures_dir,
    })
}
