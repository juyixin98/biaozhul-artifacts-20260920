//! Local synthetic fixtures used by tests, the CLI demo and `POST
//! /fixtures/load`.
//!
//! All data is generated here from a single, auditable definition; no
//! external service or real account is ever involved. The same definition
//! can be exported to JSON files under `fixtures/` with the CLI
//! `export-fixtures` command so the HTTP examples run from static files.

use crate::query::batch::{
    batch_from_rows, ColumnType, IngestReport, NullPolicy,
};
use crate::query::value::Scalar;

/// One relation fixture, already shaped like a validated ingestion report.
#[derive(Clone)]
pub struct RelationFixture {
    pub name: String,
    pub report: IngestReport,
}

/// A named verification scenario (one or more relations + the queries).
pub struct Scenario {
    pub name: &'static str,
    pub description: &'static str,
    pub relations: Vec<RelationFixture>,
    /// JSON-ready verification query.
    pub triangle_query: Option<serde_json::Value>,
}

fn report(name_cols: Vec<(String, ColumnType)>, rows: Vec<Vec<Scalar>>) -> IngestReport {
    let batch = batch_from_rows(name_cols, rows);
    IngestReport {
        null_policy: NullPolicy::Reject,
        dropped_null_rows: 0,
        replace: false,
        batch,
    }
}

fn i(v: i64) -> Scalar {
    Scalar::Int(v)
}

fn cols(names: &[&str]) -> Vec<(String, ColumnType)> {
    names
        .iter()
        .map(|n| ((*n).to_string(), ColumnType::Int64))
        .collect()
}

/// Triangle with explicit duplicate-row multiplicity.
fn triangle() -> Scenario {
    Scenario {
        name: "triangle",
        description: "R(a,b), S(b,c), T(a,c) triangle; R(1,2) appears twice",
        relations: vec![
            RelationFixture {
                name: "R".into(),
                report: report(
                    cols(&["a", "b"]),
                    vec![
                        vec![i(1), i(2)],
                        vec![i(1), i(2)],
                        vec![i(2), i(3)],
                        vec![i(3), i(4)],
                    ],
                ),
            },
            RelationFixture {
                name: "S".into(),
                report: report(
                    cols(&["b", "c"]),
                    vec![vec![i(2), i(3)], vec![i(3), i(4)], vec![i(4), i(5)]],
                ),
            },
            RelationFixture {
                name: "T".into(),
                report: report(
                    cols(&["a", "c"]),
                    vec![vec![i(1), i(3)], vec![i(2), i(4)], vec![i(3), i(5)]],
                ),
            },
        ],
        triangle_query: Some(serde_json::json!({
            "query": {
                "atoms": [
                    {"relation": "R", "vars": ["a", "b"]},
                    {"relation": "S", "vars": ["b", "c"]},
                    {"relation": "T", "vars": ["a", "c"]}
                ]
            },
            "resume_row_limit": 1
        })),
    }
}

/// Highly skewed intersection: one dominant key plus a small minority.
fn skew() -> Scenario {
    let mut a_rows: Vec<Vec<Scalar>> = Vec::new();
    let mut b_rows: Vec<Vec<Scalar>> = Vec::new();
    for k in 0..2000 {
        a_rows.push(vec![i(1), i(k)]);
    }
    a_rows.push(vec![i(2), i(9001)]);
    b_rows.push(vec![i(1), i(1999)]);
    b_rows.push(vec![i(2), i(9001)]);
    Scenario {
        name: "skew",
        description: "A(grp,k) has 2000 rows under grp=1 then one under grp=2; B intersects sparsely",
        relations: vec![
            RelationFixture {
                name: "Askew".into(),
                report: report(cols(&["grp", "k"]), a_rows),
            },
            RelationFixture {
                name: "Bskew".into(),
                report: report(cols(&["grp", "k"]), b_rows),
            },
        ],
        triangle_query: Some(serde_json::json!({
            "query": {"atoms": [
                {"relation": "Askew", "vars": ["g", "k"]},
                {"relation": "Bskew", "vars": ["g", "k"]}
            ]}
        })),
    }
}

/// Sparse intersection over dense domains: one far-away shared key.
fn sparse() -> Scenario {
    let dense: Vec<Vec<Scalar>> = (0..10_000).map(|x| vec![i(x)]).collect();
    Scenario {
        name: "sparse",
        description: "Dense(x)=0..9999 intersect Needle(x) at exactly x=9001",
        relations: vec![
            RelationFixture {
                name: "Dense".into(),
                report: report(cols(&["x"]), dense),
            },
            RelationFixture {
                name: "Needle".into(),
                report: report(cols(&["x"]), vec![vec![i(9001)]]),
            },
        ],
        triangle_query: Some(serde_json::json!({
            "query": {"atoms": [
                {"relation": "Dense", "vars": ["x"]},
                {"relation": "Needle", "vars": ["x"]}
            ]}
        })),
    }
}

/// Restricted multi-way natural join demonstrating the absence of an
/// intermediate Cartesian product.
///
/// Tables:
/// - `K1(k1)`, `K2(k2)`, `K3(k3)`: each contains two keys;
/// - `Link(k1,k2,k3,k4)`: a sparse connecting relation with ONE row.
///
/// A naive nested-loop that joins the three independent unary tables first
/// materializes `|K1|*|K2|*|K3| = 8` partial rows before it ever sees Link;
/// with larger domains this is the classic intermediate explosion. The
/// Leapfrog engine starts the first shared variable on Link and never builds
/// that intermediate. `explode` bumps the domains to 400 keys (64M naive
/// partials, past the oracle cap) while the engine still returns instantly.
fn chain(explode: bool) -> Scenario {
    let n: i64 = if explode { 400 } else { 2 };
    let k1: Vec<Vec<Scalar>> = (0..n).map(|x| vec![i(x)]).collect();
    let k2: Vec<Vec<Scalar>> = (0..n).map(|x| vec![i(x + 10_000)]).collect();
    let k3: Vec<Vec<Scalar>> = (0..n).map(|x| vec![i(x + 20_000)]).collect();
    let shared_k1 = n - 1;
    let shared_k2 = 10_000 + n - 1;
    let shared_k3 = 20_000 + n - 1;
    let link = vec![vec![i(shared_k1), i(shared_k2), i(shared_k3), i(42)]];
    let (name, desc) = if explode {
        (
            "chain_explode",
            "400-key domains: naive plan materializes 64M partials (over cap), engine returns 1 row",
        )
    } else {
        (
            "chain",
            "2-key domains restricted 4-way natural join through one sparse Link row",
        )
    };
    Scenario {
        name,
        description: desc,
        relations: vec![
            RelationFixture {
                name: "K1".into(),
                report: report(cols(&["k1"]), k1),
            },
            RelationFixture {
                name: "K2".into(),
                report: report(cols(&["k2"]), k2),
            },
            RelationFixture {
                name: "K3".into(),
                report: report(cols(&["k3"]), k3),
            },
            RelationFixture {
                name: "Link".into(),
                report: report(cols(&["k1", "k2", "k3", "k4"]), link),
            },
        ],
        triangle_query: Some(serde_json::json!({
            "query": {"atoms": [
                {"relation": "K1", "vars": ["k1"]},
                {"relation": "K2", "vars": ["k2"]},
                {"relation": "K3", "vars": ["k3"]},
                {"relation": "Link", "vars": ["k1", "k2", "k3", "k4"]}
            ]}
        })),
    }
}

pub fn all_scenarios() -> Vec<&'static str> {
    vec!["triangle", "skew", "sparse", "chain", "chain_explode"]
}

pub fn scenario(name: &str) -> Option<Scenario> {
    Some(match name {
        "triangle" => triangle(),
        "skew" => skew(),
        "sparse" => sparse(),
        "chain" => chain(false),
        "chain_explode" => chain(true),
        _ => return None,
    })
}

/// JSON request body for `POST /relations/:name` for a relation fixture.
pub fn load_request_json(fixture: &RelationFixture) -> serde_json::Value {
    let columns: Vec<serde_json::Value> = fixture
        .report
        .batch
        .columns
        .iter()
        .map(|(name, t)| serde_json::json!({"name": name, "type": t.as_str()}))
        .collect();
    serde_json::json!({
        "null_policy": "reject",
        "replace": true,
        "columns": columns,
        "rows": fixture.report.batch.rows,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_scenario_has_relations_and_a_query() {
        for name in all_scenarios() {
            let sc = scenario(name).unwrap();
            assert!(!sc.relations.is_empty(), "scenario {name}");
            assert!(sc.triangle_query.is_some(), "scenario {name}");
            for rel in &sc.relations {
                assert!(!rel.report.batch.rows.is_empty() || name == "unused");
            }
        }
    }
}
