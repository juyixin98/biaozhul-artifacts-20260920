//! Process configuration.
//!
//! Values come from, in priority order:
//! 1. explicit environment variables (`DECORRELATE_*`),
//! 2. `config/default.json` shipped with the service,
//! 3. built-in defaults.
//!
//! Configuration is local-only: no production accounts, credentials or
//! external services are referenced.

use serde::Deserialize;
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize)]
pub struct ServerConfig {
    /// Bind address (host:port) for the Axum server.
    pub bind: String,
    /// Directory of synthetic fixture relations (`*.json`).
    pub fixtures_dir: PathBuf,
    /// Log filter, e.g. `info`, `decorrelate=debug`.
    pub log_filter: String,
    /// Emit logs as newline-delimited JSON.
    pub log_json: bool,
}

impl Default for ServerConfig {
    fn default() -> Self {
        ServerConfig {
            bind: "127.0.0.1:8080".to_string(),
            fixtures_dir: PathBuf::from("fixtures/tables"),
            log_filter: "info".to_string(),
            log_json: true,
        }
    }
}

impl ServerConfig {
    /// Load configuration: file first (when present), then environment
    /// overrides. A missing config file is fine (defaults apply); a malformed
    /// file is an error.
    pub fn load(path: Option<&str>) -> Result<Self, ConfigError> {
        let mut cfg = match path {
            Some(p) => {
                let text = std::fs::read_to_string(p).map_err(|source| ConfigError::Read {
                    path: p.to_string(),
                    source,
                })?;
                serde_json::from_str(&text).map_err(|source| ConfigError::Parse {
                    path: p.to_string(),
                    source,
                })?
            }
            None => ServerConfig::default(),
        };

        if let Ok(v) = std::env::var("DECORRELATE_BIND") {
            cfg.bind = v;
        }
        if let Ok(v) = std::env::var("DECORRELATE_FIXTURES_DIR") {
            cfg.fixtures_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("DECORRELATE_LOG_FILTER") {
            cfg.log_filter = v;
        }
        if let Ok(v) = std::env::var("DECORRELATE_LOG_JSON") {
            cfg.log_json = matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes");
        }
        Ok(cfg)
    }
}

#[derive(Debug)]
pub enum ConfigError {
    Read { path: String, source: std::io::Error },
    Parse { path: String, source: serde_json::Error },
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Read { path, source } => {
                write!(f, "cannot read config {}: {}", path, source)
            }
            ConfigError::Parse { path, source } => {
                write!(f, "invalid JSON in config {}: {}", path, source)
            }
        }
    }
}

impl std::error::Error for ConfigError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_local_only() {
        let cfg = ServerConfig::default();
        assert!(cfg.bind.starts_with("127.0.0.1"));
        assert!(cfg.fixtures_dir.starts_with("fixtures"));
    }

    #[test]
    fn parses_shipped_default_config() {
        let cfg = ServerConfig::load(Some("config/default.json")).expect("shipped config parses");
        assert!(!cfg.bind.is_empty());
        assert!(cfg.fixtures_dir.exists() || cfg.fixtures_dir.to_string_lossy().contains("fixtures"));
    }
}
