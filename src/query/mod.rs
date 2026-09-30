//! Query operators: expression AST and bitmap-aware evaluation.
//!
//! A query is a SQL-ish boolean predicate:
//!
//! ```text
//! age >= 18 AND (city = 'NY' OR name IS NOT NULL) AND NOT vip = false
//! ```
//!
//! Leaves are column comparisons or `IS [NOT] NULL`; internal nodes are the
//! three-valued `AND` / `OR` / `NOT`. Evaluation pins one table version, takes
//! its live domain, and every leaf produces a [`TritPartition`] on that exact
//! domain — so indexes combined in one expression are necessarily compatible.

pub mod parse;

pub use parse::parse_predicate;

use crate::batch::Scalar;
use crate::bitset::Bitmap;
use crate::dataset::Dataset;
use crate::error::Result;
use crate::index::CompareOp;
use crate::partition::TritPartition;

/// Boolean expression AST.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Expr {
    /// `column <op> literal`
    Compare {
        column: String,
        op: CompareOp,
        literal: Scalar,
    },
    /// `column IS NULL` / `column IS NOT NULL`
    IsNull { column: String, negated: bool },
    And(Box<Expr>, Box<Expr>),
    Or(Box<Expr>, Box<Expr>),
    Not(Box<Expr>),
}

impl Expr {
    /// Every column referenced by the expression.
    pub fn columns(&self) -> Vec<&str> {
        let mut out = Vec::new();
        self.collect(&mut out);
        out
    }

    fn collect<'a>(&'a self, out: &mut Vec<&'a str>) {
        match self {
            Expr::Compare { column, .. } | Expr::IsNull { column, .. } => out.push(column),
            Expr::And(a, b) | Expr::Or(a, b) => {
                a.collect(out);
                b.collect(out);
            }
            Expr::Not(a) => a.collect(out),
        }
    }
}

/// Outcome of evaluating a predicate at one version.
#[derive(Debug)]
pub struct QueryOutcome {
    pub version: u64,
    pub n: usize,
    pub live: usize,
    pub partition: TritPartition,
}

impl QueryOutcome {
    pub fn selected_rows(&self) -> Vec<usize> {
        self.partition.true_set().iter_ones().collect()
    }

    pub fn true_count(&self) -> usize {
        self.partition.true_set().count_ones()
    }

    pub fn false_count(&self) -> usize {
        self.partition.set(crate::trit::Trit::False).count_ones()
    }

    pub fn unknown_count(&self) -> usize {
        self.partition
            .set(crate::trit::Trit::Unknown)
            .count_ones()
    }

    pub fn deleted_count(&self) -> usize {
        self.n - self.live
    }
}

/// Evaluate `expr` against `dataset` at `version` (current version if `None`).
pub fn evaluate(dataset: &Dataset, expr: &Expr, version: Option<u64>) -> Result<QueryOutcome> {
    let (v, domain) = dataset.domain_at(version)?;
    let partition = eval_node(dataset, expr, &domain)?;
    Ok(QueryOutcome {
        version: v,
        n: dataset.row_count(),
        live: domain.count_ones(),
        partition,
    })
}

fn eval_node(dataset: &Dataset, expr: &Expr, domain: &Bitmap) -> Result<TritPartition> {
    match expr {
        Expr::Compare {
            column,
            op,
            literal,
        } => {
            let idx = dataset.index(column)?;
            idx.evaluate_compare(*op, literal, domain)
        }
        Expr::IsNull { column, negated } => {
            let idx = dataset.index(column)?;
            idx.evaluate_is_null(*negated, domain)
        }
        Expr::And(a, b) => {
            let pa = eval_node(dataset, a, domain)?;
            let pb = eval_node(dataset, b, domain)?;
            pa.and(&pb).map_err(Into::into)
        }
        Expr::Or(a, b) => {
            let pa = eval_node(dataset, a, domain)?;
            let pb = eval_node(dataset, b, domain)?;
            pa.or(&pb).map_err(Into::into)
        }
        Expr::Not(a) => Ok(eval_node(dataset, a, domain)?.not()),
    }
}
