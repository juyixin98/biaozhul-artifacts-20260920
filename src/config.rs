//! Configuration layer: TOML file + `BITINDEX3VL_*` environment overrides.
//!
//! Precedence: built-in defaults < config file < environment variables.
//! Every parse/validation failure is [`ErrorKind::InvalidConfig`] with the
//! offending key named — a bad config never degrades into a silently
//! default-running server.

use std::path::Path;

use serde::Deserialize;

use crate::error::{ErrorKind, IndexError, Result};

pub const ENV_PREFIX: &str = "BITINDEX3VL_";

/// Runtime configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Bind address for the HTTP validation entry.
    pub host: String,
    /// Bind port (`0` = ask the OS for an ephemeral port; used by tests).
    pub port: u16,
    /// `tracing_subscriber` EnvFilter directive, e.g. `info` or `bitindex3vl=debug`.
    pub log_filter: String,
    /// Hard cap on rows per table (resource guard for the validation entry).
    pub max_rows_per_table: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_string(),
            port: 3030,
            log_filter: "info".to_string(),
            max_rows_per_table: 1_000_000,
        }
    }
}

/// Raw TOML shape: every field optional so "absent" and "present but wrong"
/// are distinguishable.
#[derive(Debug, Deserialize)]
struct FileConfig {
    host: Option<String>,
    port: Option<u16>,
    log_filter: Option<String>,
    max_rows_per_table: Option<usize>,
}

impl Config {
    /// Load configuration: defaults, then the TOML file if `path` is given,
    /// then environment overrides.
    pub fn load(path: Option<&Path>) -> Result<Self> {
        let mut cfg = Config::default();
        if let Some(path) = path {
            let text = std::fs::read_to_string(path).map_err(|e| {
                IndexError::new(
                    ErrorKind::InvalidConfig,
                    format!("cannot read config file {}: {e}", path.display()),
                )
            })?;
            cfg = cfg.merge_toml_str(&text)?;
        }
        cfg.apply_env()?;
        cfg.validate()?;
        Ok(cfg)
    }

    /// Parse and merge a TOML document (used by `load` and by tests).
    pub fn merge_toml_str(self, text: &str) -> Result<Self> {
        let file: FileConfig = toml::from_str(text).map_err(|e| {
            IndexError::new(ErrorKind::InvalidConfig, format!("invalid TOML configuration: {e}"))
        })?;
        let mut cfg = self;
        if let Some(host) = file.host {
            cfg.host = host;
        }
        if let Some(port) = file.port {
            cfg.port = port;
        }
        if let Some(log_filter) = file.log_filter {
            cfg.log_filter = log_filter;
        }
        if let Some(max_rows_per_table) = file.max_rows_per_table {
            cfg.max_rows_per_table = max_rows_per_table;
        }
        cfg.validate()?;
        Ok(cfg)
    }

    fn apply_env(&mut self) -> Result<()> {
        let env = |key: &str| std::env::var(format!("{ENV_PREFIX}{key}")).ok();
        if let Some(v) = env("HOST") {
            self.host = v;
        }
        if let Some(v) = env("PORT") {
            self.port = v.parse::<u16>().map_err(|_| {
                IndexError::new(
                    ErrorKind::InvalidConfig,
                    format!("{ENV_PREFIX}PORT={v:?} is not a valid port (0-65535)"),
                )
            })?;
        }
        if let Some(v) = env("LOG_FILTER") {
            self.log_filter = v;
        }
        if let Some(v) = env("MAX_ROWS_PER_TABLE") {
            self.max_rows_per_table = v.parse::<usize>().map_err(|_| {
                IndexError::new(
                    ErrorKind::InvalidConfig,
                    format!("{ENV_PREFIX}MAX_ROWS_PER_TABLE={v:?} is not a non-negative integer"),
                )
            })?;
        }
        Ok(())
    }

    fn validate(&self) -> Result<()> {
        if self.host.trim().is_empty() {
            return Err(IndexError::new(ErrorKind::InvalidConfig, "host must not be empty"));
        }
        if self.log_filter.trim().is_empty() {
            return Err(IndexError::new(ErrorKind::InvalidConfig, "log_filter must not be empty"));
        }
        if self.max_rows_per_table == 0 {
            return Err(IndexError::new(
                ErrorKind::InvalidConfig,
                "max_rows_per_table must be at least 1",
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_sane() {
        let c = Config::default();
        assert_eq!(c.port, 3030);
        assert!(c.validate().is_ok());
    }

    #[test]
    fn toml_merge_and_type_errors() {
        let c = Config::default().merge_toml_str("port = 9999\nmax_rows_per_table = 42\n").unwrap();
        assert_eq!(c.port, 9999);
        assert_eq!(c.max_rows_per_table, 42);
        assert_eq!(c.host, "127.0.0.1", "untouched fields keep defaults");

        let err = Config::default().merge_toml_str("port = \"not-a-port\"").unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidConfig);
        let err = Config::default().merge_toml_str("max_rows_per_table = 0").unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidConfig);
        let err = Config::default().merge_toml_str("host = \"\"").unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidConfig);
        let err = Config::default().merge_toml_str("not toml at all = =").unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidConfig);
    }

    #[test]
    fn missing_file_is_typed_error() {
        let err = Config::load(Some(Path::new("/nonexistent/bitindex3vl.toml"))).unwrap_err();
        assert_eq!(err.kind, ErrorKind::InvalidConfig);
    }
}
