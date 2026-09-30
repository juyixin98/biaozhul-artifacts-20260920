//! Process configuration, parsed strictly from the environment with validated
//! ranges. No secrets are involved (local synthetic data only).

use std::env;

use crate::error::{ErrorCode, JoinError, Result};

/// Runtime configuration for the HTTP service.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub host: String,
    pub port: u16,
    /// Maximum rows a client may request in one page.
    pub max_page_limit: u64,
    /// Default page size when the client omits `limit`.
    pub default_page_limit: u64,
    /// Name of the synthetic fixture set loaded at startup.
    pub fixture: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            host: "127.0.0.1".into(),
            port: 8080,
            max_page_limit: 10_000,
            default_page_limit: 100,
            fixture: "triangle".into(),
        }
    }
}

impl Config {
    /// Build from environment variables, applying defaults and validation.
    pub fn from_env() -> Result<Self> {
        let mut cfg = Config::default();
        if let Ok(v) = env::var("LFT_HOST") {
            if v.is_empty() {
                return Err(invalid("LFT_HOST must not be empty"));
            }
            cfg.host = v;
        }
        if let Ok(v) = env::var("LFT_PORT") {
            cfg.port = parse_port(&v)?;
        }
        if let Ok(v) = env::var("LFT_MAX_PAGE_LIMIT") {
            cfg.max_page_limit = parse_positive("LFT_MAX_PAGE_LIMIT", &v)?;
        }
        if let Ok(v) = env::var("LFT_DEFAULT_PAGE_LIMIT") {
            cfg.default_page_limit = parse_positive("LFT_DEFAULT_PAGE_LIMIT", &v)?;
        }
        if let Ok(v) = env::var("LFT_FIXTURE") {
            cfg.fixture = v;
        }
        if cfg.default_page_limit > cfg.max_page_limit {
            return Err(invalid(
                "LFT_DEFAULT_PAGE_LIMIT must not exceed LFT_MAX_PAGE_LIMIT",
            ));
        }
        Ok(cfg)
    }

    /// Clamp a client-requested limit into the valid page range.
    pub fn clamp_limit(&self, requested: Option<u64>) -> u64 {
        requested
            .unwrap_or(self.default_page_limit)
            .min(self.max_page_limit)
            .max(1)
    }
}

fn parse_port(raw: &str) -> Result<u16> {
    raw.parse::<u16>()
        .map_err(|_| invalid(&format!("LFT_PORT `{raw}` is not a valid port (1-65535)")))
}

fn parse_positive(name: &str, raw: &str) -> Result<u64> {
    match raw.parse::<u64>() {
        Ok(v) if v >= 1 => Ok(v),
        _ => Err(invalid(&format!("{name} `{raw}` must be a positive integer"))),
    }
}

fn invalid(msg: impl Into<String>) -> JoinError {
    JoinError::new(ErrorCode::InvalidLimit, msg)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_valid() {
        let cfg = Config::default();
        assert!(cfg.default_page_limit <= cfg.max_page_limit);
        assert_eq!(cfg.clamp_limit(None), cfg.default_page_limit);
        assert_eq!(cfg.clamp_limit(Some(u64::MAX)), cfg.max_page_limit);
        assert_eq!(cfg.clamp_limit(Some(0)), 1);
    }

    #[test]
    fn parses_port() {
        assert!(matches!(parse_port("8080"), Ok(8080)));
        assert!(parse_port("70000").is_err());
        assert!(parse_port("abc").is_err());
    }
}
