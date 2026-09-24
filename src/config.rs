use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::Path;

/// 速度限幅（对所有来源生效；E-Stop 永远输出精确零速）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Limits {
    pub vx: f64,
    pub vy: f64,
    pub omega: f64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            vx: 1.5,
            vy: 1.0,
            omega: 2.0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SourceKind {
    Autonomous,
    Rc,
    Estop,
}

impl SourceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            SourceKind::Autonomous => "autonomous",
            SourceKind::Rc => "rc",
            SourceKind::Estop => "estop",
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct SourceConfig {
    pub kind: SourceKind,
    pub key_hex: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    /// issue_ms 允许比服务器接收时刻早多少（防过期/重放窗口），毫秒。
    #[serde(default = "default_max_future_ms")]
    pub max_future_ms: i64,
    /// 租约上限，毫秒。lease_for_ms 超过它会被拒绝。
    #[serde(default = "default_max_lease_ms")]
    pub max_lease_ms: i64,
    #[serde(default)]
    pub limits: Limits,
    pub sources: HashMap<String, SourceConfig>,
}

fn default_max_future_ms() -> i64 {
    5_000
}
fn default_max_lease_ms() -> i64 {
    5_000
}

impl Config {
    pub fn load(path: &Path) -> Result<Config, String> {
        let text = fs::read_to_string(path).map_err(|e| {
            format!(
                "无法读取配置 {}: {e}（可参考 examples/config.example.json，或用 `keygen` 生成密钥）",
                path.display()
            )
        })?;
        let cfg: Config = serde_json::from_str(&text).map_err(|e| format!("配置解析失败: {e}"))?;
        for (id, src) in &cfg.sources {
            let bytes =
                hex::decode(&src.key_hex).map_err(|e| format!("来源 {id} 的 key_hex 非法: {e}"))?;
            if bytes.len() < 32 {
                return Err(format!(
                    "来源 {id} 的密钥至少需要 32 字节（64 个十六进制字符），当前 {} 字节",
                    bytes.len()
                ));
            }
        }
        if cfg.max_lease_ms <= 0 || cfg.max_future_ms < 0 {
            return Err("max_lease_ms 必须为正、max_future_ms 不能为负".into());
        }
        Ok(cfg)
    }

    pub fn source(&self, id: &str) -> Option<&SourceConfig> {
        self.sources.get(id)
    }

    pub fn key_bytes(&self, id: &str) -> Option<Vec<u8>> {
        self.sources
            .get(id)
            .and_then(|s| hex::decode(&s.key_hex).ok())
    }
}
