//! Orchestration service.
//!
//! Pipeline for every request (each stage recorded for explainability):
//!
//! 1. **validate** — resolve names/types, reject unsupported forms ([`crate::api::validate`])
//! 2. **rewrite**  — decorrelate, emit physical steps + applicability proof ([`crate::query::rewrite`])
//! 3. **execute**  — run BOTH the naïve reference and the decorrelated pipeline
//! 4. **verify**   — compare values or identical failure category; fail closed
//!
//! Outcomes are explicit: a request can succeed with rows, or both strategies
//! can fail identically (e.g. a multi-row scalar counterexample) — that is a
//! verified *error* outcome, not a service fault.

use crate::api::types::*;
use crate::api::validate::validate;
use crate::api::verify::{compare, mismatch_error};
use crate::batch::build_column;
use crate::catalog::Catalog;
use crate::exec::{naive, pipeline, StrategyOutput};
use crate::query::rewrite::rewrite;
use crate::query::QueryError;

pub const SERVICE_VERSION: &str = env!("CARGO_PKG_VERSION");
pub const SERVICE_BUILD_AT: &str = "src/api/service.rs";

/// Outcome of a processed request.
pub enum Outcome {
    /// Both strategies agreed on rows.
    Success(QueryResponse),
    /// Both strategies executed and agreed on the same failure (e.g.
    /// cardinality violation). The error is verified, still reported with the
    /// full comparison views.
    VerifiedError { status: u16, response: ErrorResponse, results: ResultsView },
    /// Request rejected before execution (validation / rewrite), or strategies
    /// disagreed (fail-closed).
    Rejected { status: u16, response: ErrorResponse },
}

/// Entry point used by the HTTP handler. `raw_body` is the parsed request.
pub fn handle(request: QueryRequest, request_id: String, catalog: &Catalog) -> Outcome {
    let mut steps: Vec<StepView> = Vec::new();
    let uncertainties = documented_uncertainties();

    // ── Stage 1: validate ──────────────────────────────────────────────────
    let validated = match validate(&request, &request_id, catalog) {
        Ok(v) => {
            steps.push(StepView {
                index: 1,
                name: "validate",
                status: "ok",
                detail: Some(format!(
                    "{} outer row(s); {} subqueries validated",
                    v.outer.len(),
                    v.subqueries.len()
                )),
            });
            v
        }
        Err(e) => {
            steps.push(StepView { index: 1, name: "validate", status: "error", detail: Some(e.message.clone()) });
            return reject(request_id, steps, uncertainties, e);
        }
    };

    // ── Stage 2: rewrite ───────────────────────────────────────────────────
    let decorated = match rewrite(&validated) {
        Ok(d) => {
            steps.push(StepView {
                index: 2,
                name: "rewrite",
                status: "ok",
                detail: Some(format!(
                    "{} subqueries decorrelated; applicability checks passed",
                    d.subqueries.len()
                )),
            });
            d
        }
        Err(e) => {
            steps.push(StepView { index: 2, name: "rewrite", status: "error", detail: Some(e.message.clone()) });
            return reject(request_id, steps, uncertainties, e);
        }
    };

    // ── Stage 3: execute both strategies ───────────────────────────────────
    let naive_out = naive::execute(&validated);
    let pipe_out = pipeline::execute(&validated);
    steps.push(StepView {
        index: 3,
        name: "execute",
        status: "ok",
        detail: Some(format!(
            "ran reference `{}` and decorrelated `{}`",
            naive_out.strategy, pipe_out.strategy
        )),
    });

    // ── Stage 4: verify equivalence ────────────────────────────────────────
    let report = compare(&naive_out, &pipe_out);
    if !report.equivalent {
        let e = mismatch_error(&report);
        steps.push(StepView {
            index: 4,
            name: "verify",
            status: "error",
            detail: Some(format!("{} discrepancy/ies", report.discrepancies.len())),
        });
        return reject(request_id, steps, uncertainties, e);
    }
    steps.push(StepView {
        index: 4,
        name: "verify",
        status: "ok",
        detail: Some(match (naive_out.error.is_some(), pipe_out.error.is_some()) {
            (false, false) => "all appended values identical across strategies".to_string(),
            (true, true) => "both strategies failed with the same category, code and outer row".to_string(),
            _ => unreachable!("non-equivalent case handled above"),
        }),
    });

    // ── Assemble shared views ──────────────────────────────────────────────
    let rewrite_view = RewriteView {
        subqueries: decorated
            .subqueries
            .iter()
            .map(|s| RewrittenSubqueryView {
                alias: s.plan.alias.clone(),
                kind: format!("{:?}", s.plan.kind),
                sql_rewrite: s.sql_rewrite.clone(),
                physical_steps: s.steps.clone(),
                applicability_checks: s.checks.clone(),
            })
            .collect(),
        proof: decorated.proof.clone(),
    };

    let naive_view = strategy_view(&naive_out);
    let pipe_view = strategy_view(&pipe_out);

    // Both failed identically → verified error outcome.
    if let (Some(_err_n), Some(err_p)) = (&naive_out.error, &pipe_out.error) {
        let results = ResultsView {
            naive: naive_view,
            decorrelated: pipe_view,
            equivalent: true,
            final_rows: None,
        };
        let representative = err_p;
        let error = ErrorView {
            category: representative.category.clone(),
            code: representative.code.clone(),
            message: representative.message.clone(),
            at_step: representative.at_step.clone(),
            at_outer_row: representative.at_outer_row,
            detail: representative.detail.clone(),
        };
        let status = category_status(&error.category);
        let response = ErrorResponse {
            request_id: request_id.clone(),
            status: "verified_error".to_string(),
            service_version: SERVICE_VERSION.to_string(),
            error,
            steps,
            uncertainties,
        };
        return Outcome::VerifiedError { status, response, results };
    }

    // Both succeeded → materialize final typed rows.
    let final_rows = materialize_final_rows(&validated, &pipe_out);
    let results = ResultsView {
        naive: naive_view,
        decorrelated: pipe_view,
        equivalent: true,
        final_rows: Some(final_rows),
    };

    Outcome::Success(QueryResponse {
        request_id,
        status: "ok".to_string(),
        service_version: SERVICE_VERSION.to_string(),
        steps,
        outer_relation: validated.outer_name.clone(),
        rewrite: rewrite_view,
        results,
        uncertainties,
    })
}

/// Build the final columns: original outer columns plus appended subquery
/// results, constructed as arrow2 arrays through the typed-batch module.
fn materialize_final_rows(
    q: &crate::query::plan::ValidatedQuery,
    out: &StrategyOutput,
) -> ResultRowsView {
    use crate::batch::Batch;

    let mut fields = q.outer.fields().to_vec();
    let mut arrays: Vec<Box<dyn arrow2::array::Array>> = q.outer.columns().to_vec();
    for col in &out.appended {
        arrays.push(build_column(col.field.col_type, &col.values));
        fields.push(col.field.clone());
    }
    let combined = Batch::new(fields, arrays);

    ResultRowsView {
        row_count: combined.len(),
        columns: combined.fields().iter().map(|f| f.name.clone()).collect(),
        rows: combined.rows_json(),
    }
}

fn strategy_view(out: &StrategyOutput) -> StrategyView {
    StrategyView {
        strategy: out.strategy.to_string(),
        status: if out.error.is_some() { "error".to_string() } else { "ok".to_string() },
        columns: out
            .appended
            .iter()
            .map(|c| ColumnView {
                name: c.field.name.clone(),
                col_type: format!("{:?}", c.field.col_type),
                values: c
                    .values
                    .iter()
                    .map(|d| d.value.as_ref().map(|s| s.as_json()).unwrap_or(serde_json::Value::Null))
                    .collect(),
            })
            .collect(),
        trace: out.trace.clone(),
        error: out.error.clone(),
    }
}

fn category_status(category: &str) -> u16 {
    match category {
        "CardinalityViolation" | "NumericOverflow" => 422,
        "EquivalenceMismatch" | "Internal" => 500,
        _ => 400,
    }
}

fn documented_uncertainties() -> Vec<String> {
    vec![
        "Float64 SUM accumulates in inner-row order; floating-point rounding is therefore order-dependent (documented limitation)".to_string(),
        "NaN is not supported as a value/group key (bit-equal NaNs would collapse into one group)".to_string(),
    ]
}

/// Assemble a fail-closed rejection response.
fn reject(
    request_id: String,
    steps: Vec<StepView>,
    uncertainties: Vec<String>,
    e: QueryError,
) -> Outcome {
    Outcome::Rejected {
        status: e.category.http_status(),
        response: ErrorResponse {
            request_id,
            status: "rejected".to_string(),
            service_version: SERVICE_VERSION.to_string(),
            error: ErrorView::from_query_error(&e),
            steps,
            uncertainties,
        },
    }
}

impl From<QueryResponse> for Outcome {
    fn from(r: QueryResponse) -> Self {
        Outcome::Success(r)
    }
}
