//! 确定性本地夹具：构建器身份（Ed25519 密钥）、策略、材料与示例请求。
//!
//! 密钥由固定标签经 SHA-256 派生 32 字节种子得到 —— 不调用系统随机源，
//! 因此夹具在任何机器上都字节一致。私钥仅用于本地演示（gen-fixtures 工具、
//! `/v1/fixture-sign` 辅助端点与测试），生产环境绝不应随服务分发。

pub mod data;
pub mod examples;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use ed25519_dalek::{SigningKey, VerifyingKey};
use sha2::Digest as _;

use crate::crypto::sign_statement;
use crate::model::{Envelope, Statement};
use crate::policy::{Policy, TrustedBuilder};

/// 一组夹具的内存表示。
#[derive(Clone)]
pub struct FixtureBundle {
    pub policy: Policy,
    /// builder_id -> 签名种子 hex。
    pub secrets: BTreeMap<String, String>,
    pub dir: PathBuf,
}

/// 由固定标签派生签名种子（确定性）。
fn derived_seed(label: &str) -> [u8; 32] {
    let mut h = sha2::Sha256::new_with_prefix(b"provenance-fixture-key:");
    h.update(label.as_bytes());
    h.finalize().into()
}

/// 构造一个构建器身份，返回 (公钥hex, 私钥种子hex)。
pub fn derive_identity(label: &str) -> (String, String) {
    let seed = derived_seed(label);
    let sk = SigningKey::from_bytes(&seed);
    let vk: VerifyingKey = sk.verifying_key();
    (hex::encode(vk.to_bytes()), hex::encode(seed))
}

/// 在内存中生成标准夹具集合：一个可信构建器 + 一个 rogue 构建器（仅私钥，
/// 不在策略注册表中）。
pub fn generate() -> FixtureBundle {
    let (a_pub, a_priv) = derive_identity(data::TRUSTED_BUILDER_A);
    let (_r_pub, r_priv) = derive_identity(data::ROGUE_BUILDER_R);

    let mut trusted = BTreeMap::new();
    trusted.insert(
        data::TRUSTED_BUILDER_A.to_string(),
        TrustedBuilder {
            public_key_hex: a_pub,
            display_name: "示例可信构建器 A（CI）".to_string(),
        },
    );

    let policy = Policy {
        trusted_builders: trusted,
        allowed_source_repos: vec![data::ALLOWED_REPO.to_string()],
        allowed_material_origins: vec![
            data::ALLOWED_ORIGIN_DEPS.to_string(),
            data::ALLOWED_ORIGIN_VENDOR.to_string(),
        ],
    };

    let mut secrets = BTreeMap::new();
    secrets.insert(data::TRUSTED_BUILDER_A.to_string(), a_priv);
    secrets.insert(data::ROGUE_BUILDER_R.to_string(), r_priv);

    FixtureBundle {
        policy,
        secrets,
        dir: PathBuf::new(),
    }
}

/// 用指定构建器的私钥对证明签名并封装成信封。
pub fn seal(bundle: &FixtureBundle, builder_id: &str, st: Statement) -> Envelope {
    let vk_hex = bundle
        .policy
        .trusted_builders
        .get(builder_id)
        .map(|b| b.public_key_hex.clone())
        .unwrap_or_else(|| {
            // rogue 构建器不在策略表中，从其标签直接推导公钥。
            derive_identity(builder_id).0
        });
    let sk_hex = bundle
        .secrets
        .get(builder_id)
        .unwrap_or_else(|| panic!("夹具中缺少构建器 {builder_id} 的私钥"));
    let signature_hex =
        sign_statement(sk_hex, &st).expect("fixture signing with deterministic key cannot fail");
    Envelope {
        payload: st,
        key_id: vk_hex,
        signature_hex,
    }
}

/// 将夹具（policy、私钥、示例请求）写入目录。
pub fn persist(bundle: &FixtureBundle, dir: &Path) -> Result<(), Box<dyn std::error::Error>> {
    fs::create_dir_all(dir.join("builders"))?;

    let policy_path = dir.join("policy.json");
    fs::write(&policy_path, serde_json::to_vec_pretty(&bundle.policy)?)?;

    for (builder_id, sk_hex) in &bundle.secrets {
        fs::write(
            dir.join("builders")
                .join(format!("{builder_id}.secret.hex")),
            sk_hex,
        )?;
    }

    let examples_dir = dir.join("examples");
    fs::create_dir_all(&examples_dir)?;
    for (name, req) in examples::all(bundle) {
        fs::write(
            examples_dir.join(format!("{name}.json")),
            serde_json::to_vec_pretty(&req)?,
        )?;
    }
    Ok(())
}

/// 从目录加载夹具；目录为空或关键文件缺失时自动生成（演示友好）。
pub fn load_or_create(dir: &Path) -> Result<FixtureBundle, Box<dyn std::error::Error>> {
    let policy_path = dir.join("policy.json");
    let builders_dir = dir.join("builders");

    if !policy_path.exists() || !builders_dir.exists() {
        let bundle = FixtureBundle {
            dir: dir.to_path_buf(),
            ..generate()
        };
        persist(&bundle, dir)?;
        return Ok(bundle);
    }

    let policy: Policy = serde_json::from_slice(&fs::read(&policy_path)?)?;
    policy.validate()?;

    let mut secrets = BTreeMap::new();
    for entry in fs::read_dir(&builders_dir)? {
        let entry = entry?;
        let path = entry.path();
        let fname = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or_default();
        let Some(builder_id) = fname.strip_suffix(".secret.hex").map(str::to_string) else {
            continue;
        };
        let sk_hex = fs::read_to_string(&path)?.trim().to_string();
        secrets.insert(builder_id, sk_hex);
    }

    // 可信构建器的私钥必须齐备（rogue 构建器的私钥可有可无）。
    for builder_id in policy.trusted_builders.keys() {
        if !secrets.contains_key(builder_id) {
            let bundle = FixtureBundle {
                dir: dir.to_path_buf(),
                ..generate()
            };
            persist(&bundle, dir)?;
            return Ok(bundle);
        }
    }

    Ok(FixtureBundle {
        policy,
        secrets,
        dir: dir.to_path_buf(),
    })
}
