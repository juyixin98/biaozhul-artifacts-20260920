//! Query operators: evaluate a [`Predicate`] into a [`TriPartition`].
//!
//! Every leaf is answered by a bitmap index (a per-column [`ValueIndex`], or
//! a [`CompositeIndex`] for equality conjunctions under the `combined`/`auto`
//! strategies); logical nodes are pure bitmap algebra in
//! [`crate::trivalent`]. No operator ever falls back to per-row scans inside
//! the engine — the per-row oracle lives in the independent test suite.
//!
//! Strategies:
//! * `single`   — per-column value indexes only.
//! * `combined` — require a composite index for equality conjunctions;
//!   if none serves the predicate, fail with
//!   [`ErrorKind::CombinedIndexUnavailable`] (never silently downgrade).
//! * `auto`     — prefer a composite index, fall back to `single`.
//!
//! Every evaluation emits a step log: each entry carries the run id, the
//! predicate node, the strategy path taken and the resulting T/F/U counts,
//! so a test log can always be traced back to its input and run identity.

use serde::Serialize;

use crate::bits::WordBitmap;
use crate::error::{ErrorKind, IndexError, Result};
use crate::predicate::Predicate;
use crate::table::Table;
use crate::trivalent::{TriCounts, TriPartition};

/// Execution strategy for a query.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strategy {
    Single,
    Combined,
    Auto,
}

impl Strategy {
    pub fn parse(raw: &str) -> Result<Self> {
        match raw {
            "single" => Ok(Strategy::Single),
            "combined" => Ok(Strategy::Combined),
            "auto" => Ok(Strategy::Auto),
            other => Err(IndexError::new(
                ErrorKind::UnknownStrategy,
                format!("unknown strategy {other:?}; expected single|combined|auto"),
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Strategy::Single => "single",
            Strategy::Combined => "combined",
            Strategy::Auto => "auto",
        }
    }
}

/// One recorded evaluation step.
#[derive(Debug, Clone, Serialize)]
pub struct EvalStep {
    pub seq: usize,
    pub run_id: String,
    /// Human-readable predicate node, e.g. `grp = 3` or `(a AND b)`.
    pub node: String,
    /// Which operator/path produced this step, e.g. `value_index.compare`.
    pub op: String,
    pub true_rows: usize,
    pub false_rows: usize,
    pub unknown_rows: usize,
}

/// Result of one query: the partition plus its full step log.
#[derive(Debug)]
pub struct EvalOutcome {
    pub partition: TriPartition,
    pub steps: Vec<EvalStep>,
    /// The strategy the caller asked for (per-node paths are in the steps).
    pub requested_strategy: Strategy,
}

struct Ctx<'a> {
    table: &'a Table,
    universe: &'a WordBitmap,
    run_id: &'a str,
    strategy: Strategy,
    steps: Vec<EvalStep>,
}

impl Ctx<'_> {
    fn record(&mut self, node: &Predicate, op: &str, counts: TriCounts) {
        let step = EvalStep {
            seq: self.steps.len(),
            run_id: self.run_id.to_string(),
            node: node.describe(),
            op: op.to_string(),
            true_rows: counts.true_rows,
            false_rows: counts.false_rows,
            unknown_rows: counts.unknown_rows,
        };
        tracing::info!(
            run_id = %self.run_id,
            seq = step.seq,
            node = %step.node,
            op = %step.op,
            true_rows = step.true_rows,
            false_rows = step.false_rows,
            unknown_rows = step.unknown_rows,
            "eval step"
        );
        self.steps.push(step);
    }
}

/// Evaluate `pred` over the live-row universe of `table` at `version`.
pub fn evaluate(
    table: &Table,
    pred: &Predicate,
    strategy: Strategy,
    version: u64,
    run_id: &str,
) -> Result<EvalOutcome> {
    let universe = table.universe_at(version)?;
    let mut ctx = Ctx { table, universe: &universe, run_id, strategy, steps: Vec::new() };
    let partition = eval_node(&mut ctx, pred)?;
    partition.validate()?;
    Ok(EvalOutcome { partition, steps: ctx.steps, requested_strategy: strategy })
}

fn eval_node(ctx: &mut Ctx, pred: &Predicate) -> Result<TriPartition> {
    // Composite-index path for equality conjunctions (combined/auto).
    if ctx.strategy != Strategy::Single {
        match pred.as_eq_conjunction() {
            Some(pairs) if pairs.len() >= 2 => {
                let cols: Vec<&str> = pairs.iter().map(|(c, _)| *c).collect();
                match ctx.table.composite_for(&cols) {
                    Some(index) => {
                        let values: Vec<crate::scalar::Scalar> =
                            pairs.iter().map(|(_, v)| (*v).clone()).collect();
                        let (t, f, u) = index.lookup(&values)?;
                        // The index answers over the physical row space;
                        // intersect with the versioned universe like any leaf.
                        let part = TriPartition::new(ctx.universe.clone(), t, f)?;
                        // Cross-check: the index's UNKNOWN set (NULL key
                        // components) must match the derived one.
                        let index_unknown = u.intersect(ctx.universe)?;
                        if part.unknown_set() != index_unknown {
                            return Err(IndexError::new(
                                ErrorKind::InvalidBitmap,
                                format!(
                                    "composite index on {:?}: derived UNKNOWN disagrees with index UNKNOWN",
                                    index.columns()
                                ),
                            ));
                        }
                        ctx.record(pred, "composite_index.lookup", part.counts());
                        return Ok(part);
                    }
                    None if ctx.strategy == Strategy::Combined => {
                        return Err(IndexError::new(
                            ErrorKind::CombinedIndexUnavailable,
                            format!(
                                "strategy=combined but no composite index serves columns {cols:?} on table {:?}",
                                ctx.table.id()
                            ),
                        ));
                    }
                    None => { /* auto: fall through to per-column indexes */ }
                }
            }
            _ if ctx.strategy == Strategy::Combined => {
                return Err(IndexError::new(
                    ErrorKind::CombinedIndexUnavailable,
                    format!(
                        "strategy=combined requires a conjunction of equalities on 2+ columns; got {}",
                        pred.describe()
                    ),
                ));
            }
            _ => {}
        }
    }

    match pred {
        Predicate::Compare { column, op, value } => {
            let index = ctx.table.value_index(column)?;
            let (t, f) = index.compare(*op, value)?;
            let part = TriPartition::new(ctx.universe.clone(), t, f)?;
            ctx.record(pred, "value_index.compare", part.counts());
            Ok(part)
        }
        Predicate::IsNull { column } => {
            let index = ctx.table.value_index(column)?;
            let t = index.nulls().clone();
            let f = index.present()?;
            let part = TriPartition::new(ctx.universe.clone(), t, f)?;
            ctx.record(pred, "value_index.is_null", part.counts());
            Ok(part)
        }
        Predicate::And(children) => {
            let mut iter = children.iter();
            let mut acc = eval_node(ctx, iter.next().expect("non-empty by parse"))?;
            for child in iter {
                let rhs = eval_node(ctx, child)?;
                acc = acc.and(&rhs)?;
            }
            ctx.record(pred, "kleene_and", acc.counts());
            Ok(acc)
        }
        Predicate::Or(children) => {
            let mut iter = children.iter();
            let mut acc = eval_node(ctx, iter.next().expect("non-empty by parse"))?;
            for child in iter {
                let rhs = eval_node(ctx, child)?;
                acc = acc.or(&rhs)?;
            }
            ctx.record(pred, "kleene_or", acc.counts());
            Ok(acc)
        }
        Predicate::Not(child) => {
            let inner = eval_node(ctx, child)?;
            let out = inner.not()?;
            ctx.record(pred, "kleene_not", out.counts());
            Ok(out)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::{BatchSchema, ColumnSchema, ColumnType, TypedBatch};
    use crate::scalar::{CompareOp, Scalar};
    use crate::trivalent::TriState;
    use std::sync::Arc;

    fn table() -> Table {
        let schema = Arc::new(
            BatchSchema::new(vec![
                ColumnSchema { name: "a".into(), ty: ColumnType::Int },
                ColumnSchema { name: "b".into(), ty: ColumnType::Int },
            ])
            .unwrap(),
        );
        let mut t = Table::new("t", schema.clone());
        let batch = TypedBatch::from_columns(
            schema,
            vec![
                vec![Some(Scalar::Int(1)), None, Some(Scalar::Int(2)), Some(Scalar::Int(1))],
                vec![Some(Scalar::Int(9)), Some(Scalar::Int(9)), None, Some(Scalar::Int(8))],
            ],
        )
        .unwrap();
        t.append(batch).unwrap();
        t
    }

    #[test]
    fn leaf_and_logic_nodes_are_bitmap_algebra() {
        let t = table();
        // a = 1 AND b = 9 -> row0 T, row1 U (a NULL), row2 F (b NULL), row3 F
        let p = Predicate::and(vec![
            Predicate::compare("a", CompareOp::Eq, Scalar::Int(1)),
            Predicate::compare("b", CompareOp::Eq, Scalar::Int(9)),
        ]);
        let out = evaluate(&t, &p, Strategy::Single, 0, "run-1").unwrap();
        assert_eq!(out.partition.classify(0), Some(TriState::True));
        assert_eq!(out.partition.classify(1), Some(TriState::Unknown));
        assert_eq!(out.partition.classify(2), Some(TriState::False));
        assert_eq!(out.partition.classify(3), Some(TriState::False));
        // Steps: two leaves + one AND node, all tagged with the run id.
        assert_eq!(out.steps.len(), 3);
        assert!(out.steps.iter().all(|s| s.run_id == "run-1"));
        assert_eq!(out.steps[2].op, "kleene_and");
    }

    #[test]
    fn combined_strategy_requires_and_uses_composite_index() {
        let mut t = table();
        let p = Predicate::and(vec![
            Predicate::compare("a", CompareOp::Eq, Scalar::Int(1)),
            Predicate::compare("b", CompareOp::Eq, Scalar::Int(9)),
        ]);
        let err = evaluate(&t, &p, Strategy::Combined, 0, "run-2").unwrap_err();
        assert_eq!(err.kind, ErrorKind::CombinedIndexUnavailable);

        t.register_composite(&["a".to_string(), "b".to_string()]).unwrap();
        let out = evaluate(&t, &p, Strategy::Combined, 0, "run-3").unwrap();
        assert_eq!(out.partition.classify(0), Some(TriState::True));
        assert_eq!(out.partition.classify(1), Some(TriState::Unknown), "NULL key component");
        assert_eq!(out.partition.classify(3), Some(TriState::False));
        assert!(out.steps.iter().any(|s| s.op == "composite_index.lookup"));

        // Auto falls back to value indexes when no composite index matches.
        let q = Predicate::compare("a", CompareOp::Eq, Scalar::Int(1));
        let out = evaluate(&t, &q, Strategy::Auto, 0, "run-4").unwrap();
        assert!(out.steps.iter().any(|s| s.op == "value_index.compare"));
    }

    #[test]
    fn combined_rejects_non_conjunction() {
        let mut t = table();
        t.register_composite(&["a".to_string(), "b".to_string()]).unwrap();
        let p = Predicate::or(vec![
            Predicate::compare("a", CompareOp::Eq, Scalar::Int(1)),
            Predicate::compare("b", CompareOp::Eq, Scalar::Int(9)),
        ]);
        let err = evaluate(&t, &p, Strategy::Combined, 0, "run-5").unwrap_err();
        assert_eq!(err.kind, ErrorKind::CombinedIndexUnavailable);
    }

    #[test]
    fn unknown_strategy_is_typed() {
        assert_eq!(Strategy::parse("fast").unwrap_err().kind, ErrorKind::UnknownStrategy);
        assert_eq!(Strategy::parse("single").unwrap(), Strategy::Single);
    }
}
