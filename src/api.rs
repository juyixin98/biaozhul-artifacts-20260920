// HTTP handlers: parse/validate, assemble the graph+policy, run the solver
// and (optionally) the exhaustive oracle, and render the response.

use axum::extract::rejection::JsonRejection;
use axum::extract::FromRequest;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::json;

use crate::graph::Graph;
use crate::model::{
    AnalyzeRequest, AnalyzeResponse, CycleView, NodeResult, ValidateRequest, ValidateResponse,
};
use crate::policy::ResolvedPolicy;
use crate::solve;
use crate::spdx;

/// JSON extractor that maps every rejection (malformed body, unknown
/// fields, wrong types) to a uniform 400 response.
#[derive(FromRequest)]
#[from_request(via(axum::Json), rejection(AppError))]
pub struct AppJson<T>(pub T);

pub struct AppError(pub String);

impl From<JsonRejection> for AppError {
    fn from(r: JsonRejection) -> Self {
        AppError(r.body_text())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        (StatusCode::BAD_REQUEST, Json(json!({ "error": self.0 }))).into_response()
    }
}

pub fn router() -> Router {
    Router::new()
        .route("/", get(index))
        .route("/health", get(health))
        .route("/api/validate-expression", post(validate_expression))
        .route("/api/analyze", post(analyze))
}

async fn index() -> Json<serde_json::Value> {
    Json(json!({
        "service": "license-policy-propagation",
        "rule_only": "computes policy satisfaction; does not provide legal conclusions",
        "endpoints": {
            "health": "GET /health",
            "validate_expression": "POST /api/validate-expression",
            "analyze": "POST /api/analyze",
        },
    }))
}

async fn health() -> Json<serde_json::Value> {
    Json(json!({ "status": "ok" }))
}

fn bad_request(message: impl Into<String>) -> Response {
    let body = json!({ "error": message.into() });
    (StatusCode::BAD_REQUEST, Json(body)).into_response()
}

async fn validate_expression(
    AppJson(req): AppJson<ValidateRequest>,
) -> Response {
    let response = match spdx::parse(&req.expression) {
        Ok(expr) => ValidateResponse {
            valid: true,
            alternatives: Some(
                expr.dnf
                    .iter()
                    .map(|t| solve::term_view(t, true))
                    .collect(),
            ),
            error: None,
        },
        Err(e) => ValidateResponse {
            valid: false,
            alternatives: None,
            error: Some(e),
        },
    };
    Json(response).into_response()
}

async fn analyze(AppJson(req): AppJson<AnalyzeRequest>) -> Response {
    match run_analyze(req) {
        Ok(resp) => Json(resp).into_response(),
        Err(message) => bad_request(message),
    }
}

fn run_analyze(req: AnalyzeRequest) -> Result<AnalyzeResponse, String> {
    if req.packages.is_empty() {
        return Err("packages must not be empty".to_string());
    }
    let mut ids = Vec::with_capacity(req.packages.len());
    let mut spdx_texts = Vec::with_capacity(req.packages.len());
    for p in &req.packages {
        if p.id.trim().is_empty() {
            return Err("package with empty id".to_string());
        }
        ids.push(p.id.clone());
        spdx_texts.push(p.spdx.clone());
    }
    let exprs: Vec<spdx::SpdxExpr> = req
        .packages
        .iter()
        .map(|p| {
            spdx::parse(&p.spdx).map_err(|e| format!("package '{}': {}", p.id, e))
        })
        .collect::<Result<_, _>>()?;

    let raw_edges: Vec<(String, String, crate::model::Link)> = req
        .edges
        .iter()
        .map(|e| (e.from.clone(), e.to.clone(), e.link))
        .collect();
    let graph = Graph::build(&ids, &raw_edges)?;
    let policy = ResolvedPolicy::resolve(&req.policy)?;
    let prep = solve::prepare(&ids, &spdx_texts, exprs, &graph, &policy)
        .map_err(|(i, e)| format!("package '{}': {}", ids[i], e))?;

    let mut warnings = Vec::new();
    detect_duplicate_edges(&raw_edges, &mut warnings);
    if req.exhaustive_limit == 0 {
        return Err("exhaustive_limit must be >= 1".to_string());
    }

    // Cycles are reported regardless of satisfiability.
    let mut cycles = Vec::new();
    for scc in graph.cyclic_sccs() {
        let (nodes, edges) = graph.cycle_in_scc(&scc);
        cycles.push(CycleView { nodes, edges });
    }

    // Per-package term views with feasibility flags.
    let node_views: Vec<NodeResult> = prep
        .nodes
        .iter()
        .map(|n| NodeResult {
            id: n.id.clone(),
            spdx: n.spdx_text.clone(),
            alternatives: n
                .expr
                .dnf
                .iter()
                .zip(n.term_status.iter())
                .map(|(t, st)| {
                    let feasible = st.is_ok();
                    solve::term_view(t, feasible)
                })
                .collect(),
            chosen: None,
        })
        .collect();

    let solution = solve::backtrack(&prep, &graph, &policy);
    let satisfiable = solution.is_some();

    let mut selection = node_views;
    let mut active_obligations = Vec::new();
    let mut conflicts = Vec::new();

    if let Some(sol) = solution {
        for (i, view) in selection.iter_mut().enumerate() {
            let ti = sol.chosen_terms[i];
            view.chosen = Some(solve::term_view(
                &prep.nodes[i].expr.dnf[ti],
                true,
            ));
        }
        active_obligations = sol
            .obligations
            .iter()
            .map(|o| solve::to_obligation(&prep, o))
            .collect();
        // stable, readable obligation ordering
        active_obligations.sort_by(|a, b| {
            a.source
                .cmp(&b.source)
                .then_with(|| a.source_license.cmp(&b.source_license))
        });
    } else {
        // Explain infeasibility: allow-matrix/static reasons first, then a
        // concrete copyleft conflict path when domains are non-empty.
        conflicts = solve::static_conflicts(&prep);
        if conflicts.is_empty() {
            if let Some(witness) = solve::copyleft_conflict_witness(&prep, &graph, &policy) {
                conflicts.push(witness);
            }
        }
    }

    let exhaustive = if req.exhaustive {
        let cap = req.exhaustive_limit;
        let result = solve::enumerate(&prep, &graph, &policy, cap);
        if result.capped {
            warnings.push(format!(
                "exhaustive enumeration stopped after {} assignments; \
                 feasible_assignments is a lower bound",
                cap
            ));
        }
        Some(solve::exhaustive_view(&result, satisfiable, cap))
    } else {
        None
    };

    Ok(AnalyzeResponse {
        satisfiable,
        selection,
        conflicts,
        cycles,
        active_obligations,
        exhaustive,
        warnings,
    })
}

fn detect_duplicate_edges(
    edges: &[(String, String, crate::model::Link)],
    warnings: &mut Vec<String>,
) {
    use std::collections::HashSet;
    let mut seen = HashSet::new();
    for (from, to, link) in edges {
        let key = (from, to, link);
        if !seen.insert(key) {
            warnings.push(format!(
                "duplicate edge {} -> {} ({:?})",
                from, to, link
            ));
        }
    }
}
