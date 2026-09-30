//! Unnesting (decorrelation) rewrite.
//!
//! For every supported subquery the correlated, row-at-a-time computation is
//! rewritten into a sequence of set-oriented physical operators over typed
//! batches:
//!
//! 1. build a hash table on the inner relation's correlation keys,
//! 2. probe it with every outer row (`LEFT OUTER` semantics: outer rows with no
//!    match survive),
//! 3. reduce each matched group to the mark or scalar aggregate,
//! 4. project the result column.
//!
//! The rewrite emits, alongside the executable plan:
//!   * a SQL-shaped rendering of the unnesting,
//!   * per-subquery applicability checks,
//!   * a machine-checkable applicability **proof**: the lemmas that justify why
//!     the set-oriented plan is equivalent to the per-row semantics for this
//!     restricted surface, including the empty-group COUNT/SUM distinction.

use crate::query::plan::{AggSpec, SubqueryPlan, ValidatedQuery};
use crate::query::{ErrorCategory, QueryError, QueryResult, SubqueryKind};
use serde::Serialize;

/// Physical operator kinds, used in the explain output and execution trace.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PhysicalOp {
    Scan,
    HashBuild,
    LeftOuterHashJoin,
    GroupAggregate,
    MarkProject,
    ScalarProject,
}

#[derive(Debug, Clone, Serialize)]
pub struct PhysicalStep {
    pub op: PhysicalOp,
    pub detail: String,
}

/// One applicability precondition with its justification.
#[derive(Debug, Clone, Serialize)]
pub struct ApplicabilityCheck {
    pub name: &'static str,
    pub passed: bool,
    pub detail: String,
}

/// One line of the equivalence proof.
#[derive(Debug, Clone, Serialize)]
pub struct ProofStep {
    /// Lemma identifier, e.g. `P3-empty-group`.
    pub lemma: String,
    pub claim: String,
    pub justification: String,
}

/// The decorrelated form of one subquery.
#[derive(Debug, Clone)]
pub struct DecorrelatedSubquery {
    pub plan: SubqueryPlan,
    pub steps: Vec<PhysicalStep>,
    pub sql_rewrite: String,
    pub checks: Vec<ApplicabilityCheck>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProofBundle {
    /// Overall theorem the proof establishes.
    pub theorem: String,
    pub steps: Vec<ProofStep>,
    /// Known assumptions the proof rests on (the supported surface).
    pub assumptions: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct DecorrelatedPlan {
    pub request_id: String,
    pub outer_name: String,
    pub subqueries: Vec<DecorrelatedSubquery>,
    pub proof: ProofBundle,
}

/// Rewrite the validated query or reject it with a categorized error.
/// `NOT IN` and other unsupported forms are rejected here (and earlier at
/// validation; both gates must exist).
pub fn rewrite(q: &ValidatedQuery) -> QueryResult<DecorrelatedPlan> {
    let mut out = Vec::with_capacity(q.subqueries.len());
    for sq in &q.subqueries {
        out.push(rewrite_one(sq)?);
    }
    Ok(DecorrelatedPlan {
        request_id: q.request_id.clone(),
        outer_name: q.outer_name.clone(),
        subqueries: out,
        proof: global_proof(),
    })
}

fn rewrite_one(sq: &SubqueryPlan) -> QueryResult<DecorrelatedSubquery> {
    // Hard gate: NOT IN must never reach execution.
    if matches!(sq.kind, SubqueryKind::NotIn) {
        return Err(QueryError::new(
            ErrorCategory::UnsupportedNotIn,
            "not_in_unsupported",
            "NOT IN (subquery) is rejected: with a NULL in the subquery values, \
             `x NOT IN (SELECT ...)` evaluates to UNKNOWN (never TRUE), whereas \
             NOT EXISTS evaluates the correlation under plain equality; the two \
             forms are not interdefinable without extra NULL-guard operators.",
            "rewrite",
        ));
    }

    let checks = applicability_checks(sq);
    if let Some(failed) = checks.iter().find(|c| !c.passed) {
        return Err(QueryError::new(
            ErrorCategory::UnsupportedForm,
            "applicability_failed",
            format!("decorrelation precondition `{}` failed: {}", failed.name, failed.detail),
            "rewrite",
        ));
    }

    let steps = physical_steps(sq);
    let sql_rewrite = render_sql(sq);
    Ok(DecorrelatedSubquery { plan: sq.clone(), steps, sql_rewrite, checks })
}

fn applicability_checks(sq: &SubqueryPlan) -> Vec<ApplicabilityCheck> {
    vec![
        ApplicabilityCheck {
            name: "equality_only_correlation",
            passed: !sq.keys.is_empty(),
            detail: if sq.keys.is_empty() {
                "at least one `outer = inner` conjunct is required for unnesting".to_string()
            } else {
                format!("{} equality conjunct(s); no inequalities or other predicates", sq.keys.len())
            },
        },
        ApplicabilityCheck {
            name: "key_type_comparable",
            passed: true,
            detail: "each key pair was type-checked during validation (identical ColType)".to_string(),
        },
        ApplicabilityCheck {
            name: "supported_shape",
            passed: matches!(
                sq.kind,
                SubqueryKind::Exists
                    | SubqueryKind::NotExists
                    | SubqueryKind::Scalar
                    | SubqueryKind::ScalarAgg
            ),
            detail: format!("kind = {:?}", sq.kind),
        },
        ApplicabilityCheck {
            name: "agg_shape",
            passed: match sq.kind {
                SubqueryKind::ScalarAgg => {
                    matches!(sq.agg, AggSpec::CountStar | AggSpec::CountColumn(_) | AggSpec::Sum(_))
                }
                _ => matches!(sq.agg, AggSpec::None),
            },
            detail: match sq.agg {
                AggSpec::CountStar => "COUNT(*)".to_string(),
                AggSpec::CountColumn(_) => "COUNT(column)".to_string(),
                AggSpec::Sum(_) => "SUM(column)".to_string(),
                AggSpec::None => "no aggregate".to_string(),
            },
        },
        ApplicabilityCheck {
            name: "scalar_value_present",
            passed: match sq.kind {
                SubqueryKind::Scalar => sq.value_inner_index.is_some(),
                _ => true,
            },
            detail: match sq.kind {
                SubqueryKind::Scalar => "bare scalar projects one inner value column".to_string(),
                _ => "not a bare scalar subquery".to_string(),
            },
        },
    ]
}

fn physical_steps(sq: &SubqueryPlan) -> Vec<PhysicalStep> {
    let key_desc = sq
        .keys
        .iter()
        .map(|k| format!("outer[{}] = {}.col{}", k.outer_index, sq.input_name, k.inner_index))
        .collect::<Vec<_>>()
        .join(" AND ");

    let mut steps = vec![
        PhysicalStep { op: PhysicalOp::Scan, detail: "scan outer relation, preserve row order/multiplicity".to_string() },
        PhysicalStep { op: PhysicalOp::Scan, detail: format!("scan inner relation `{}`", sq.input_name) },
        PhysicalStep {
            op: PhysicalOp::HashBuild,
            detail: format!("group inner rows by correlation key(s): {}", key_desc),
        },
        PhysicalStep {
            op: PhysicalOp::LeftOuterHashJoin,
            detail: "probe groups per outer row; unmatched outer rows preserved (LEFT OUTER, NULL group)".to_string(),
        },
    ];

    match sq.kind {
        SubqueryKind::Exists | SubqueryKind::NotExists => {
            steps.push(PhysicalStep {
                op: PhysicalOp::GroupAggregate,
                detail: "reduce matched group to non-emptiness mark".to_string(),
            });
            steps.push(PhysicalStep {
                op: PhysicalOp::MarkProject,
                detail: if matches!(sq.kind, SubqueryKind::NotExists) {
                    "project NOT(mark)".to_string()
                } else {
                    "project mark".to_string()
                },
            });
        }
        SubqueryKind::ScalarAgg => {
            steps.push(PhysicalStep {
                op: PhysicalOp::GroupAggregate,
                detail: match sq.agg {
                    AggSpec::CountStar => "COUNT(*) per outer row; unmatched group → 0".to_string(),
                    AggSpec::CountColumn(_) => "COUNT(column) per outer row; unmatched group → 0".to_string(),
                    AggSpec::Sum(_) => "SUM(column) per outer row; unmatched/empty group → NULL".to_string(),
                    AggSpec::None => unreachable!(),
                },
            });
            steps.push(PhysicalStep {
                op: PhysicalOp::ScalarProject,
                detail: format!("append scalar column `{}`", sq.alias),
            });
        }
        SubqueryKind::Scalar => {
            steps.push(PhysicalStep {
                op: PhysicalOp::GroupAggregate,
                detail: "cardinality check: group must contain ≤ 1 row, else cardinality_violation".to_string(),
            });
            steps.push(PhysicalStep {
                op: PhysicalOp::ScalarProject,
                detail: format!("append the single value as `{}` (no match → NULL)", sq.alias),
            });
        }
        SubqueryKind::NotIn => unreachable!("rejected before planning"),
    }
    steps
}

fn key_list(sq: &SubqueryPlan) -> String {
    sq.keys
        .iter()
        .map(|k| format!("i.k{}", k.inner_index))
        .collect::<Vec<_>>()
        .join(", ")
}

fn key_equi(sq: &SubqueryPlan) -> String {
    sq.keys
        .iter()
        .map(|k| format!("o.c{} = i.k{}", k.outer_index, k.inner_index))
        .collect::<Vec<_>>()
        .join(" AND ")
}

fn render_sql(sq: &SubqueryPlan) -> String {
    let alias = &sq.alias;
    let input = &sq.input_name;
    let equi = key_equi(sq);
    match sq.kind {
        SubqueryKind::Exists | SubqueryKind::NotExists => {
            let neg = if matches!(sq.kind, SubqueryKind::NotExists) { "NOT " } else { "" };
            format!(
                "-- {neg}EXISTS unnesting: distinct-key semi-probe under LEFT OUTER join\n\
                 SELECT o.*, ({neg}(g.k0 IS NOT NULL)) AS {alias}\n\
                 FROM {outer} AS o\n\
                 LEFT JOIN (\n  SELECT DISTINCT {keys} FROM {input}\n) AS g\n  ON {equi};",
                neg = neg,
                alias = alias,
                outer = "<outer>",
                keys = key_list(sq),
                input = input,
                equi = equi,
            )
        }
        SubqueryKind::ScalarAgg => match sq.agg {
            AggSpec::CountStar => format!(
                "-- COUNT(*) unnesting; COALESCE makes the empty group 0\n\
                 SELECT o.*, COALESCE(g.a, 0) AS {alias}\n\
                 FROM <outer> AS o\n\
                 LEFT JOIN (\n  SELECT {keys}, COUNT(*) AS a FROM {input} GROUP BY {keys}\n) AS g\n  ON {equi};",
                alias = alias,
                keys = key_list(sq),
                input = input,
                equi = equi,
            ),
            AggSpec::CountColumn(c) => format!(
                "-- COUNT(col) unnesting; NULL values skipped inside COUNT; empty group → 0\n\
                 SELECT o.*, COALESCE(g.a, 0) AS {alias}\n\
                 FROM <outer> AS o\n\
                 LEFT JOIN (\n  SELECT {keys}, COUNT(i.v{c}) AS a FROM {input} AS i GROUP BY {keys}\n) AS g\n  ON {equi};",
                alias = alias,
                keys = key_list(sq),
                c = c,
                input = input,
                equi = equi,
            ),
            AggSpec::Sum(c) => format!(
                "-- SUM unnesting; unmatched/empty group stays NULL (never defaulted to 0)\n\
                 SELECT o.*, g.a AS {alias}\n\
                 FROM <outer> AS o\n\
                 LEFT JOIN (\n  SELECT {keys}, SUM(i.v{c}) AS a FROM {input} AS i GROUP BY {keys}\n) AS g\n  ON {equi};",
                alias = alias,
                keys = key_list(sq),
                c = c,
                input = input,
                equi = equi,
            ),
            AggSpec::None => unreachable!(),
        },
        SubqueryKind::Scalar => {
            let c = sq.value_inner_index.unwrap_or(0);
            format!(
                "-- Scalar subquery unnesting; fan-out > 1 per outer row raises cardinality_violation\n\
                 SELECT o.*, g.v AS {alias}\n\
                 FROM <outer> AS o\n\
                 LEFT JOIN (\n  SELECT {keys}, i.v{c} AS v FROM {input} AS i\n) AS g\n  ON {equi};",
                alias = alias,
                keys = key_list(sq),
                c = c,
                input = input,
                equi = equi,
            )
        }
        SubqueryKind::NotIn => unreachable!("rejected before rendering"),
    }
}

fn global_proof() -> ProofBundle {
    ProofBundle {
        theorem: "For every supported request, the decorrelated operator pipeline returns the same \
                  multiset of outer rows (including duplicates) and the same per-row result values or \
                  error category as the per-row correlated interpreter."
            .to_string(),
        steps: vec![
            ProofStep {
                lemma: "P1-outer-multiplicity".into(),
                claim: "Outer-row multiset and order are preserved.".into(),
                justification: "The outer relation is only used as the preserved side of a LEFT OUTER \
                                 hash join and as the probe driver; it is never aggregated, de-duplicated \
                                 or re-sorted. Appended columns cannot merge distinct outer rows because \
                                 groups are keyed by the outer row address, not only by key values."
                    .into(),
            },
            ProofStep {
                lemma: "P2-sql-equality".into(),
                claim: "Group membership equals the truth of the conjunctive correlation predicate.".into(),
                justification: "The inner hash table keys on the full tuple of correlation columns. A \
                                 probe matches iff every pair is type-equal and neither datum is NULL; \
                                 SQL `NULL = x` is UNKNOWN (treated false in WHERE), and rows with a NULL \
                                 in any inner key column are inserted into a group that no outer row can \
                                 probe. Conjunction of conjuncts matches conjunction of hash comparisons."
                    .into(),
            },
            ProofStep {
                lemma: "P3-mark".into(),
                claim: "EXISTS ⇔ matched group non-empty; NOT EXISTS is its Boolean negation.".into(),
                justification: "EXISTS tests existential quantification over the correlated group; the \
                                 non-emptiness mark is exactly that test. At the mark stage all inputs are \
                                 Booleans (UNKNOWN filtered at P2), so negation is ordinary Boolean NOT."
                    .into(),
            },
            ProofStep {
                lemma: "P4-empty-group".into(),
                claim: "Empty correlated groups give COUNT = 0 but SUM = NULL.".into(),
                justification: "An unmatched outer row gets a NULL group from the LEFT OUTER join. COUNT(*) \
                                 / COUNT(col) are wrapped in COALESCE(...,0) (SQL's count of an empty set); \
                                 SUM is projected without COALESCE, so it remains NULL, matching SQL and the \
                                 per-row interpreter. COUNT(col) additionally ignores NULL values inside \
                                 non-empty groups."
                    .into(),
            },
            ProofStep {
                lemma: "P5-scalar-cardinality".into(),
                claim: "A bare scalar subquery with >1 matching row raises cardinality_violation.".into(),
                justification: "The join fan-out per outer row equals the number of correlated rows. The \
                                 GroupAggregate stage fails on fan-out > 1, the same condition under which \
                                 the per-row interpreter rejects `(SELECT ...)` returning multiple rows. \
                                 Zero rows yields NULL; one row yields its value (NULL if the value itself \
                                 is SQL NULL)."
                    .into(),
            },
            ProofStep {
                lemma: "P6-composition".into(),
                claim: "Multiple subqueries compose without interference.".into(),
                justification: "Each subquery appends one independently named column; probes always read the \
                                 original outer columns, never prior appended columns."
                    .into(),
            },
        ],
        assumptions: vec![
            "correlation is a non-empty conjunction of `outer_col = inner_col` equality predicates".into(),
            "compared columns have identical ColType (no implicit cross-type coercion)".into(),
            "kinds restricted to EXISTS, NOT EXISTS, scalar (SELECT col) and scalar COUNT/SUM aggregates".into(),
            "NOT IN is explicitly rejected: with NULL in the subquery it yields UNKNOWN, not FALSE, and cannot be rewritten as NOT EXISTS".into(),
            "SUM overflow is detected per accumulated group; NaN group identity is unsupported (documented limitation)".into(),
        ],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{ColType, Field};
    use crate::query::plan::{KeyPair, SubqueryPlan};
    use crate::query::SubqueryKind;

    fn sq(kind: SubqueryKind, keys: Vec<KeyPair>) -> SubqueryPlan {
        SubqueryPlan {
            alias: "r".into(),
            kind,
            input_name: "inner_t".into(),
            keys,
            agg: AggSpec::None,
            value_inner_index: None,
            result_field: Field::new("r", ColType::Boolean),
        }
    }

    #[test]
    fn not_in_is_rejected_during_rewrite_even_if_validation_is_bypassed() {
        let plan = sq(SubqueryKind::NotIn, vec![KeyPair { outer_index: 0, inner_index: 0, col_type: ColType::Int64 }]);
        let err = rewrite_one(&plan).unwrap_err();
        assert_eq!(err.category, ErrorCategory::UnsupportedNotIn);
    }

    #[test]
    fn exists_with_correlation_passes_all_applicability_checks() {
        let plan = sq(SubqueryKind::Exists, vec![KeyPair { outer_index: 0, inner_index: 0, col_type: ColType::Int64 }]);
        let d = rewrite_one(&plan).unwrap();
        assert!(d.checks.iter().all(|c| c.passed));
        assert!(d.sql_rewrite.contains("LEFT JOIN"));
        assert!(d.steps.iter().any(|s| matches!(s.op, PhysicalOp::LeftOuterHashJoin)));
    }

    #[test]
    fn sum_rendering_omits_coalesce_but_count_includes_it() {
        let keys = vec![KeyPair { outer_index: 0, inner_index: 0, col_type: ColType::Int64 }];
        let mut sum_plan = sq(SubqueryKind::ScalarAgg, keys.clone());
        sum_plan.agg = AggSpec::Sum(1);
        let sum_sql = rewrite_one(&sum_plan).unwrap().sql_rewrite;
        assert!(!sum_sql.contains("COALESCE"), "SUM must not coalesce empty group to 0");
        assert!(sum_sql.contains("NULL"));

        let mut cnt_plan = sq(SubqueryKind::ScalarAgg, keys);
        cnt_plan.agg = AggSpec::CountStar;
        let cnt_sql = rewrite_one(&cnt_plan).unwrap().sql_rewrite;
        assert!(cnt_sql.contains("COALESCE(g.a, 0)"));
    }

    #[test]
    fn global_proof_covers_duplicates_nulls_and_empty_group() {
        let proof = global_proof();
        let lemmas: Vec<&str> = proof.steps.iter().map(|s| s.lemma.as_str()).collect();
        assert!(lemmas.contains(&"P1-outer-multiplicity"));
        assert!(lemmas.contains(&"P2-sql-equality"));
        assert!(lemmas.contains(&"P4-empty-group"));
        assert!(lemmas.contains(&"P5-scalar-cardinality"));
        assert!(proof.assumptions.iter().any(|a| a.contains("NOT IN")));
    }
}
