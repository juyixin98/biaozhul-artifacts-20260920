// Request/response data models for the HTTP API.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Deserialize, Serialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Link {
    #[default]
    Static,
    Dynamic,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Strength {
    Weak,
    Strong,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum UnknownPolicy {
    Allow,
    #[default]
    Reject,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PackageReq {
    pub id: String,
    pub spdx: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EdgeReq {
    pub from: String,
    pub to: String,
    /// How `from` (consumer) links `to` (dependency). Defaults to "static".
    #[serde(default)]
    pub link: Link,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PolicyReq {
    /// License -> contexts under which a package may carry that license.
    /// Contexts: "static" and "dynamic" (incoming dependency edge),
    /// or "*" meaning a root package / any context.
    #[serde(default)]
    pub allowed_licenses: std::collections::BTreeMap<String, Vec<String>>,

    /// License ids the project treats as known even if absent from the
    /// built-in illustrative table.
    #[serde(default)]
    pub extra_licenses: Vec<String>,

    /// Exception ids allowed to carry copyleft-obligation suppression.
    /// If empty, a built-in illustrative set is used.
    #[serde(default)]
    pub exceptions: Vec<String>,

    /// Override the copyleft classification of a license.
    /// Map license -> "none" | "weak" | "strong".
    #[serde(default)]
    pub copyleft: std::collections::BTreeMap<String, String>,

    /// Custom copyleft compatibility matrix.
    /// For an X obligation arriving at a package whose chosen atom is L,
    /// L must be listed under compatible_with[X] (or under the "*" key).
    #[serde(default)]
    pub compatible_with: std::collections::BTreeMap<String, Vec<String>>,

    /// Link types along which strong copyleft obligations propagate.
    #[serde(default)]
    pub strong_propagates_on: Option<Vec<String>>,

    /// Link types along which weak copyleft obligations propagate.
    #[serde(default)]
    pub weak_propagates_on: Option<Vec<String>>,

    /// How unknown licenses are handled. Defaults to "reject".
    #[serde(default)]
    pub unknown_licenses: UnknownPolicy,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnalyzeRequest {
    pub packages: Vec<PackageReq>,
    #[serde(default)]
    pub edges: Vec<EdgeReq>,
    #[serde(default)]
    pub policy: PolicyReq,
    /// Enumerate all satisfying choices and cross-check the solver.
    /// Stops counting after `exhaustive_limit` assignments.
    #[serde(default)]
    pub exhaustive: bool,
    #[serde(default = "default_limit")]
    pub exhaustive_limit: usize,
}

fn default_limit() -> usize {
    100_000
}

#[derive(Debug, Clone, Serialize)]
pub struct TermView {
    pub expression: String,
    pub atoms: Vec<String>,
    /// False when this alternative is ruled out up-front (unknown license,
    /// disallowed exception, or allow-matrix mismatch for incoming links).
    pub feasible: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct NodeResult {
    pub id: String,
    pub spdx: String,
    pub alternatives: Vec<TermView>,
    pub chosen: Option<TermView>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ConflictView {
    /// Chosen license is not allowed for the package's incoming context.
    Allow {
        package: String,
        license: String,
        context: String,
        detail: String,
    },
    /// A copyleft obligation reached a package whose chosen license is
    /// incompatible per the matrix.
    Copyleft {
        source: String,
        source_license: String,
        strength: Strength,
        path: Vec<EdgeHop>,
        target: String,
        target_license: String,
        /// True if the target's chosen atom carries an exception. The
        /// exception suppresses the target emitting *new* obligations; it
        /// does not by itself grant compatibility with obligations arriving
        /// from other packages.
        target_has_exception: bool,
        detail: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EdgeHop {
    pub from: String,
    pub to: String,
    pub link: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleView {
    pub nodes: Vec<String>,
    pub edges: Vec<EdgeHop>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ObligationView {
    pub source: String,
    pub source_license: String,
    pub strength: Strength,
    pub exception: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExhaustiveView {
    pub feasible_assignments: usize,
    pub total_assignments_of_feasible_terms: usize,
    pub capped: bool,
    pub cap: usize,
    pub agrees_with_solver: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct AnalyzeResponse {
    pub satisfiable: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub selection: Vec<NodeResult>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub conflicts: Vec<ConflictView>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub cycles: Vec<CycleView>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub active_obligations: Vec<ObligationView>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exhaustive: Option<ExhaustiveView>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidateRequest {
    pub expression: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ValidateResponse {
    pub valid: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub alternatives: Option<Vec<TermView>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}
