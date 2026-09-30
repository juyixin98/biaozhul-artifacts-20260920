//! Independent nested-loop reference enumeration (the test oracle).
//!
//! Deliberately the simplest possible, obviously-correct natural join: choose
//! one row from each relation (nested loops over the **cartesian product** of
//! the relations), then test every pair of columns bound to the same variable
//! for equality under the null policy. Every surviving combination emits one
//! projected row; duplicate input rows are separate choices, so multiset
//! multiplicity falls out for free.
//!
//! It builds no trie and shares no code with [`crate::leapfrog`], so a
//! self-consistent bug in the Triejoin cannot manufacture a passing test. Its
//! work metric [`NaiveResult::combinations_tested`] is the cartesian baseline —
//! exactly the blow-up Leapfrog avoids — and the verification harness compares
//! result multisets *and* contrasts that cost against leapfrog seeks/nexts.

use crate::plan::JoinPlan;
use crate::types::{Datum, NullPolicy};

/// Reference output and its deliberately cartesian cost.
#[derive(Debug, Default, Clone)]
pub struct NaiveResult {
    /// Projected output rows as datums (multiset: duplicates preserved).
    pub rows: Vec<Vec<Datum>>,
    /// Full row-combinations examined (product of retained relation sizes).
    pub combinations_tested: u64,
    /// Scalar equality comparisons performed while testing a combination.
    pub equality_checks: u64,
}

/// Equality under the active null policy. SQL rejects null=null; match_nulls
/// treats null as an ordinary equal value.
fn values_equal(a: &Datum, b: &Datum, policy: NullPolicy) -> bool {
    match policy {
        NullPolicy::SqlMatchNullsReject => !a.is_null() && !b.is_null() && a == b,
        NullPolicy::MatchNulls => a == b,
    }
}

/// Enumerate the join with a plain cartesian nested loop.
///
/// `projected_rows[ri]` are the relation's retained rows already placed in
/// global trie order (see [`project_and_filter`]), so positions line up with
/// `plan.relations[ri].trie_vars`.
pub fn enumerate(plan: &JoinPlan, projected_rows: &[Vec<Vec<Datum>>]) -> NaiveResult {
    let n_rel = plan.relations.len();
    debug_assert_eq!(projected_rows.len(), n_rel);

    // An empty relation makes the cartesian product empty.
    if projected_rows.iter().any(|r| r.is_empty()) {
        return NaiveResult::default();
    }

    // For each pair (ri, trie-level) record the global variable index.
    let trie_global: Vec<Vec<usize>> = plan
        .relations
        .iter()
        .map(|pr| {
            pr.trie_vars
                .iter()
                .map(|v| {
                    plan.ordered_vars
                        .iter()
                        .position(|x| x == v)
                        .expect("trie var present")
                })
                .collect()
        })
        .collect();

    // Equality constraints: for each global variable, the (relation, trie-level)
    // slots that must all agree.
    let mut slots_by_var: Vec<Vec<(usize, usize)>> =
        vec![Vec::new(); plan.ordered_vars.len()];
    for ri in 0..n_rel {
        for (tl, &gl) in trie_global[ri].iter().enumerate() {
            slots_by_var[gl].push((ri, tl));
        }
    }

    let mut chosen: Vec<usize> = vec![0; n_rel];
    let mut rows: Vec<Vec<Datum>> = Vec::new();
    let mut combinations: u64 = 0;
    let mut checks: u64 = 0;

    // Iterate the cartesian product with an odometer (no recursion depth limit
    // concerns and easy to read).
    loop {
        combinations += 1;

        // Gather the global assignment implied by the chosen rows.
        let mut assignment: Vec<Option<Datum>> = vec![None; plan.ordered_vars.len()];
        let mut consistent = true;

        'vars: for gl in 0..plan.ordered_vars.len() {
            for &(ri, tl) in &slots_by_var[gl] {
                let val = &projected_rows[ri][chosen[ri]][tl];
                checks += 1;
                match &assignment[gl] {
                    None => assignment[gl] = Some(val.clone()),
                    Some(prev) => {
                        if !values_equal(prev, val, plan.null_policy) {
                            consistent = false;
                            break 'vars;
                        }
                    }
                }
            }
        }

        if consistent {
            let row: Vec<Datum> = plan
                .projection
                .iter()
                .map(|&gl| assignment[gl].clone().unwrap_or(Datum::Null))
                .collect();
            rows.push(row);
        }

        // Odometer increment.
        let mut k = n_rel;
        loop {
            if k == 0 {
                // Enumerated every combination.
                rows.sort();
                return NaiveResult {
                    rows,
                    combinations_tested: combinations,
                    equality_checks: checks,
                };
            }
            k -= 1;
            chosen[k] += 1;
            if chosen[k] < projected_rows[k].len() {
                break;
            }
            chosen[k] = 0;
        }
    }
}

/// Apply the trie builder's null policy and place each relation's retained rows
/// in global trie order, so the oracle can ingest raw catalog rows directly.
pub fn project_and_filter(
    plan: &JoinPlan,
    catalog_rows: &[Vec<Vec<Datum>>],
) -> Vec<Vec<Vec<Datum>>> {
    let reject = plan.null_policy == NullPolicy::SqlMatchNullsReject;
    plan.relations
        .iter()
        .enumerate()
        .map(|(ri, pr)| {
            let lmap: Vec<usize> = pr
                .trie_vars
                .iter()
                .map(|v| {
                    plan.ordered_vars
                        .iter()
                        .position(|x| x == v)
                        .expect("trie var present")
                })
                .collect();
            catalog_rows[ri]
                .iter()
                .filter_map(|row| {
                    let proj: Vec<Datum> =
                        pr.source_columns.iter().map(|&c| row[c].clone()).collect();
                    let null_key = reject
                        && proj.iter().enumerate().any(|(tl, d)| {
                            lmap[tl] < plan.join_var_count && d.is_null()
                        });
                    if null_key {
                        None
                    } else {
                        Some(proj)
                    }
                })
                .collect()
        })
        .collect()
}
