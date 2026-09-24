//! Wire types and the in-memory package registry model.

use std::collections::BTreeMap;

use semver::Version;
use serde::{Deserialize, Serialize};

use crate::platform::Target;
use crate::semver_range::Req;

/// One published package version with its dependency edges.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PackageVersion {
    pub name: String,
    pub version: Version,
    #[serde(default)]
    pub dependencies: Vec<Dependency>,
    /// Marker for versions the solver must never pick unless a root
    /// requirement names the exact version.
    #[serde(default)]
    pub yanked: bool,
}

/// A dependency edge from some package version onto another package.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Dependency {
    pub name: String,
    pub req: Req,
    /// When set, this edge is only active if the corresponding feature of the
    /// *dependent* package is enabled. The feature defaults to the dependency
    /// name itself (Cargo convention).
    #[serde(default)]
    pub optional: bool,
    #[serde(default)]
    pub feature: Option<String>,
    /// Platform condition; null/absent = unconditional.
    #[serde(default)]
    pub target: Option<Target>,
}

impl Dependency {
    pub fn feature_name(&self) -> String {
        self.feature.clone().unwrap_or_else(|| self.name.clone())
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Registry {
    /// Package name -> its published versions, in publication order.
    #[serde(default)]
    pub packages: BTreeMap<String, Vec<PackageVersion>>,
}

/// A requirement from the resolution root (the "application").
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RootRequirement {
    pub name: String,
    pub req: Req,
}

/// Fixed selection preference for one package.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(tag = "strategy", rename_all = "snake_case")]
pub enum Preference {
    /// Pick the highest available version satisfying all constraints.
    #[default]
    Highest,
    /// Pick the lowest available version satisfying all constraints.
    Lowest,
    /// Pin an exact version; the solver treats it as an extra constraint and
    /// reports a conflict if no such version is available.
    Pin { version: Version },
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SolveRequest {
    pub registry: Registry,
    #[serde(default)]
    pub requirements: Vec<RootRequirement>,
    /// Features enabled per package, e.g. `{"web": ["tls"]}` enables the
    /// dependency gated behind web's `tls` feature.
    #[serde(default)]
    pub features: BTreeMap<String, Vec<String>>,
    /// Active platform used to evaluate conditional dependencies.
    #[serde(default)]
    pub platform: Option<crate::platform::Platform>,
    /// Package -> fixed selection preference. Packages absent from the map use
    /// the global default (`highest`).
    #[serde(default)]
    pub preferences: BTreeMap<String, Preference>,
    #[serde(default)]
    pub default_preference: DefaultPref,
    /// Global prerelease opt-in.
    #[serde(default)]
    pub allow_prerelease: bool,
    /// When true, run the brute-force oracle and attach its verdict. Only
    /// honored for small problem instances (see solver::ORACLE_NODE_LIMIT).
    #[serde(default)]
    pub verify: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum DefaultPref {
    #[default]
    Highest,
    Lowest,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LockedPackage {
    pub name: String,
    pub version: Version,
    #[serde(default)]
    pub dependencies: BTreeMap<String, String>,
    #[serde(default)]
    pub via: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Lockfile {
    pub version: u32,
    #[serde(default)]
    pub packages: Vec<LockedPackage>,
    /// Fingerprint of the solve inputs, used by replay.
    pub input_fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConflictView {
    pub package: String,
    pub trace: Vec<TraceStep>,
    /// Candidate versions that were tried and rejected, with the constraint
    /// that rejected each one.
    #[serde(default)]
    pub rejections: Vec<RejectionView>,
    /// When the conflict came from a failing subtree, the subtree conflicts.
    #[serde(default)]
    pub nested: Vec<ConflictView>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceStep {
    /// The package whose selection imposed this constraint. `"root"` for
    /// application requirements.
    pub from: String,
    /// Version of `from`, absent for the root.
    pub version: Option<Version>,
    pub constraint: String,
    pub optional: bool,
    pub target_active: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RejectionView {
    pub version: Version,
    pub rejected_by: TraceStep,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct SolveStats {
    pub assignments: usize,
    pub backtracks: usize,
    pub nodes_explored: usize,
    pub elapsed_ms: u128,
    #[serde(default)]
    pub oracle: Option<OracleVerdict>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OracleVerdict {
    pub satisfiable: bool,
    pub combinations_explored: u64,
    pub agrees: bool,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SolveResponse {
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lockfile: Option<Lockfile>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cycles: Option<Vec<Vec<String>>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stats: Option<SolveStats>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conflict: Option<ConflictView>,
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplayRequest {
    pub request: SolveRequest,
    pub lockfile: Lockfile,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplayResponse {
    /// "matches" when re-solving picks exactly the locked versions and the
    /// input fingerprint is unchanged; otherwise a structured mismatch.
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mismatch: Option<Mismatch>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stats: Option<SolveStats>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Mismatch {
    #[serde(default)]
    pub fingerprint_changed: bool,
    #[serde(default)]
    pub missing: Vec<String>,
    #[serde(default)]
    pub extra: Vec<String>,
    #[serde(default)]
    pub changed: Vec<ChangedPackage>,
    #[serde(default)]
    pub unsat: Option<ConflictView>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChangedPackage {
    pub name: String,
    pub locked: Version,
    pub resolved: Version,
}
