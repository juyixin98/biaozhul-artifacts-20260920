//! Shared fixtures and assertions for integration tests.
//!
//! The reference results in the test files are HAND-COMPUTED from the tables
//! below and written out as literals; they are never produced by the engine
//! under test. If you change a table here, recompute every expectation by
//! hand (SQL three-valued logic, empty-group rules) and update the tests.
//!
//! orders (outer):                         lineitems (inner):
//!   id cust_key amount  status              order_key qty  price
//!   1  10       100.0   new                 10        2    9.5
//!   2  20       200.5   paid                10        3    1.5
//!   3  10       150.0   new                 20        1    100.0
//!   4  NULL     50.0    new                 20        NULL 3.0
//!   5  30       75.25   paid                20        4    2.0
//!   6  10       300.0   hold                30        5    7.25
//!   7  40       120.0   new                 NULL      9    4.0
//!   8  50       10.0    new                 10        1    0.5
//!                                           50        NULL NULL
//!
//! Correlated groups (order_key = cust_key):
//!   key 10 -> 3 rows, qty {2,3,1} sum 6,  price sum 11.5
//!   key 20 -> 3 rows, qty {1,NULL,4}: COUNT(qty)=2, SUM(qty)=5, price sum 105.0
//!   key 30 -> 1 row,  qty {5}, price {7.25}
//!   key 40 -> no rows (empty group)
//!   key 50 -> 1 row, qty NULL, price NULL: COUNT(*)=1, COUNT(qty)=0, SUM=NULL
//!   NULL   -> empty group (NULL never matches an equi-key)

#![allow(dead_code)]

use std::path::PathBuf;

use serde_json::{json, Value as Json};

use decorr_service::config::Config;
use decorr_service::service::{JobReport, Mode, QueryJob};
use decorr_service::state::{Catalog, FixtureColumn, FixtureTable};

pub fn test_config() -> Config {
    Config {
        host: "127.0.0.1".to_string(),
        port: 0,
        fixtures_dir: PathBuf::from("fixtures"),
        max_rows_per_table: 50_000,
        max_outer_rows_per_query: 100_000,
        log_level: "off".to_string(),
    }
}

pub fn ints(name: &str, vals: &[Option<i64>]) -> FixtureColumn {
    FixtureColumn {
        name: name.to_string(),
        ty_name: "int".to_string(),
        values: vals.iter().map(|v| v.map(|x| json!(x))).collect(),
    }
}

pub fn floats(name: &str, vals: &[Option<f64>]) -> FixtureColumn {
    FixtureColumn {
        name: name.to_string(),
        ty_name: "float".to_string(),
        values: vals.iter().map(|v| v.map(|x| json!(x))).collect(),
    }
}

pub fn strs(name: &str, vals: &[Option<&str>]) -> FixtureColumn {
    FixtureColumn {
        name: name.to_string(),
        ty_name: "str".to_string(),
        values: vals.iter().map(|v| v.map(|s| json!(s))).collect(),
    }
}

pub fn table(name: &str, columns: Vec<FixtureColumn>) -> FixtureTable {
    FixtureTable {
        name: name.to_string(),
        columns,
    }
}

pub fn orders() -> FixtureTable {
    table(
        "orders",
        vec![
            ints(
                "id",
                &[Some(1), Some(2), Some(3), Some(4), Some(5), Some(6), Some(7), Some(8)],
            ),
            ints(
                "cust_key",
                &[Some(10), Some(20), Some(10), None, Some(30), Some(10), Some(40), Some(50)],
            ),
            floats(
                "amount",
                &[
                    Some(100.0),
                    Some(200.5),
                    Some(150.0),
                    Some(50.0),
                    Some(75.25),
                    Some(300.0),
                    Some(120.0),
                    Some(10.0),
                ],
            ),
            strs(
                "status",
                &[
                    Some("new"),
                    Some("paid"),
                    Some("new"),
                    Some("new"),
                    Some("paid"),
                    Some("hold"),
                    Some("new"),
                    Some("new"),
                ],
            ),
        ],
    )
}

pub fn lineitems() -> FixtureTable {
    table(
        "lineitems",
        vec![
            ints(
                "order_key",
                &[
                    Some(10),
                    Some(10),
                    Some(20),
                    Some(20),
                    Some(20),
                    Some(30),
                    None,
                    Some(10),
                    Some(50),
                ],
            ),
            ints(
                "qty",
                &[Some(2), Some(3), Some(1), None, Some(4), Some(5), Some(9), Some(1), None],
            ),
            floats(
                "price",
                &[
                    Some(9.5),
                    Some(1.5),
                    Some(100.0),
                    Some(3.0),
                    Some(2.0),
                    Some(7.25),
                    Some(4.0),
                    Some(0.5),
                    None,
                ],
            ),
        ],
    )
}

pub fn empty_inner() -> FixtureTable {
    table("empty_inner", vec![ints("k", &[]), ints("v", &[])])
}

pub fn customers() -> FixtureTable {
    table(
        "customers",
        vec![
            strs("name", &[Some("Ana"), Some("Bo"), Some("Cy"), Some("Di")]),
            strs("region", &[Some("eu"), Some("us"), None, Some("eu")]),
        ],
    )
}

pub fn shipments() -> FixtureTable {
    table(
        "shipments",
        vec![
            strs("dest", &[Some("eu"), Some("eu"), Some("ap"), None]),
            floats("weight", &[Some(1.0), Some(2.5), Some(3.0), Some(4.0)]),
        ],
    )
}

pub fn test_catalog() -> Catalog {
    Catalog::default()
        .with_inline(vec![orders(), lineitems(), empty_inner(), customers(), shipments()])
        .expect("test catalog builds")
}

pub fn run_with(catalog: &Catalog, config: &Config, sql: &str, mode: Mode) -> JobReport {
    QueryJob {
        request_id: "test-request",
        sql,
        mode,
        base_catalog: catalog,
        inline_tables: vec![],
        config,
    }
    .run()
}

pub fn run(sql: &str, mode: Mode) -> JobReport {
    let catalog = test_catalog();
    let config = test_config();
    run_with(&catalog, &config, sql, mode)
}

/// Run in `both` mode, require rewrite == reference, and require the primary
/// result to equal the hand-computed rows.
pub fn check(sql: &str, expected: Json) -> JobReport {
    let report = run(sql, Mode::Both);
    assert_equivalent(&report);
    assert_rows(&report, &expected);
    // Each strategy individually must reproduce the same rows.
    assert_eq!(report.executions.len(), 2, "both strategies ran for: {sql}");
    for exec in &report.executions {
        let rows = exec
            .result
            .as_ref()
            .unwrap_or_else(|| panic!("strategy {} failed for: {sql}", exec.strategy));
        assert_eq!(
            serde_json::to_value(&rows.rows).unwrap(),
            expected,
            "strategy {} disagrees with hand-computed rows for: {sql}",
            exec.strategy
        );
    }
    report
}

pub fn assert_equivalent(report: &JobReport) {
    let eq = report
        .equivalence
        .as_ref()
        .expect("both mode always emits an equivalence report");
    assert!(
        eq.equivalent,
        "differential mismatch for {}: {:?}",
        report.sql,
        eq.mismatch
    );
}

pub fn assert_rows(report: &JobReport, expected: &Json) {
    assert!(
        report.error.is_none(),
        "unexpected error for {}: {:?}",
        report.sql,
        report.error
    );
    let result = report.result.as_ref().expect("result present");
    let actual = serde_json::to_value(&result.rows).unwrap();
    assert_eq!(&actual, expected, "row mismatch for {}", report.sql);
    assert_eq!(
        result.row_count,
        expected.as_array().expect("expected rows array").len(),
        "row_count metadata for {}",
        report.sql
    );
}

/// Run in `both` mode and require the given error category.
pub fn check_error(sql: &str, kind: &str) -> JobReport {
    let report = run(sql, Mode::Both);
    let err = report
        .error
        .as_ref()
        .unwrap_or_else(|| panic!("expected {kind} for: {sql}"));
    assert_eq!(err.kind, kind, "wrong error kind for: {sql}");
    report
}
