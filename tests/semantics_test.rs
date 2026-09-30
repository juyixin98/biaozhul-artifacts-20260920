//! Semantics integration tests.
//!
//! Every scenario asserts **concrete result values** and/or **failure
//! categories**, and success cases are cross-checked against the independent
//! JSON-only oracle in `tests/common` — not against the production engine.
//!
//! Required counterexample families: outer NULL keys, duplicate keys, an empty
//! inner table, and a multi-row scalar subquery.

mod common;

use common::{
    appended_columns, assert_strategies_agree, correlation, evaluate, key_tag_schema, rejected,
    relation, request, rows_of, run, success, verified_error, OracleOutcome,
};
use serde_json::{json, Value};

// ───────────────────────────── shared fixtures ─────────────────────────────

/// Outer: k 1 (one row), k 2 twice (duplicate rows), then a SQL NULL key.
fn outer_rows() -> Value {
    json!([
        { "k": 1, "tag": "a" },
        { "k": 2, "tag": "dup-first" },
        { "k": 2, "tag": "dup-second" },
        { "k": null, "tag": "null-key" }
    ])
}

/// Inner: two matches for k=1, one for k=2, one NULL-key row (unmatchable),
/// and a value column with a NULL for COUNT(col)/SUM distinction.
fn inner_rows() -> Value {
    json!([
        { "ik": 1, "v": 10 },
        { "ik": 1, "v": null },
        { "ik": 2, "v": 7 },
        { "ik": null, "v": 99 }
    ])
}

fn inner_relation(rows: Value) -> Value {
    relation(
        "inner_t",
        json!([
            { "name": "ik", "type": "Int64" },
            { "name": "v", "type": "Int64" }
        ]),
        rows,
    )
}

fn make_request(subqueries: Value, rows: Value) -> Value {
    request(
        "outer_t",
        json!([
            { "name": "k", "type": "Int64" },
            { "name": "tag", "type": "Utf8" }
        ]),
        rows,
        vec![inner_relation(inner_rows())],
        serde_json::from_value(subqueries).unwrap(),
    )
}

// ───────────────────────────────── tests ───────────────────────────────────

#[test]
fn exists_preserves_duplicate_rows_and_handles_null_outer_key() {
    let body = make_request(
        json!([
            {
                "alias": "has",
                "type": "exists",
                "input": "inner_t",
                "correlations": [ correlation("k", "ik") ]
            }
        ]),
        outer_rows(),
    );

    let resp = success(&run(body));
    assert_strategies_agree(&resp);

    let cols = appended_columns(&resp);
    // Concrete expectations: k=1 matches; both duplicate k=2 rows match;
    // the NULL-key outer row cannot match anything.
    assert_eq!(
        cols["has"],
        vec![json!(true), json!(true), json!(true), json!(false)]
    );

    // Outer multiplicity is preserved exactly (4 rows, tags in order).
    let final_rows = resp.results.final_rows.as_ref().unwrap();
    assert_eq!(final_rows.row_count, 4);
    let tags: Vec<&str> = final_rows
        .rows
        .iter()
        .map(|r| r.get("tag").unwrap().as_str().unwrap())
        .collect();
    assert_eq!(tags, vec!["a", "dup-first", "dup-second", "null-key"]);
}

#[test]
fn not_exists_vs_exists_are_complementary_including_nulls() {
    let mut body = make_request(
        json!([
            {
                "alias": "missing",
                "type": "not_exists",
                "input": "inner_t",
                "correlations": [ correlation("k", "ik") ]
            }
        ]),
        outer_rows(),
    );
    let resp = success(&run(body.clone()));
    let cols = appended_columns(&resp);
    assert_eq!(
        cols["missing"],
        vec![json!(false), json!(false), json!(false), json!(true)],
        "NULL outer key yields NOT EXISTS = true"
    );

    // Flip the same request to EXISTS and assert exact Boolean complement.
    body["subqueries"][0]["type"] = json!("exists");
    let resp2 = success(&run(body));
    let cols2 = appended_columns(&resp2);
    let exists_vals = &cols2["missing"];
    for (ne, e) in cols["missing"].iter().zip(exists_vals) {
        assert_ne!(ne, e, "EXISTS and NOT EXISTS must be complementary");
        assert!(ne.is_boolean() && e.is_boolean());
    }
}

#[test]
fn empty_inner_table_marks_aggregates_correctly() {
    // COUNT(*)=0, COUNT(v)=0, SUM(v)=NULL, EXISTS=false, NOT EXISTS=true for
    // EVERY outer row when the inner table is empty.
    let body = request(
        "outer_t",
        key_tag_schema("k", "tag2"),
        json!([ { "k": 1, "tag2": "x" }, { "k": null, "tag2": "y" } ]),
        vec![inner_relation(json!([]))],
        serde_json::from_value(json!([
            { "alias": "n_star", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "count",
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "n_v", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "count", "agg_argument": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "s_v", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "sum", "agg_argument": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "has", "type": "exists", "input": "inner_t",
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "missing", "type": "not_exists", "input": "inner_t",
              "correlations": [ correlation("k", "ik") ] }
        ]))
        .unwrap(),
    );

    let resp = success(&run(body));
    assert_strategies_agree(&resp);
    let cols = appended_columns(&resp);
    for (row, n_star) in cols["n_star"].iter().enumerate() {
        assert_eq!(n_star, &json!(0), "COUNT(*) empty group is 0");
        assert_eq!(cols["n_v"][row], json!(0), "COUNT(v) empty group is 0");
        assert_eq!(cols["s_v"][row], Value::Null, "SUM empty group MUST be NULL, not 0");
        assert_eq!(cols["has"][row], json!(false));
        assert_eq!(cols["missing"][row], json!(true));
    }
}

#[test]
fn count_column_skips_null_values_but_count_star_does_not() {
    // k=1 matches two inner rows: (1,10) and (1,NULL).
    let body = make_request(
        json!([
            { "alias": "n_star", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "count",
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "n_v", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "count", "agg_argument": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "s_v", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "sum", "agg_argument": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]),
        outer_rows(),
    );
    let resp = success(&run(body));
    let cols = appended_columns(&resp);
    // Order: k=1, k=2, k=2(dup), NULL
    assert_eq!(cols["n_star"], vec![json!(2), json!(1), json!(1), json!(0)]);
    assert_eq!(cols["n_v"], vec![json!(1), json!(1), json!(1), json!(0)]);
    assert_eq!(
        cols["s_v"],
        vec![json!(10), json!(7), json!(7), Value::Null],
        "SUM skips NULL inputs; unmatched and all-NULL groups are NULL"
    );
}

#[test]
fn multirow_scalar_subquery_is_cardinality_violation_in_both_strategies() {
    // Bare scalar over inner_t: k=1 has TWO matching rows → error at outer
    // row #0; k=2 one row → value; NULL key → no match → NULL.
    let body = make_request(
        json!([
            { "alias": "only_v", "type": "scalar", "input": "inner_t",
              "value": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]),
        json!([
            { "k": 2, "tag": "one-match" },
            { "k": 9, "tag": "no-match" },
            { "k": null, "tag": "null-no-match" },
            { "k": 1, "tag": "two-matches-fails" }
        ]),
    );

    let outcome = run(body);
    let (status, err) = verified_error(&outcome);
    assert_eq!(status, 422);
    assert_eq!(err.error.category, "CardinalityViolation");
    assert_eq!(err.error.code, "cardinality_violation");
    // Fails at the FIRST violating outer row (index 3), in both strategies.
    assert_eq!(err.error.at_outer_row, Some(3));
    assert_eq!(err.error.at_step, "exec:decorrelated_hash_pipeline");
}

#[test]
fn scalar_subquery_zero_and_one_rows_give_null_and_value() {
    let body = make_request(
        json!([
            { "alias": "only_v", "type": "scalar", "input": "inner_t",
              "value": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]),
        json!([
            { "k": 9, "tag": "no-match-null" },
            { "k": 2, "tag": "one-match-value-7" },
            { "k": 2, "tag": "duplicate-also-7" }
        ]),
    );
    let resp = success(&run(body));
    assert_strategies_agree(&resp);
    let cols = appended_columns(&resp);
    assert_eq!(cols["only_v"], vec![Value::Null, json!(7), json!(7)]);
}

#[test]
fn scalar_value_itself_null_is_returned_as_null() {
    // k=1 matches (1,10) and (1,NULL): cardinality is 2 → error regardless.
    // For exactly-one-match with NULL value: build a dedicated inner table.
    let body = request(
        "outer_t",
        key_tag_schema("k", "tag"),
        json!([ { "k": 1, "tag": "only-match-is-null" } ]),
        vec![inner_relation(json!([ { "ik": 1, "v": null } ]))],
        serde_json::from_value(json!([
            { "alias": "only_v", "type": "scalar", "input": "inner_t",
              "value": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]))
        .unwrap(),
    );
    let resp = success(&run(body));
    let cols = appended_columns(&resp);
    assert_eq!(cols["only_v"], vec![Value::Null], "one matching row whose value is NULL → NULL");
}

#[test]
fn null_inner_key_rows_never_match_even_for_null_outer_key() {
    // Inner has ONLY a NULL-key row. Both a normal outer key and a NULL outer
    // key must fail to match under SQL equality.
    let body = request(
        "outer_t",
        key_tag_schema("k", "tag"),
        json!([ { "k": null, "tag": "null-outer" }, { "k": 1, "tag": "one-outer" } ]),
        vec![inner_relation(json!([ { "ik": null, "v": 99 }, { "ik": null, "v": 5 } ]))],
        serde_json::from_value(json!([
            { "alias": "has", "type": "exists", "input": "inner_t",
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "n", "type": "scalar_agg", "input": "inner_t", "agg_func": "count",
              "correlations": [ correlation("k", "ik") ] }
        ]))
        .unwrap(),
    );
    let resp = success(&run(body));
    let cols = appended_columns(&resp);
    assert_eq!(cols["has"], vec![json!(false), json!(false)]);
    assert_eq!(cols["n"], vec![json!(0), json!(0)]);

    // The build trace must report the NULL-key rows as excluded.
    let trace = &resp.results.decorrelated.trace;
    assert!(
        trace.iter().any(|t| t.step == "hash_build" && t.detail.contains("2 row(s) excluded (NULL key)")),
        "trace should explain NULL-key exclusion, got: {:#?}",
        trace
    );
}

// ───────────────────── independent oracle cross-checks ─────────────────────

#[test]
fn oracle_cross_check_all_kinds_match_engine_outputs() {
    let outer = outer_rows();
    let inner = inner_rows();
    let specs: Value = json!([
        { "alias": "o_has", "type": "exists", "input": "inner_t",
          "correlations": [ correlation("k", "ik") ] },
        { "alias": "o_missing", "type": "not_exists", "input": "inner_t",
          "correlations": [ correlation("k", "ik") ] },
        { "alias": "o_cnt", "type": "scalar_agg", "input": "inner_t",
          "agg_func": "count",
          "correlations": [ correlation("k", "ik") ] },
        { "alias": "o_cntv", "type": "scalar_agg", "input": "inner_t",
          "agg_func": "count", "agg_argument": { "name": "v" },
          "correlations": [ correlation("k", "ik") ] },
        { "alias": "o_sumv", "type": "scalar_agg", "input": "inner_t",
          "agg_func": "sum", "agg_argument": { "name": "v" },
          "correlations": [ correlation("k", "ik") ] }
    ]);
    let body = request(
        "outer_t",
        json!([
            { "name": "k", "type": "Int64" },
            { "name": "tag", "type": "Utf8" }
        ]),
        outer.clone(),
        vec![inner_relation(inner.clone())],
        serde_json::from_value(specs.clone()).unwrap(),
    );
    let resp = success(&run(body));
    let cols = appended_columns(&resp);

    let outer_json = rows_of(&json!({ "rows": outer }));
    let inner_json = rows_of(&json!({ "rows": inner }));
    for spec in specs.as_array().unwrap() {
        let alias = spec["alias"].as_str().unwrap();
        match evaluate(&outer_json, &inner_json, spec.as_object().unwrap()) {
            OracleOutcome::Values(expected) => {
                assert_eq!(
                    cols[alias], expected,
                    "engine output for `{}` must equal the independent oracle",
                    alias
                );
            }
            OracleOutcome::Error { .. } => panic!("oracle did not expect error for {}", alias),
        }
    }
}

#[test]
fn oracle_cross_check_scalar_cardinality_failure() {
    let spec = json!({
        "alias": "only_v", "type": "scalar", "input": "inner_t",
        "value": { "name": "v" },
        "correlations": [ correlation("k", "ik") ]
    });
    let outer_json: Vec<_> = serde_json::from_value::<Vec<serde_json::Map<String, Value>>>(json!([
        { "k": 1, "tag": "two-matches" }
    ]))
    .unwrap();
    let inner_json = rows_of(&json!({ "rows": inner_rows() }));
    match evaluate(&outer_json, &inner_json, spec.as_object().unwrap()) {
        OracleOutcome::Error { at_outer_row, category, code } => {
            assert_eq!(at_outer_row, 0);
            assert_eq!(category, "CardinalityViolation");
            assert_eq!(code, "cardinality_violation");
        }
        other => panic!("oracle must flag cardinality violation, got {:?}", other),
    }
}

// ─────────────────────── validation / rejection semantics ──────────────────

#[test]
fn not_in_is_rejected_with_dedicated_category() {
    let body = make_request(
        json!([
            { "alias": "bad", "type": "not_in", "input": "inner_t",
              "value": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]),
        json!([ { "k": 1, "tag": "x" } ]),
    );
    let (status, err) = rejected(&run(body));
    assert_eq!(status, 400);
    assert_eq!(err.error.category, "UnsupportedNotIn");
    assert_eq!(err.error.code, "not_in_unsupported");
    assert!(err.error.message.contains("NOT IN"));
    // The rejection must be at validation, before rewrite/execution.
    let step_names: Vec<&str> = err.steps.iter().map(|s| s.name).collect();
    assert_eq!(step_names, vec!["validate"]);
}

#[test]
fn uncorrelated_subquery_is_rejected_as_unsupported_form() {
    let body = make_request(
        json!([
            { "alias": "uc", "type": "exists", "input": "inner_t", "correlations": [] }
        ]),
        json!([ { "k": 1, "tag": "x" } ]),
    );
    let (status, err) = rejected(&run(body));
    assert_eq!(status, 400);
    assert_eq!(err.error.category, "UnsupportedForm");
    assert_eq!(err.error.code, "correlation_required");
}

#[test]
fn incomparable_key_types_are_rejected() {
    let body = request(
        "outer_t",
        json!([{ "name": "k", "type": "Utf8" }, { "name": "tag", "type": "Utf8" }]),
        json!([ { "k": "1", "tag": "x" } ]),
        vec![inner_relation(inner_rows())],
        serde_json::from_value(json!([
            { "alias": "has", "type": "exists", "input": "inner_t",
              "correlations": [ correlation("k", "ik") ] }
        ]))
        .unwrap(),
    );
    let (status, err) = rejected(&run(body));
    assert_eq!(status, 400);
    assert_eq!(err.error.category, "TypeMismatch");
    assert_eq!(err.error.code, "incomparable_types");
}

#[test]
fn unknown_relation_and_column_are_validation_errors() {
    let mut body = make_request(
        json!([
            { "alias": "has", "type": "exists", "input": "ghost",
              "correlations": [ correlation("k", "ik") ] }
        ]),
        json!([ { "k": 1, "tag": "x" } ]),
    );
    let (_, err) = rejected(&run(body));
    assert_eq!(err.error.code, "unknown_relation");

    body = make_request(
        json!([
            { "alias": "has", "type": "exists", "input": "inner_t",
              "correlations": [ correlation("nope", "ik") ] }
        ]),
        json!([ { "k": 1, "tag": "x" } ]),
    );
    let (_, err) = rejected(&run(body));
    assert_eq!(err.error.code, "unknown_column");
}

#[test]
fn int_sum_overflow_is_same_failure_category_in_both_strategies() {
    // i64::MAX + i64::MAX must overflow for the single outer row.
    let body = request(
        "outer_t",
        key_tag_schema("k", "tag"),
        json!([ { "k": 1, "tag": "overflow" } ]),
        vec![inner_relation(json!([
            { "ik": 1, "v": i64::MAX },
            { "ik": 1, "v": i64::MAX }
        ]))],
        serde_json::from_value(json!([
            { "alias": "s_v", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "sum", "agg_argument": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]))
        .unwrap(),
    );
    let (status, err) = verified_error(&run(body));
    assert_eq!(status, 422);
    assert_eq!(err.error.category, "NumericOverflow");
    assert_eq!(err.error.code, "sum_overflow");
    assert_eq!(err.error.at_outer_row, Some(0));
}
