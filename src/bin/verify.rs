//! Verification entry point: runs triangle, highly-skewed and sparse-
//! intersection scenarios, compares Leapfrog Triejoin against the independent
//! naive enumeration, and writes a human-auditable report.
//!
//! Run: `cargo run --bin lft-verify` (optionally `-- --out docs/verification-results.txt`).

use std::fs;
use std::io::Write;

use leapfrog_triejoin::dataset::FixtureSet;
use leapfrog_triejoin::types::NullPolicy;
use leapfrog_triejoin::verify::{make_request, verify_scenario};

fn main() {
    let out_path = std::env::args()
        .nth(1)
        .filter(|a| a == "--out")
        .map(|_| std::env::args().nth(2).unwrap_or_else(|| "docs/verification-results.txt".into()))
        .or_else(|| Some("docs/verification-results.txt".to_string()));

    let triangle = FixtureSet::by_name("triangle").unwrap();
    let skew = FixtureSet::by_name("skew").unwrap();
    let sparse = FixtureSet::by_name("sparse").unwrap();

    // Triangle: R(a,b) ▷◁ S(b,c) ▷◁ T(a,c).
    let tri_req = make_request(
        vec![
            ("R", vec!["a", "b"]),
            ("S", vec!["b", "c"]),
            ("T", vec!["a", "c"]),
        ],
        NullPolicy::SqlMatchNullsReject,
    );
    let tri = verify_scenario("triangle R(a,b),S(b,c),T(a,c)", &tri_req, &triangle.relations)
        .expect("triangle scenario");

    // Highly skewed two-way join on k.
    let skew_req = make_request(
        vec![("R", vec!["k", "v"]), ("S", vec!["k", "w"])],
        NullPolicy::SqlMatchNullsReject,
    );
    let skew_rep =
        verify_scenario("highly-skewed R(k,v),S(k,w) n=1000", &skew_req, &skew.relations)
            .expect("skew scenario");

    // Sparse intersection on k (single common key).
    let sparse_req = make_request(
        vec![("R", vec!["k", "v"]), ("S", vec!["k", "w"])],
        NullPolicy::SqlMatchNullsReject,
    );
    let sparse_rep =
        verify_scenario("sparse-intersection R,S (one shared key)", &sparse_req, &sparse.relations)
            .expect("sparse scenario");

    let mut text = String::new();
    text.push_str("Leapfrog Triejoin verification report\n");
    text.push_str("=====================================\n\n");
    for r in [&tri, &skew_rep, &sparse_rep] {
        text.push_str(&r.to_text());
        text.push('\n');
    }

    text.push_str("Triangle result multiset (a,b,c):\n");
    for row in &tri.leapfrog_rows {
        text.push_str(&format!("  {row:?}\n"));
    }

    println!("{text}");

    // Hard correctness gates: mismatches fail the binary with non-zero exit.
    let all_match = tri.results_match && skew_rep.results_match && sparse_rep.results_match;
    let no_blowup = tri.no_cartesian_blowup()
        && skew_rep.no_cartesian_blowup()
        && sparse_rep.no_cartesian_blowup();

    if !all_match {
        eprintln!("FAIL: leapfrog result multiset differs from naive oracle");
        std::process::exit(1);
    }
    if !no_blowup {
        eprintln!("FAIL: materialised trie exceeds cartesian intermediate");
        std::process::exit(1);
    }

    if let Some(path) = out_path {
        if let Some(parent) = std::path::Path::new(&path).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let mut f = fs::File::create(&path).expect("create report file");
        f.write_all(text.as_bytes()).expect("write report");
        println!("report written to {path}");
    }
}
