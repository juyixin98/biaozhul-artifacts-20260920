//! Server configuration, parsed once at startup.
//!
//! Every knob has an environment variable and a `--flag=value` CLI override
//! (CLI wins over env, env wins over the default). Configuration is plain
//! data: it is logged in redacted diagnostics and exposed through
//! `GET /config` so a verification run can prove which limits applied.

use serde::Serialize;

use crate::error::{Error, ReasonCode};

/// All tunables of the backend.
#[derive(Debug, Clone, Serialize)]
pub struct ServerConfig {
    /// HTTP bind address.
    pub bind: String,
    /// Maximum accepted request body size in bytes.
    pub max_body_bytes: usize,
    /// Maximum number of distinct join variables per query.
    pub max_variables: usize,
    /// Whether column-permuted views may be materialized at plan time.
    pub allow_reindex: bool,
    /// Maximum distinct answer rows a single `/query` slice may return.
    pub max_row_limit: usize,
    /// Default slice row limit when the caller omits one.
    pub default_row_limit: usize,
    /// Default primitive trie-access budget per slice.
    pub default_access_budget: Option<u64>,
    /// Safety cap on naive-oracle materialized partial assignments.
    pub oracle_intermediate_cap: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            bind: "127.0.0.1:8080".to_string(),
            max_body_bytes: 4 * 1024 * 1024,
            max_variables: 16,
            allow_reindex: true,
            max_row_limit: 10_000,
            default_row_limit: 1000,
            default_access_budget: Some(2_000_000),
            oracle_intermediate_cap: 200_000,
        }
    }
}

impl ServerConfig {
    /// Build defaults, apply `LTJ_*` environment variables, then CLI
    /// overrides of the form `--name=value` / `--name value`.
    pub fn from_env_and_args(argv: &[String]) -> Result<Self, Error> {
        let mut cfg = Self::default();

        if let Ok(v) = std::env::var("LTJ_BIND") {
            cfg.bind = v;
        }
        if let Some(v) = env_usize("LTJ_MAX_BODY_BYTES")? {
            cfg.max_body_bytes = v;
        }
        if let Some(v) = env_usize("LTJ_MAX_VARIABLES")? {
            cfg.max_variables = v;
        }
        if let Some(v) = std::env::var("LTJ_ALLOW_REINDEX").ok() {
            cfg.allow_reindex = parse_bool(&v)?;
        }
        if let Some(v) = env_usize("LTJ_MAX_ROW_LIMIT")? {
            cfg.max_row_limit = v;
        }
        if let Some(v) = env_usize("LTJ_DEFAULT_ROW_LIMIT")? {
            cfg.default_row_limit = v;
        }
        if let Ok(v) = std::env::var("LTJ_DEFAULT_ACCESS_BUDGET") {
            cfg.default_access_budget = Some(parse_u64(&v)?);
        }
        if let Some(v) = env_usize("LTJ_ORACLE_INTERMEDIATE_CAP")? {
            cfg.oracle_intermediate_cap = v;
        }

        let mut i = 0;
        while i < argv.len() {
            let arg = &argv[i];
            let (key, inline) = match arg.strip_prefix("--") {
                Some(rest) => match rest.split_once('=') {
                    Some((k, v)) => (k.to_string(), Some(v.to_string())),
                    None => (rest.to_string(), None),
                },
                None => {
                    return Err(Error::validation(
                        ReasonCode::MalformedJson,
                        format!("unexpected positional argument: {arg}"),
                    ))
                }
            };
            let take = |inline: Option<String>, i: &mut usize| -> Result<String, Error> {
                if let Some(v) = inline {
                    return Ok(v);
                }
                *i += 1;
                argv.get(*i).cloned().ok_or_else(|| {
                    Error::validation(
                        ReasonCode::MalformedJson,
                        format!("flag --{key} requires a value"),
                    )
                })
            };
            macro_rules! num {
                ($field:ident, $parse:expr) => {{
                    let v = take(inline, &mut i)?;
                    cfg.$field = $parse(&v)?;
                }};
            }
            match key.as_str() {
                "bind" => cfg.bind = take(inline, &mut i)?,
                "max-body-bytes" => num!(max_body_bytes, parse_usize),
                "max-variables" => num!(max_variables, parse_usize),
                "allow-reindex" => {
                    cfg.allow_reindex = parse_bool(&take(inline, &mut i)?)?
                }
                "max-row-limit" => num!(max_row_limit, parse_usize),
                "default-row-limit" => num!(default_row_limit, parse_usize),
                "default-access-budget" => {
                    cfg.default_access_budget = Some(parse_u64(&take(inline, &mut i)?)?)
                }
                "oracle-intermediate-cap" => num!(oracle_intermediate_cap, parse_usize),
                other => {
                    return Err(Error::validation(
                        ReasonCode::MalformedJson,
                        format!("unknown configuration flag: --{other}"),
                    ))
                }
            }
            i += 1;
        }

        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<(), Error> {
        if self.max_variables == 0
            || self.max_row_limit == 0
            || self.default_row_limit == 0
            || self.oracle_intermediate_cap == 0
            || self.max_body_bytes == 0
        {
            return Err(Error::validation(
                ReasonCode::InvalidLimit,
                "all size/variable limits must be at least 1",
            ));
        }
        if self.default_row_limit > self.max_row_limit {
            return Err(Error::validation(
                ReasonCode::InvalidLimit,
                "default_row_limit must not exceed max_row_limit",
            ));
        }
        Ok(())
    }
}

fn env_usize(name: &str) -> Result<Option<usize>, Error> {
    match std::env::var(name) {
        Ok(v) => Ok(Some(parse_usize(&v)?)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err(Error::validation(
            ReasonCode::InvalidLimit,
            format!("{name} is not valid UTF-8"),
        )),
    }
}

fn parse_usize(v: &str) -> Result<usize, Error> {
    v.trim()
        .parse::<usize>()
        .map_err(|_| Error::validation(ReasonCode::InvalidLimit, format!("not a positive integer: {v}")))
}

fn parse_u64(v: &str) -> Result<u64, Error> {
    v.trim()
        .parse::<u64>()
        .map_err(|_| Error::validation(ReasonCode::InvalidLimit, format!("not a non-negative integer: {v}")))
}

fn parse_bool(v: &str) -> Result<bool, Error> {
    match v.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        other => Err(Error::validation(
            ReasonCode::InvalidLimit,
            format!("expected a boolean, got: {other}"),
        )),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::needless_update)]

    use super::*;

    #[test]
    fn cli_overrides_defaults_and_rejects_bad_values() {
        let cfg = ServerConfig::from_env_and_args(&[
            "--bind".to_string(),
            "0.0.0.0:9999".to_string(),
            "--max-variables=3".to_string(),
            "--allow-reindex=false".to_string(),
        ])
        .unwrap();
        assert_eq!(cfg.bind, "0.0.0.0:9999");
        assert_eq!(cfg.max_variables, 3);
        assert!(!cfg.allow_reindex);

        let err = ServerConfig::from_env_and_args(&["--max-variables=0".to_string()]).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::InvalidLimit);

        let err = ServerConfig::from_env_and_args(&["--nope".to_string()]).unwrap_err();
        assert_eq!(err.reason().unwrap().code, ReasonCode::MalformedJson);
    }
}
