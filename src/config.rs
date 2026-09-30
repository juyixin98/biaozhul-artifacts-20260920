//! Process configuration: `config/default.toml` overlaid with `DECORR_*`
//! environment variables. Locked, local, no secrets.
//!
//! The config file uses a deliberately tiny flat `key = "value"` /
//! `key = number` subset (comments with `#`), parsed here without a TOML
//! dependency — the schema is fixed and shallow.

use std::collections::HashMap;
use std::path::PathBuf;

use crate::error::{EngineError, EngineResult, ErrorKind};

#[derive(Debug, Clone)]
pub struct Config {
    pub host: String,
    pub port: u16,
    pub fixtures_dir: PathBuf,
    pub max_rows_per_table: usize,
    pub max_outer_rows_per_query: usize,
    pub log_level: String,
}

impl Config {
    pub fn load() -> EngineResult<Self> {
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        let cfg_path = std::env::var("DECORR_CONFIG")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                let local = PathBuf::from("config/default.toml");
                if local.exists() {
                    local
                } else {
                    PathBuf::from(manifest_dir).join("config/default.toml")
                }
            });

        let text = std::fs::read_to_string(&cfg_path).map_err(|e| {
            EngineError::new(
                ErrorKind::Internal,
                format!("cannot read config {}: {e}", cfg_path.display()),
            )
        })?;
        let map = parse_flat_toml(&text)?;

        let str_or = |key: &str, default: &str| {
            map.get(key).cloned().unwrap_or_else(|| default.to_string())
        };
        let int_or = |key: &str, default: i64| -> EngineResult<i64> {
            match map.get(key) {
                None => Ok(default),
                Some(v) => v.trim().parse::<i64>().map_err(|_| {
                    EngineError::validation(format!("config '{key}' must be an integer, got {v:?}"))
                }),
            }
        };

        let mut cfg = Config {
            host: str_or("host", "127.0.0.1"),
            port: int_or("port", 8080)? as u16,
            fixtures_dir: PathBuf::from(str_or("fixtures_dir", "fixtures")),
            max_rows_per_table: int_or("max_rows_per_table", 50_000)? as usize,
            max_outer_rows_per_query: int_or("max_outer_rows_per_query", 100_000)? as usize,
            log_level: str_or("log_level", "info,decorr_service=debug"),
        };

        if let Ok(v) = std::env::var("DECORR_HOST") {
            cfg.host = v;
        }
        if let Ok(v) = std::env::var("DECORR_PORT") {
            cfg.port = v
                .parse()
                .map_err(|_| EngineError::validation("DECORR_PORT must be a u16"))?;
        }
        if let Ok(v) = std::env::var("DECORR_FIXTURES_DIR") {
            cfg.fixtures_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("DECORR_LOG_LEVEL") {
            cfg.log_level = v;
        }

        if !cfg.fixtures_dir.is_absolute() {
            cfg.fixtures_dir = PathBuf::from(manifest_dir).join(cfg.fixtures_dir);
        }
        Ok(cfg)
    }

    pub fn bind_addr(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }
}

/// Parse the flat config subset: `key = value`, one per line, `#` comments.
/// Values are stored raw; strings keep their quotes stripped.
fn parse_flat_toml(text: &str) -> EngineResult<HashMap<String, String>> {
    let mut map = HashMap::new();
    for (lineno, raw_line) in text.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, value) = line.split_once('=').ok_or_else(|| {
            EngineError::validation(format!(
                "config line {} is not key = value: {raw_line:?}",
                lineno + 1
            ))
        })?;
        let key = key.trim();
        let mut value = value.trim();
        // Strip an inline comment only when value is not a quoted string.
        if !value.starts_with('"') && !value.starts_with('\'') {
            if let Some(hash) = value.find('#') {
                value = value[..hash].trim();
            }
        }
        if (value.starts_with('"') && value.ends_with('"') && value.len() >= 2)
            || (value.starts_with('\'') && value.ends_with('\'') && value.len() >= 2)
        {
            value = &value[1..value.len() - 1];
        }
        if !key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
        {
            return Err(EngineError::validation(format!(
                "invalid config key {key:?} on line {}",
                lineno + 1
            )));
        }
        map.insert(key.to_string(), value.to_string());
    }
    Ok(map)
}
