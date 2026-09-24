//! Platform conditions for dependencies.
//!
//! A target is a small boolean expression over the active platform's
//! `os` / `arch` / `family` attributes:
//!
//! ```json
//! { "os": "linux" }
//! { "arch": "aarch64" }
//! { "all": [ {"os": "linux"}, {"arch": "x86_64"} ] }
//! { "any": [ {"os": "linux"}, {"os": "macos"} ] }
//! { "not": {"os": "windows"} }
//! ```
//!
//! A dependency with `target: null` (or omitted) is unconditional.

use serde::{Deserialize, Serialize};

/// The active build platform. Missing attributes never match an equality test
/// against them (but are fine when no dependency constrains that attribute).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Platform {
    #[serde(default)]
    pub os: Option<String>,
    #[serde(default)]
    pub arch: Option<String>,
    #[serde(default)]
    pub family: Option<String>,
}

/// A platform predicate. `null`/absent means "every platform".
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Target {
    Os(String),
    Arch(String),
    Family(String),
    All(Vec<Target>),
    Any(Vec<Target>),
    Not(Box<Target>),
}

impl Target {
    pub fn eval(&self, p: &Platform) -> bool {
        match self {
            Target::Os(os) => p.os.as_deref() == Some(os),
            Target::Arch(a) => p.arch.as_deref() == Some(a),
            Target::Family(f) => p.family.as_deref() == Some(f),
            Target::All(ts) => ts.iter().all(|t| t.eval(p)),
            Target::Any(ts) => ts.iter().any(|t| t.eval(p)),
            Target::Not(t) => !t.eval(p),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn predicates() {
        let linux = Platform { os: Some("linux".into()), arch: Some("x86_64".into()), family: Some("unix".into()) };
        assert!(Target::Os("linux".into()).eval(&linux));
        assert!(!Target::Os("macos".into()).eval(&linux));
        assert!(Target::All(vec![Target::Os("linux".into()), Target::Arch("x86_64".into())]).eval(&linux));
        assert!(!Target::All(vec![Target::Os("linux".into()), Target::Arch("aarch64".into())]).eval(&linux));
        assert!(Target::Any(vec![Target::Os("macos".into()), Target::Family("unix".into())]).eval(&linux));
        assert!(Target::Not(Box::new(Target::Os("windows".into()))).eval(&linux));
    }
}
