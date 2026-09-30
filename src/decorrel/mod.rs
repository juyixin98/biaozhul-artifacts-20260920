//! Decorrelating compiler and executor.
//!
//! Rewrite applied (magic-set / hash-groupby style unnesting):
//!
//! ```text
//! for each outer row o:
//!     SELECT agg FROM inner i WHERE o.k1 = i.k1 AND ... AND P(o,i)
//! ```
//! is rewritten into
//! ```text
//! build:  scan inner once
//!         apply inner-only local filters
//!         group surviving rows by inner correlation key H(i.k1, ...)
//! probe:  for each outer row o (duplicates preserved)
//!         bucket = map.get(H(o.k1, ...))   (NULL key -> empty bucket)
//!         apply residual correlated predicates P(o,i) over bucket rows only
//!         finish: EXISTS / NOT EXISTS / COUNT / SUM / scalar cardinality
//! ```
//!
//! Semantic load-bearers, all shared with the interpreter via
//! [`crate::operators`] and [`crate::interpreter::finish_subquery`]:
//!   * Outer duplicate rows are iterated, never deduplicated.
//!   * Equi-correlation under NULL: a NULL on either side never matches, so a
//!     NULL outer key probes an empty bucket (EXISTS=false, NOT EXISTS=true,
//!     COUNT=0, SUM=NULL, scalar=NULL).
//!   * COUNT(*) / COUNT(col) on an empty group => 0; SUM(col) => NULL.
//!   * Scalar projections returning >1 row raise cardinality_violation.
//!
//! Error timing: aggregates and cardinality checks are computed LAZILY at
//! first probe of a bucket, never at build time. Precomputing at build time
//! would raise cardinality errors for inner groups that no outer row ever
//! references — observably different from row-by-row execution. Lazy
//! memoization preserves the interpreter's outer-order error timing while
//! still avoiding rescans (duplicate outer rows reuse the cached value).

use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::Arc;

use crate::ast::{CmpOp, ColumnRef, Predicate, ScalarProj, Scope};
use crate::batch::{Batch, Value};
use crate::error::{EngineError, EngineResult};
use crate::interpreter::finish_subquery;
use crate::operators::aggregate;
use crate::operators::predicate::{all_pass, RowEnv};
use crate::operators::runner;
use crate::semantic::{ValidatedQuery, ValidatedSubKind, ValidatedSubquery};
use crate::state::{Catalog, Table};

/// Engine version stamped on every plan/explain output.
pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");
/// Location tag for explainability ("where was this produced").
pub const COMPILER_LOCATION: &str = "src/decorrel/mod.rs";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PlanShape {
    /// Bucket rows; probe evaluates residual correlated predicates; finish.
    Generic,
    /// Pure equi-correlation + local filters: aggregate lazily memoized.
    DirectAgg,
    /// Pure equi-correlation + local filters: EXISTS/NOT EXISTS from presence.
    DirectExists,
    /// Pure equi-correlation + local filters: scalar column, 0/1 cardinality.
    DirectScalar,
}

pub(crate) struct CompiledSubquery {
    inner_table: String,
    /// (outer_col, inner_col) equi-correlation keys, validated order.
    eq_keys: Vec<(String, String)>,
    /// Predicates referencing inner columns only — evaluated once at build.
    local_preds: Vec<Predicate>,
    /// Predicates needing the outer row — evaluated only over bucket rows.
    residual_preds: Vec<Predicate>,
    shape: PlanShape,
}

/// A decorrelated execution plan for one whole query (one build per subquery).
pub struct CompiledPlan {
    pub(crate) subqueries: Vec<CompiledSubquery>,
}

/// Machine-checkable proof of rewrite applicability for one subquery.
///
/// `applicable` is the verdict: every subquery that reaches execution has
/// been accepted by the restricted grammar, so it is always `true`; rejected
/// forms (NOT IN / OR / nested subqueries / ...) never produce a plan and are
/// reported with `unsupported_form` before this point.
#[derive(Debug, Clone, serde::Serialize)]
pub struct SubqueryProof {
    pub subquery_index: usize,
    pub inner_table: String,
    pub applicable: bool,
    pub shape: String,
    pub correlation_keys: Vec<String>,
    pub local_predicates: Vec<String>,
    pub residual_predicates: Vec<String>,
    pub empty_group_rules: Vec<String>,
    pub correctness_basis: Vec<String>,
}

impl CompiledPlan {
    /// One structured applicability proof per subquery (API/文档核验材料).
    pub fn proofs(&self) -> Vec<SubqueryProof> {
        self.subqueries
            .iter()
            .enumerate()
            .map(|(i, sq)| SubqueryProof {
                subquery_index: i,
                inner_table: sq.inner_table.clone(),
                applicable: true,
                shape: format!("{:?}", sq.shape).to_ascii_lowercase(),
                correlation_keys: sq
                    .eq_keys
                    .iter()
                    .map(|(o, c)| format!("outer.{o} = inner.{c}"))
                    .collect(),
                local_predicates: sq.local_preds.iter().map(render_predicate).collect(),
                residual_predicates: sq
                    .residual_preds
                    .iter()
                    .map(render_predicate)
                    .collect(),
                empty_group_rules: vec![
                    "NULL on either side of an equi-key matches no inner row".to_string(),
                    "EXISTS on empty group -> false; NOT EXISTS -> true".to_string(),
                    "COUNT(*)/COUNT(col) on empty group -> 0".to_string(),
                    "SUM(col) on empty (or all-NULL) group -> NULL".to_string(),
                    "scalar column: 0 rows -> NULL; >1 rows -> cardinality_violation".to_string(),
                ],
                correctness_basis: vec![
                    "single inner scan at build; outer rows iterated with duplicates preserved"
                        .to_string(),
                    "residual correlated predicates evaluated only inside the key bucket".to_string(),
                    "aggregates/cardinality evaluated lazily at first probe, matching reference error timing"
                        .to_string(),
                    "finish operator shared with the row-by-row interpreter".to_string(),
                ],
            })
            .collect()
    }

    /// Human-readable, stable description of rewrite steps (proof material).
    pub fn describe(&self) -> Vec<PlanStep> {
        let mut steps = vec![
            PlanStep::stage(
                1,
                "parse",
                "SQL lowered to restricted IR; NOT IN / OR / JOIN / nested subqueries rejected",
                "src/parser/mod.rs",
            ),
            PlanStep::stage(
                2,
                "validate",
                "names, types and equi-correlation keys resolved against catalog",
                "src/semantic.rs",
            ),
        ];
        for (i, sq) in self.subqueries.iter().enumerate() {
            let keys: Vec<String> = sq
                .eq_keys
                .iter()
                .map(|(o, i)| format!("outer.{o} = inner.{i}"))
                .collect();
            steps.push(PlanStep::stage(
                3 + i,
                "decorrelate",
                format!(
                    "subquery[{i}] on {} rewritten: single inner scan grouped by [{}]; \
                     shape={:?}; local_predicates={}, residual_predicates={}; \
                     NULL key -> empty group; outer duplicates preserved; \
                     empty-group COUNT=0 / SUM=NULL; scalar >1 row -> cardinality_violation; \
                     aggregates evaluated lazily at first probe (error timing preserved)",
                    sq.inner_table,
                    keys.join(", "),
                    sq.shape,
                    sq.local_preds.len(),
                    sq.residual_preds.len(),
                ),
                COMPILER_LOCATION,
            ));
        }
        steps.push(PlanStep::stage(
            100,
            "execute",
            "build hash groups once, probe per outer row, emit arrow2 batch in outer order",
            "src/operators/runner.rs",
        ));
        steps
    }
}

/// Stable textual rendering of a classified predicate for proof output.
pub fn render_predicate(p: &Predicate) -> String {
    match p {
        Predicate::And(a, b) => {
            format!("({} AND {})", render_predicate(a), render_predicate(b))
        }
        Predicate::NullTest { col, expect_null } => format!(
            "{}{}",
            render_ref(col),
            if *expect_null { " IS NULL" } else { " IS NOT NULL" }
        ),
        Predicate::LitCmp { col, op, value } => {
            format!("{} {} {}", render_ref(col), render_op(*op), value)
        }
        Predicate::ColCmp { left, op, right } => {
            format!("{} {} {}", render_ref(left), render_op(*op), render_ref(right))
        }
    }
}

fn render_ref(r: &ColumnRef) -> String {
    let scope = match r.scope {
        Scope::Outer => "outer",
        Scope::Inner => "inner",
    };
    match &r.table {
        Some(t) => format!("{scope}.{t}.{}", r.name),
        None => format!("{scope}.{}", r.name),
    }
}

fn render_op(op: crate::ast::CmpOp) -> &'static str {
    match op {
        CmpOp::Eq => "=",
        CmpOp::Neq => "<>",
        CmpOp::Lt => "<",
        CmpOp::Lte => "<=",
        CmpOp::Gt => ">",
        CmpOp::Gte => ">=",
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PlanStep {
    pub order: usize,
    pub stage: String,
    pub detail: String,
    pub location: String,
    pub engine_version: String,
}

impl PlanStep {
    fn stage(order: usize, stage: &str, detail: impl Into<String>, location: &str) -> Self {
        PlanStep {
            order,
            stage: stage.to_string(),
            detail: detail.into(),
            location: location.to_string(),
            engine_version: ENGINE_VERSION.to_string(),
        }
    }
}

pub fn compile(q: &ValidatedQuery) -> CompiledPlan {
    CompiledPlan {
        subqueries: q.subqueries.iter().map(compile_subquery).collect(),
    }
}

fn compile_subquery(sq: &ValidatedSubquery) -> CompiledSubquery {
    let eq_sigs = build_eq_signatures(&sq.correlation.eq_keys);
    let mut local_preds = Vec::new();
    let mut residual_preds = Vec::new();
    for p in &sq.predicates {
        if is_equi_corr(p, &eq_sigs) {
            continue; // realized by the hash join key
        }
        if refs_are_inner_only(p) {
            local_preds.push(p.clone());
        } else {
            residual_preds.push(p.clone());
        }
    }
    let shape = if residual_preds.is_empty() {
        match &sq.kind {
            ValidatedSubKind::ScalarAgg { .. } => PlanShape::DirectAgg,
            ValidatedSubKind::Exists | ValidatedSubKind::NotExists => PlanShape::DirectExists,
            ValidatedSubKind::ScalarColumn { .. } => PlanShape::DirectScalar,
        }
    } else {
        PlanShape::Generic
    };
    CompiledSubquery {
        inner_table: sq.inner_table.clone(),
        eq_keys: sq.correlation.eq_keys.clone(),
        local_preds,
        residual_preds,
        shape,
    }
}

/// `(outer_col, inner_col)` pairs that are the validated equi-correlations.
fn build_eq_signatures(eq_keys: &[(String, String)]) -> Vec<(String, String)> {
    eq_keys.to_vec()
}

fn is_equi_corr(p: &Predicate, eq_keys: &[(String, String)]) -> bool {
    let Predicate::ColCmp { left, op, right } = p else {
        return false;
    };
    if !matches!(op, crate::ast::CmpOp::Eq) {
        return false;
    }
    let pair = match (&left.scope, &right.scope) {
        (Scope::Outer, Scope::Inner) => (&left.name, &right.name),
        (Scope::Inner, Scope::Outer) => (&right.name, &left.name),
        _ => return false,
    };
    eq_keys.iter().any(|(o, i)| o == pair.0 && i == pair.1)
}

fn refs_are_inner_only(p: &Predicate) -> bool {
    let mut inner_only = true;
    for_each_ref(p, &mut |r| {
        if r.scope != Scope::Inner {
            inner_only = false;
        }
    });
    inner_only
}

fn for_each_ref<F: FnMut(&ColumnRef)>(p: &Predicate, f: &mut F) {
    match p {
        Predicate::And(a, b) => {
            for_each_ref(a, f);
            for_each_ref(b, f);
        }
        Predicate::NullTest { col, .. } | Predicate::LitCmp { col, .. } => f(col),
        Predicate::ColCmp { left, right, .. } => {
            f(left);
            f(right);
        }
    }
}

// ---------- execution ----------

/// Collision-safe hash key over correlation columns.
/// Per part: [tag:1][payload bytes], strings length-prefixed.
/// Returns None when ANY part is NULL — such a row belongs to no group.
fn encode_key(parts: &[Value]) -> Option<Vec<u8>> {
    let mut buf = Vec::new();
    for v in parts {
        match v {
            Value::Null => return None,
            Value::Bool(b) => {
                buf.push(1);
                buf.push(*b as u8);
            }
            Value::Int(i) => {
                buf.push(2);
                buf.extend_from_slice(&i.to_le_bytes());
            }
            Value::Float(f) => {
                buf.push(3);
                // Normalize -0.0 to +0.0 bits so -0.0/+0.0 join like SQL `=`;
                // NaN never equals anything in SQL, so exclude it from groups.
                if f.is_nan() {
                    return None;
                }
                buf.extend_from_slice(&((if *f == 0.0 { 0.0 } else { *f }).to_bits()).to_le_bytes());
            }
            Value::Str(s) => {
                buf.push(4);
                let bytes = s.as_bytes();
                buf.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
                buf.extend_from_slice(bytes);
            }
        }
    }
    Some(buf)
}

struct BuiltSubquery {
    inner: Arc<Table>,
    plan: CompiledSubquery,
    /// Correlation key -> inner rows passing local filters.
    groups: HashMap<Vec<u8>, Vec<usize>>,
    /// Lazily memoized direct aggregate results per key.
    agg_cache: RefCell<HashMap<Vec<u8>, Value>>,
}

pub fn execute(
    q: &ValidatedQuery,
    plan: &CompiledPlan,
    catalog: &Catalog,
) -> EngineResult<Batch> {
    let mut built: Vec<BuiltSubquery> = Vec::with_capacity(plan.subqueries.len());
    for plan_sq in &plan.subqueries {
        built.push(build(plan_sq, catalog)?);
    }

    runner::run(q, catalog, |index, outer_batch, outer_row| {
        probe(&q.subqueries[index], &built[index], outer_batch, outer_row)
    })
}

fn build(plan: &CompiledSubquery, catalog: &Catalog) -> EngineResult<BuiltSubquery> {
    let inner = catalog.get(&plan.inner_table)?;
    let batch = &inner.batch;
    let mut groups: HashMap<Vec<u8>, Vec<usize>> = HashMap::new();

    for inner_row in 0..batch.n_rows {
        // Inner-only local predicates: evaluated once, here, with a lookup
        // that refuses outer references (they cannot occur after classification).
        let lookup = |r: &ColumnRef| -> EngineResult<Value> {
            match r.scope {
                Scope::Inner => batch.value(&r.name, inner_row),
                Scope::Outer => Err(EngineError::internal(
                    "outer column reached build-time local evaluation",
                )),
            }
        };
        if !all_pass(&plan.local_preds, &lookup)? {
            continue;
        }
        let mut parts = Vec::with_capacity(plan.eq_keys.len());
        for (_, inner_col) in &plan.eq_keys {
            parts.push(batch.value(inner_col, inner_row)?);
        }
        if let Some(key) = encode_key(&parts) {
            groups.entry(key).or_default().push(inner_row);
        }
    }

    Ok(BuiltSubquery {
        inner,
        plan: clone_plan_meta(plan),
        groups,
        agg_cache: RefCell::new(HashMap::new()),
    })
}

fn clone_plan_meta(plan: &CompiledSubquery) -> CompiledSubquery {
    CompiledSubquery {
        inner_table: plan.inner_table.clone(),
        eq_keys: plan.eq_keys.clone(),
        local_preds: plan.local_preds.clone(),
        residual_preds: plan.residual_preds.clone(),
        shape: plan.shape,
    }
}

fn probe(
    sq: &ValidatedSubquery,
    built: &BuiltSubquery,
    outer_batch: &Batch,
    outer_row: usize,
) -> EngineResult<Value> {
    let inner_batch = &built.inner.batch;

    // Outer correlation key; NULL anywhere => empty group.
    let mut parts = Vec::with_capacity(built.plan.eq_keys.len());
    for (outer_col, _) in &built.plan.eq_keys {
        parts.push(outer_batch.value(outer_col, outer_row)?);
    }
    let key = encode_key(&parts);

    let candidates: &[usize] = match &key {
        Some(k) => built.groups.get(k).map(|v| v.as_slice()).unwrap_or(&[]),
        None => &[],
    };

    match built.plan.shape {
        PlanShape::DirectExists => Ok(Value::Bool(match &sq.kind {
            ValidatedSubKind::Exists => !candidates.is_empty(),
            ValidatedSubKind::NotExists => candidates.is_empty(),
            _ => unreachable!("shape/kind mismatch validated at compile time"),
        })),
        PlanShape::DirectScalar => direct_scalar(sq, inner_batch, candidates),
        PlanShape::DirectAgg => {
            // Lazily memoize: first outer row referencing this key computes it.
            if let Some(k) = &key {
                if let Some(v) = built.agg_cache.borrow().get(k) {
                    return Ok(v.clone());
                }
                let v = direct_agg(sq, inner_batch, candidates)?;
                built.agg_cache.borrow_mut().insert(k.clone(), v.clone());
                Ok(v)
            } else {
                // NULL key: empty group result; nothing to cache.
                direct_agg(sq, inner_batch, candidates)
            }
        }
        PlanShape::Generic => {
            let mut matching = Vec::with_capacity(candidates.len());
            for &inner_row in candidates {
                let env = RowEnv {
                    outer: outer_batch,
                    outer_row,
                    inner: inner_batch,
                    inner_row,
                };
                if all_pass(&built.plan.residual_preds, &|r| env.lookup(r))? {
                    matching.push(inner_row);
                }
            }
            finish_subquery(sq, inner_batch, &matching)
        }
    }
}

fn direct_agg(
    sq: &ValidatedSubquery,
    inner_batch: &Batch,
    candidates: &[usize],
) -> EngineResult<Value> {
    let ValidatedSubKind::ScalarAgg { proj, .. } = &sq.kind else {
        return Err(EngineError::internal("DirectAgg without scalar aggregate kind"));
    };
    let values = match proj {
        ScalarProj::CountStar => vec![None; candidates.len()],
        ScalarProj::CountCol { col } | ScalarProj::SumCol { col, .. } => {
            aggregate::column_values(col, inner_batch, candidates)?
        }
    };
    aggregate::compute(proj, &values)
}

fn direct_scalar(
    sq: &ValidatedSubquery,
    inner_batch: &Batch,
    candidates: &[usize],
) -> EngineResult<Value> {
    let ValidatedSubKind::ScalarColumn { col, .. } = &sq.kind else {
        return Err(EngineError::internal("DirectScalar without scalar-column kind"));
    };
    match candidates {
        [] => Ok(Value::Null),
        [only] => Ok(inner_batch.value(col, *only)?),
        _ => Err(crate::interpreter::cardinality_error(col, candidates.len())),
    }
}
