//! Wire DTOs for the HTTP API.
//!
//! Request model (see `examples/` and `README.md`):
//!
//! ```json
//! {
//!   "request_id": "optional-caller-id",
//!   "outer": { "relation": "orders" }                      // catalog name …
//!   "outer": { "name": "orders", "schema": [...], "rows": [...] }, // … or inline
//!   "relations": [ <inline relations overlaid on the catalog> ],
//!   "subqueries": [
//!     { "alias": "has_items", "type": "exists", "input": "items",
//!       "correlations": [ {"outer": {"name":"id"}, "inner": {"relation":"i","name":"order_id"} } ] }
//!   ]
//! }
//! ```

use crate::query::SubquerySpec;
use serde::{Deserialize, Serialize};

/// Either a reference to a catalog/overlay relation, or a full inline
/// relation. The untagged form keeps requests compact.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum RelationSpec {
    /// Reference by name to a catalog fixture or an inline overlay.
    Named {
        relation: String,
    },
    /// Complete inline typed relation.
    Inline(crate::query::RelationInput),
}

impl RelationSpec {
    pub fn display_name(&self) -> &str {
        match self {
            RelationSpec::Named { relation } => relation,
            RelationSpec::Inline(r) => &r.name,
        }
    }
}

/// A query request.
#[derive(Debug, Clone, Deserialize)]
pub struct QueryRequest {
    /// Caller-correlated identity. Echoed everywhere in response/logs; the
    /// service generates one (`req_…`) when absent.
    #[serde(default)]
    pub request_id: Option<String>,
    /// The outer (driving) relation.
    pub outer: RelationSpec,
    /// Correlated subqueries, applied in declaration order as appended columns.
    pub subqueries: Vec<SubquerySpec>,
    /// Inline relation fixtures overlaid onto the catalog for this request
    /// only (never written back to shared state).
    #[serde(default)]
    pub relations: Vec<crate::query::RelationInput>,
}

/// One processing step of the service pipeline, for explainability.
#[derive(Debug, Clone, Serialize)]
pub struct StepView {
    pub index: usize,
    pub name: &'static str,
    pub status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// View of one subquery's rewrite.
#[derive(Debug, Clone, Serialize)]
pub struct RewrittenSubqueryView {
    pub alias: String,
    pub kind: String,
    pub sql_rewrite: String,
    pub physical_steps: Vec<crate::query::rewrite::PhysicalStep>,
    pub applicability_checks: Vec<crate::query::rewrite::ApplicabilityCheck>,
}

/// Result columns from one execution strategy.
#[derive(Debug, Clone, Serialize)]
pub struct StrategyView {
    pub strategy: String,
    pub status: String,
    /// Appended subquery result columns as JSON values in outer-row order.
    pub columns: Vec<ColumnView>,
    pub trace: Vec<crate::exec::TraceEvent>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<crate::exec::ExecError>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ColumnView {
    pub name: String,
    #[serde(rename = "type")]
    pub col_type: String,
    pub values: Vec<serde_json::Value>,
}

/// Final materialized rows: the original outer columns plus appended results.
#[derive(Debug, Clone, Serialize)]
pub struct ResultRowsView {
    pub row_count: usize,
    pub columns: Vec<String>,
    pub rows: Vec<serde_json::Map<String, serde_json::Value>>,
}

/// Successful response envelope.
#[derive(Debug, Clone, Serialize)]
pub struct QueryResponse {
    pub request_id: String,
    pub status: String,
    pub service_version: String,
    pub steps: Vec<StepView>,
    pub outer_relation: String,
    pub rewrite: RewriteView,
    pub results: ResultsView,
    /// Uncertain, non-fatal observations kept separate from hard results.
    pub uncertainties: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct RewriteView {
    pub subqueries: Vec<RewrittenSubqueryView>,
    pub proof: crate::query::rewrite::ProofBundle,
}

#[derive(Debug, Clone, Serialize)]
pub struct ResultsView {
    pub naive: StrategyView,
    pub decorrelated: StrategyView,
    /// Both strategies yielded identical values (or the identical error).
    pub equivalent: bool,
    pub final_rows: Option<ResultRowsView>,
}

/// Error response envelope. Failures and their reasons live in a dedicated
/// section rather than being mixed into normal results.
#[derive(Debug, Clone, Serialize)]
pub struct ErrorResponse {
    pub request_id: String,
    pub status: String,
    pub service_version: String,
    pub error: ErrorView,
    pub steps: Vec<StepView>,
    pub uncertainties: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ErrorView {
    pub category: String,
    pub code: String,
    pub message: String,
    pub at_step: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub at_outer_row: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<serde_json::Value>,
}

impl ErrorView {
    pub fn from_query_error(e: &crate::query::QueryError) -> Self {
        let at_outer_row = e
            .detail
            .as_ref()
            .and_then(|d| d.get("outer_row"))
            .and_then(|v| v.as_u64())
            .map(|n| n as usize);
        ErrorView {
            category: format!("{:?}", e.category),
            code: e.code.to_string(),
            message: e.message.clone(),
            at_step: e.at_step.clone(),
            at_outer_row,
            detail: e.detail.clone(),
        }
    }
}

/// Health / metadata response.
#[derive(Debug, Clone, Serialize)]
pub struct HealthResponse {
    pub status: String,
    pub service_version: String,
    pub catalog_relations: Vec<String>,
    pub supported_kinds: Vec<&'static str>,
}
