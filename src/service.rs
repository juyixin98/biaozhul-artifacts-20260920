//! End-to-end query entry point.
//!
//! This is the only place the pipeline stages are orchestrated:
//! ```text
//! SQL --parse--> IR --validate--> ValidatedQuery --compile--> Plan
//!                     |                               |
//!                     +--> interpreter (reference) ---+
//!                     +--> decorrelated executor -----+
//! ```
//! The HTTP handler and every test go through here, so request identity,
//! logging, and the before/after equivalence check live in exactly one place.

use serde::Serialize;
use tracing::{debug, instrument};

use crate::batch::ScalarType;
use crate::config::Config;
use crate::decorrel;
use crate::error::{EngineError, EngineResult};
use crate::interpreter;
use crate::parser;
use crate::semantic::{self, ValidatedQuery};
use crate::state::{Catalog, FixtureTable};

/// Execution strategy requested.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    /// Row-by-row reference executor only.
    Interpreter,
    /// Decorrelating rewrite + hash-group executor only.
    Decorrelated,
    /// Run BOTH and require identical results/errors (default for verify).
    Both,
}

impl Mode {
    pub fn parse(s: &str) -> EngineResult<Mode> {
        match s.to_ascii_lowercase().as_str() {
            "interpreter" | "reference" => Ok(Mode::Interpreter),
            "decorrelated" | "rewrite" => Ok(Mode::Decorrelated),
            "both" | "verify" => Ok(Mode::Both),
            other => Err(EngineError::validation(format!(
                "unknown mode '{other}' (want interpreter|decorrelated|both)"
            ))),
        }
    }
}

/// One logical query job, carrying its correlation id through every stage.
pub struct QueryJob<'a> {
    pub request_id: &'a str,
    pub sql: &'a str,
    pub mode: Mode,
    pub base_catalog: &'a Catalog,
    pub inline_tables: Vec<FixtureTable>,
    pub config: &'a Config,
}

#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct ColumnDto {
    pub name: String,
    pub r#type: String,
}

#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct ResultSetDto {
    pub columns: Vec<ColumnDto>,
    pub row_count: usize,
    pub rows: Vec<Vec<serde_json::Value>>,
}

#[derive(Debug, Serialize)]
pub struct ErrorDto {
    pub kind: String,
    pub message: String,
}

impl ErrorDto {
    pub fn from(e: &EngineError) -> Self {
        ErrorDto {
            kind: e.kind.as_str().to_string(),
            message: e.message.clone(),
        }
    }
}

/// Outcome of one executor.
#[derive(Debug, Serialize)]
pub struct ExecutionDto {
    pub strategy: String,
    pub location: String,
    /// Ok result set, or null.
    pub result: Option<ResultSetDto>,
    /// Structured error, or null.
    pub error: Option<ErrorDto>,
    /// Number of inner-table row scans performed (reference always scans all
    /// matching candidates; rewrite scans each inner row exactly once at build).
    pub inner_scan_complexity: String,
}

/// Full job result returned by the service/API.
#[derive(Debug, Serialize)]
pub struct JobReport {
    pub request_id: String,
    pub engine_version: String,
    pub sql: String,
    pub mode: String,
    pub stages: Vec<decorrel::PlanStep>,
    /// Primary result (rewritten by default; reference in interpreter mode).
    pub result: Option<ResultSetDto>,
    pub error: Option<ErrorDto>,
    /// Present only in `both` mode: the equivalence verdict.
    pub equivalence: Option<EquivalenceReport>,
    /// Per-subquery applicability proof of the decorrelating rewrite.
    pub rewrite_proof: Vec<decorrel::SubqueryProof>,
    /// Per-executor detail.
    pub executions: Vec<ExecutionDto>,
    /// Things the engine could not prove or that callers should know.
    pub caveats: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct EquivalenceReport {
    pub equivalent: bool,
    pub basis: String,
    pub mismatch: Option<String>,
}

/// Front-load stages that can fail before execution (parse/validate/compile).
struct Prepared {
    validated: ValidatedQuery,
    catalog: Catalog,
    plan: decorrel::CompiledPlan,
}

impl<'a> QueryJob<'a> {
    #[instrument(skip(self), fields(request_id = %self.request_id))]
    pub fn run(self) -> JobReport {
        debug!(sql = %self.sql, mode = ?self.mode, "query job started");
        let prepared = self.prepare();

        let (stages, rewrite_proof, prepared) = match prepared {
            Ok(p) => (p.plan.describe(), p.plan.proofs(), Some(p)),
            Err(e) => {
                // Parse/validate/compile errors are shared: both "strategies"
                // fail identically before any execution. Report that honestly.
                return self.report_pre_exec_failure(e);
            }
        };
        let prepared = prepared.unwrap();

        let mut executions = Vec::new();
        let mut reference: Result<ResultSetDto, EngineError> =
            Err(EngineError::internal("reference not run"));
        let mut rewritten: Result<ResultSetDto, EngineError> =
            Err(EngineError::internal("rewrite not run"));

        if matches!(self.mode, Mode::Interpreter | Mode::Both) {
            let r = interpreter::execute(&prepared.validated, &prepared.catalog)
                .map(|b| to_dto(&prepared.validated, b));
            executions.push(execution_dto("row_by_row_interpreter", "src/interpreter/mod.rs", &r, "O(outer_rows x inner_rows) rescans"));
            reference = r;
        }
        if matches!(self.mode, Mode::Decorrelated | Mode::Both) {
            let r = decorrel::execute(&prepared.validated, &prepared.plan, &prepared.catalog)
                .map(|b| to_dto(&prepared.validated, b));
            executions.push(execution_dto("decorrelated_hash_groups", "src/decorrel/mod.rs", &r, "one inner scan at build; O(1) key probe per outer row"));
            rewritten = r;
        }

        // Equivalence is judged while both results are still owned.
        let equivalence = if self.mode == Mode::Both {
            Some(compare(&reference, &rewritten))
        } else {
            None
        };

        // Pick the primary result/error.
        let (primary_result, primary_error) = match self.mode {
            Mode::Interpreter => take(reference),
            Mode::Decorrelated => take(rewritten),
            // Primary follows the reference outcome; the rewrite's own
            // result/error is always present in `executions`, and any
            // disagreement is called out explicitly in `equivalence`.
            Mode::Both => take(reference),
        };

        let caveats = prepared_caveats(&prepared.validated);

        JobReport {
            request_id: self.request_id.to_string(),
            engine_version: decorrel::ENGINE_VERSION.to_string(),
            sql: self.sql.to_string(),
            mode: format!("{:?}", self.mode).to_lowercase(),
            stages,
            result: primary_result,
            error: primary_error,
            equivalence,
            rewrite_proof,
            executions,
            caveats,
        }
    }

    fn prepare(&self) -> EngineResult<Prepared> {
        let ir = parser::parse_query(self.sql)?;
        let catalog = self.base_catalog.with_inline(self.inline_tables.clone())?;
        if let Err(e) = enforce_limits(&ir, &catalog, self.config) {
            return Err(e);
        }
        let validated = semantic::validate(&ir, &catalog)?;
        let plan = decorrel::compile(&validated);
        Ok(Prepared {
            validated,
            catalog,
            plan,
        })
    }

    fn report_pre_exec_failure(&self, e: EngineError) -> JobReport {
        // Without a validated query there are no rewrite stages; show the
        // pipeline location where the failure occurred.
        let location = match e.kind {
            crate::error::ErrorKind::Parse => "src/parser/mod.rs",
            crate::error::ErrorKind::UnsupportedForm => "src/parser/mod.rs",
            _ => "src/semantic.rs",
        };
        let stage = decorrel::PlanStep {
            order: 0,
            stage: "rejected".to_string(),
            detail: format!("query rejected before execution: {}", e.kind),
            location: location.to_string(),
            engine_version: decorrel::ENGINE_VERSION.to_string(),
        };
        JobReport {
            request_id: self.request_id.to_string(),
            engine_version: decorrel::ENGINE_VERSION.to_string(),
            sql: self.sql.to_string(),
            mode: format!("{:?}", self.mode).to_lowercase(),
            stages: vec![stage],
            result: None,
            error: Some(ErrorDto::from(&e)),
            equivalence: None,
            rewrite_proof: vec![],
            executions: vec![],
            caveats: vec![],
        }
    }
}

fn enforce_limits(
    ir: &crate::ast::Query,
    catalog: &Catalog,
    config: &Config,
) -> EngineResult<()> {
    let outer = catalog.get(&ir.outer_table)?;
    if outer.batch.n_rows > config.max_outer_rows_per_query {
        return Err(EngineError::new(
            crate::error::ErrorKind::LimitExceeded,
            format!(
                "outer table has {} rows, query limit is {}",
                outer.batch.n_rows, config.max_outer_rows_per_query
            ),
        ));
    }
    Ok(())
}

fn prepared_caveats(q: &ValidatedQuery) -> Vec<String> {
    let mut notes = Vec::new();
    for (i, sq) in q.subqueries.iter().enumerate() {
        if sq.correlation.eq_keys.is_empty() {
            notes.push(format!(
                "subquery[{i}] has no equi-correlation key: rewrite falls back to a \
                 single bucket scanned per outer row (still semantically identical)"
            ));
        }
        if matches!(
            sq.kind,
            semantic::ValidatedSubKind::ScalarColumn { .. }
        ) {
            notes.push(format!(
                "subquery[{i}] is a bare scalar column: >1 matching row raises \
                 cardinality_violation at runtime in both strategies"
            ));
        }
    }
    notes
}

fn take(r: Result<ResultSetDto, EngineError>) -> (Option<ResultSetDto>, Option<ErrorDto>) {
    match r {
        Ok(rs) => (Some(rs), None),
        Err(e) => (None, Some(ErrorDto::from(&e))),
    }
}

fn execution_dto(
    strategy: &str,
    location: &str,
    r: &Result<ResultSetDto, EngineError>,
    complexity: &str,
) -> ExecutionDto {
    let (result, error) = match r {
        Ok(rs) => (Some(rs.clone()), None),
        Err(e) => (None, Some(ErrorDto::from(e))),
    };
    ExecutionDto {
        strategy: strategy.to_string(),
        location: location.to_string(),
        result,
        error,
        inner_scan_complexity: complexity.to_string(),
    }
}

fn compare(
    reference: &Result<ResultSetDto, EngineError>,
    rewritten: &Result<ResultSetDto, EngineError>,
) -> EquivalenceReport {
    let basis = "differential check: same validated query; exact equality of column \
                schema and every output row (incl. NULLs/duplicates), or same error \
                kind+message"
        .to_string();
    match (reference, rewritten) {
        (Ok(a), Ok(b)) => {
            if a == b {
                EquivalenceReport {
                    equivalent: true,
                    basis,
                    mismatch: None,
                }
            } else {
                EquivalenceReport {
                    equivalent: false,
                    basis,
                    mismatch: Some(describe_result_diff(a, b)),
                }
            }
        }
        (Err(ea), Err(eb)) => {
            if ea.kind == eb.kind && ea.message == eb.message {
                EquivalenceReport {
                    equivalent: true,
                    basis,
                    mismatch: None,
                }
            } else {
                EquivalenceReport {
                    equivalent: false,
                    basis,
                    mismatch: Some(format!(
                        "reference failed [{}] {}; rewrite failed [{}] {}",
                        ea.kind, ea.message, eb.kind, eb.message
                    )),
                }
            }
        }
        (Ok(a), Err(eb)) => EquivalenceReport {
            equivalent: false,
            basis,
            mismatch: Some(format!(
                "reference returned {} rows but rewrite failed [{}] {}",
                a.row_count, eb.kind, eb.message
            )),
        },
        (Err(ea), Ok(b)) => EquivalenceReport {
            equivalent: false,
            basis,
            mismatch: Some(format!(
                "reference failed [{}] {} but rewrite returned {} rows",
                ea.kind, ea.message, b.row_count
            )),
        },
    }
}

fn describe_result_diff(a: &ResultSetDto, b: &ResultSetDto) -> String {
    if a.columns.len() != b.columns.len()
        || a.columns
            .iter()
            .zip(&b.columns)
            .any(|(x, y)| x.name != y.name || x.r#type != y.r#type)
    {
        return format!(
            "schema mismatch: reference {:?} vs rewrite {:?}",
            a.columns
                .iter()
                .map(|c| format!("{}:{}", c.name, c.r#type))
                .collect::<Vec<_>>(),
            b.columns
                .iter()
                .map(|c| format!("{}:{}", c.name, c.r#type))
                .collect::<Vec<_>>()
        );
    }
    if a.row_count != b.row_count {
        return format!(
            "row count mismatch: reference {} vs rewrite {}",
            a.row_count, b.row_count
        );
    }
    for (i, (ra, rb)) in a.rows.iter().zip(&b.rows).enumerate() {
        if ra != rb {
            return format!("first differing row at index {i}: reference {ra:?} vs rewrite {rb:?}");
        }
    }
    "result sets differ in an undetected way".to_string()
}

fn to_dto(q: &ValidatedQuery, batch: crate::batch::Batch) -> ResultSetDto {
    let columns = q
        .outputs
        .iter()
        .map(|o| ColumnDto {
            name: o.name.clone(),
            r#type: type_name(o.ty),
        })
        .collect();
    ResultSetDto {
        columns,
        row_count: batch.n_rows,
        rows: batch.to_json_rows(),
    }
}

fn type_name(t: ScalarType) -> String {
    t.name().to_string()
}
