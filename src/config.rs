//! Configuration layer: values come from an optional TOML file and can be
//! overridden by environment variables (prefix `TRITMAP_`). Configuration is
//! fully validated at startup — a bad value aborts with a concrete
//! [`TritmapError::Config`] rather than silently falling back.

use serde::Deserialize;

use crate::error::{Result, TritmapError};

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    pub server: ServerConfig,
    pub data: DataConfig,
    pub verify: VerifyConfig,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DataConfig {
    /// Directory containing synthetic CSV fixtures.
    pub fixture_dir: String,
    /// CSV file (inside fixture_dir) loaded at startup.
    pub table_file: String,
    /// Snapshot version assigned to loaded data.
    pub data_version: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct VerifyConfig {
    /// Base seed for randomized differential checks.
    pub seed: u64,
    /// Number of randomized cases per verification run.
    pub iterations: usize,
    /// Rows per randomized case.
    pub rows: usize,
    /// NULL percentage 0..=100.
    pub null_density: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            server: ServerConfig {
                host: "127.0.0.1".into(),
                port: 8080,
            },
            data: DataConfig {
                fixture_dir: "fixtures".into(),
                table_file: "users.csv".into(),
                data_version: 1,
            },
            verify: VerifyConfig {
                seed: 0x5EED_1234,
                iterations: 24,
                rows: 200,
                null_density: 40,
            },
        }
    }
}

impl Config {
    /// Load defaults, overlay a TOML file if present, then env overrides.
    pub fn load(path: Option<&str>) -> Result<Self> {
        let mut cfg = Config::default();
        if let Some(p) = path {
            let text = std::fs::read_to_string(p)?;
            cfg = toml::from_str(&text)
                .map_err(|e| TritmapError::Config(format!("parse {p}: {e}")))?;
        }
        cfg.apply_env()?;
        cfg.validate()?;
        Ok(cfg)
    }

    fn apply_env(&mut self) -> Result<()> {
        if let Ok(v) = std::env::var("TRITMAP_HOST") {
            cfg_set(&v, |x| self.server.host = x)?;
        }
        if let Ok(v) = std::env::var("TRITMAP_PORT") {
            self.server.port = parse_env("TRITMAP_PORT", &v)?;
        }
        if let Ok(v) = std::env::var("TRITMAP_FIXTURE_DIR") {
            self.data.fixture_dir = v;
        }
        if let Ok(v) = std::env::var("TRITMAP_TABLE_FILE") {
            self.data.table_file = v;
        }
        if let Ok(v) = std::env::var("TRITMAP_DATA_VERSION") {
            self.data.data_version = parse_env("TRITMAP_DATA_VERSION", &v)?;
        }
        if let Ok(v) = std::env::var("TRITMAP_VERIFY_SEED") {
            self.verify.seed = parse_env("TRITMAP_VERIFY_SEED", &v)?;
        }
        if let Ok(v) = std::env::var("TRITMAP_VERIFY_ITERATIONS") {
            self.verify.iterations = parse_env("TRITMAP_VERIFY_ITERATIONS", &v)?;
        }
        Ok(())
    }

    fn validate(&self) -> Result<()> {
        if self.server.port == 0 {
            return Err(TritmapError::Config("server.port must be non-zero".into()));
        }
        if self.data.data_version == 0 {
            return Err(TritmapError::Config("data.data_version must be >= 1".into()));
        }
        if self.verify.null_density > 100 {
            return Err(TritmapError::Config(
                "verify.null_density must be in 0..=100".into(),
            ));
        }
        if self.verify.iterations == 0 {
            return Err(TritmapError::Config("verify.iterations must be >= 1".into()));
        }
        Ok(())
    }
}

fn cfg_set(v: &str, f: impl FnOnce(String)) -> Result<()> {
    if v.trim().is_empty() {
        return Err(TritmapError::Config("empty string env override".into()));
    }
    f(v.to_string());
    Ok(())
}

fn parse_env<T>(name: &str, v: &str) -> Result<T>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    v.parse::<T>().map_err(|e| {
        TritmapError::Config(format!("env {name}='{v}' is invalid: {e}"))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_validate_and_reject_bad_values() {
        Config::default().validate().unwrap();
        let mut bad = Config::default();
        bad.verify.null_density = 101;
        assert!(matches!(bad.validate(), Err(TritmapError::Config(_))));
        let mut bad = Config::default();
        bad.data.data_version = 0;
        assert!(matches!(bad.validate(), Err(TritmapError::Config(_))));
    }

    #[test]
    fn toml_overlay_parses() {
        let toml = r#"
[server]
host = "0.0.0.0"
port = 9999
[data]
fixture_dir = "fixtures"
table_file = "users.csv"
data_version = 7
[verify]
seed = 1
iterations = 3
rows = 67
null_density = 100
"#;
        let p = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(p.path(), toml).unwrap();
        let cfg = Config::load(Some(p.path().to_str().unwrap())).unwrap();
        assert_eq!(cfg.server.port, 9999);
        assert_eq!(cfg.data.data_version, 7);
        assert_eq!(cfg.verify.rows, 67);
        assert_eq!(cfg.verify.null_density, 100);
    }
}
