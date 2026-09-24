//! 验证策略：可信构建器注册表、允许的源码仓库与材料来源。
//!
//! 策略文件为本地 JSON 夹具（`fixtures/policy.json`），由 [`crate::fixture`]
//! 生成与加载；生产环境中可替换为集中式策略源，验证逻辑不变。

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// 策略文件根结构。
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Policy {
    /// 可信构建器：builder_id -> 注册信息（公钥等）。
    pub trusted_builders: BTreeMap<String, TrustedBuilder>,
    /// 允许的源码仓库（精确匹配 source_commit.repo）。
    pub allowed_source_repos: Vec<String>,
    /// 允许的材料来源 URI 前缀（按前缀匹配，必须以 `/` 结尾）。
    pub allowed_material_origins: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TrustedBuilder {
    /// 注册的签名公钥（hex）。信封 key_id 必须与之完全一致。
    pub public_key_hex: String,
    pub display_name: String,
}

impl Policy {
    /// 加载后自检：来源前缀必须以 `/` 结尾，保证前缀匹配不会越过目录边界。
    pub fn validate(&self) -> Result<(), String> {
        for origin in &self.allowed_material_origins {
            if !origin.ends_with('/') {
                return Err(format!(
                    "allowed_material_origins 条目必须以 '/' 结尾: {origin}"
                ));
            }
        }
        Ok(())
    }

    pub fn is_trusted_builder(&self, builder_id: &str) -> bool {
        self.trusted_builders.contains_key(builder_id)
    }

    pub fn is_source_repo_allowed(&self, repo: &str) -> bool {
        self.allowed_source_repos.iter().any(|r| r == repo)
    }

    pub fn is_origin_allowed(&self, uri: &str) -> bool {
        self.allowed_material_origins
            .iter()
            .any(|prefix| uri.starts_with(prefix.as_str()))
    }
}
