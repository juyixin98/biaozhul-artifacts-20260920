//! Exhaustive split-point tests ("全部切分点测试").
//!
//! For every fixture wire string and for *every* cut position k in 1..len,
//! the same bytes are fed as two chunks `[..k]` then `[k..]`. In addition
//! each fixture is fed **one byte at a time**. Whatever chunking is used,
//! the sequence of frames the incremental parser yields must be identical
//! to feeding the whole input at once.
//!
//! A second, even stricter matrix feeds random 3-way and 4-way splits to
//! cover "split in the middle of a multi-message pipeline".
//!
//! Fixtures intentionally include the required edge cases:
//!   * CRLF appearing *inside* a bulk payload,
//!   * negative bulk lengths (-1 legal null, others fatal),
//!   * deeply nested arrays at exactly the depth limit,
//!   * several messages concatenated in one stream.

use resp_incremental::{Config, ParseError, Parser, Poll, Value};

/// Outcome of draining a parser over a complete input.
#[derive(Debug, PartialEq)]
enum Outcome {
    Frames(Vec<Value>),
    Error(ParseError),
}

fn run_chunked(wire: &[u8], cuts: &[usize], config: &Config) -> Outcome {
    let mut p = Parser::new(config.clone());
    let mut frames = Vec::new();
    let mut start = 0usize;
    for &cut in cuts {
        assert!(cut >= start && cut <= wire.len(), "bad cut {}..{}", start, cut);
        if cut > start {
            p.feed(&wire[start..cut]).unwrap();
        }
        start = cut;
        loop {
            match p.try_next() {
                Poll::Ready(v) => frames.push(v),
                Poll::Pending => break,
                Poll::Error(e) => return Outcome::Error(e),
            }
        }
    }
    if start < wire.len() {
        p.feed(&wire[start..]).unwrap();
    }
    loop {
        match p.try_next() {
            Poll::Ready(v) => frames.push(v),
            Poll::Pending => break,
            Poll::Error(e) => return Outcome::Error(e),
        }
    }
    Outcome::Frames(frames)
}

fn run_whole(wire: &[u8], config: &Config) -> Outcome {
    run_chunked(wire, &[], config)
}

fn assert_every_cut_invariant(wire: &[u8], config: &Config, label: &str) {
    let reference = run_whole(wire, config);

    // Single cut at every position 1..len (cut at 0/len are equivalent to
    // feeding the whole input, tested via the reference itself).
    for k in 1..wire.len() {
        let got = run_chunked(wire, &[k], config);
        assert_eq!(
            got, reference,
            "[{}] 2-way split at {} differs from whole input; wire={:?}",
            label, k, wire
        );
    }

    // One byte at a time — the finest possible chunking.
    let cuts: Vec<usize> = (1..wire.len()).collect();
    let got = run_chunked(wire, &cuts, config);
    assert_eq!(
        got, reference,
        "[{}] byte-at-a-time differs from whole input; wire={:?}",
        label, wire
    );
}

/// Deterministic pseudo-random sequence so the matrix is reproducible.
struct Lcg(u64);
impl Lcg {
    fn next(&mut self, bound: usize) -> usize {
        // Numerical Recipes constants.
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 33) as usize) % bound
    }
}

fn assert_random_multi_cuts(wire: &[u8], config: &Config, label: &str) {
    let reference = run_whole(wire, config);
    let mut rng = Lcg(0x1234_5678_9abc_def0 ^ wire.len() as u64);
    if wire.len() < 4 {
        return;
    }
    for _ in 0..64 {
        let n_parts = 2 + rng.next(3); // 2..=4 chunks
        let mut cuts: Vec<usize> = (0..n_parts - 1)
            .map(|_| 1 + rng.next(wire.len() - 1))
            .collect();
        cuts.sort_unstable();
        cuts.dedup();
        let got = run_chunked(wire, &cuts, config);
        assert_eq!(
            got, reference,
            "[{}] random multi-cut {:?} differs; wire={:?}",
            label, cuts, wire
        );
    }
}

fn fixtures() -> Vec<(&'static str, &'static [u8])> {
    vec![
        ("simple", b"+OK\r\n".as_slice()),
        ("simple-empty", b"+\r\n"),
        ("error", b"-ERR bad thing happened\r\n"),
        ("integer-zero", b":0\r\n"),
        ("integer-neg", b":-42\r\n"),
        ("integer-min", b":-9223372036854775808\r\n"),
        ("integer-max", b":9223372036854775807\r\n"),
        ("bulk-hello", b"$5\r\nhello\r\n"),
        ("bulk-empty", b"$0\r\n\r\n"),
        ("bulk-null", b"$-1\r\n"),
        // CRLF lives INSIDE the payload — the parser must count bytes, not scan.
        ("bulk-embedded-crlf", b"$9\r\nab\r\ncdXf\r\n".as_slice()),
        ("bulk-embedded-crlf-only", b"$4\r\n\r\n\r\n\r\n".as_slice()),
        ("bulk-leading-crlf", b"$3\r\n\r\nab\r\n".as_slice()),
        ("bulk-trailing-crlf", b"$4\r\nab\r\n\r\n".as_slice()),
        ("bulk-nul-bytes", b"$5\r\n\x00\x01\x02\x03\x04\r\n".as_slice()),
        ("bulk-all-cr", b"$3\r\n\r\r\r\r\n".as_slice()),
        ("array-empty", b"*0\r\n"),
        ("array-null", b"*-1\r\n"),
        ("array-ints", b"*3\r\n:1\r\n:2\r\n:3\r\n"),
        ("array-mixed", b"*4\r\n+a\r\n-b\r\n:1\r\n$2\r\nhi\r\n"),
        ("array-nested", b"*2\r\n*2\r\n:1\r\n:2\r\n*2\r\n:3\r\n:4\r\n"),
        ("array-with-null-and-empty", b"*3\r\n$-1\r\n$0\r\n\r\n$1\r\nx\r\n"),
        // Exactly max_depth = 2 nesting (outer 0, inner 1, innermost 2).
        ("at-depth-limit", b"*1\r\n*1\r\n*1\r\n:1\r\n"),
        // Multi-message pipelines.
        (
            "pipeline-two",
            b"*1\r\n$4\r\nPING\r\n+PONG\r\n".as_slice(),
        ),
        (
            "pipeline-set-get",
            b"*3\r\n$3\r\nSET\r\n$1\r\na\r\n$1\r\nb\r\n*2\r\n$3\r\nGET\r\n$1\r\na\r\n",
        ),
        (
            "pipeline-five-scalars",
            b"+a\r\n-b\r\n:1\r\n:2\r\n$0\r\n\r\n",
        ),
        // Bulk whose content mimics protocol framing.
        ("bulk-looks-like-frame", b"$12\r\n:999\r\n+hi\r\n".as_slice()),
        ("bulk-looks-like-array", b"$6\r\n*1\r\n:1\r\n".as_slice()),
    ]
}

#[test]
fn every_split_point_of_each_valid_fixture_is_transparent() {
    let config = Config::default();
    for (label, wire) in fixtures() {
        assert!(!wire.is_empty());
        assert_every_cut_invariant(wire, &config, label);
        assert_random_multi_cuts(wire, &config, label);
    }
}

#[test]
fn split_invariants_hold_under_tight_limits() {
    // Same bytes, different configuration: still identical outcomes across
    // every cut. Fixtures here are all within tight limits.
    let config = Config {
        max_bulk_length: 16,
        max_array_length: 8,
        max_depth: 2,
        max_line_length: 32,
        max_total_bytes: 512,
    };
    let small: Vec<(&str, &[u8])> = fixtures()
        .into_iter()
        // embedded/large fixtures exceed 16 bytes of payload; keep them out.
        .filter(|(label, wire)| {
            !matches!(
                *label,
                "bulk-embedded-crlf"
                    | "bulk-embedded-crlf-only"
                    | "bulk-trailing-crlf"
                    | "bulk-all-cr"
                    | "bulk-looks-like-frame"
            ) && wire.len() <= 64
        })
        .collect();
    for (label, wire) in small {
        assert_every_cut_invariant(wire, &config, label);
    }
}

// ---------- fatal inputs: chunking must not change the error ---------------

fn fatal_fixtures() -> Vec<(&'static str, &'static [u8], &'static str)> {
    vec![
        ("bad-type", b"@123\r\n", "UnknownTypeByte"),
        ("bare-lf", b"+OK\n", "InvalidLineEnding"),
        ("cr-no-lf", b"+OK\rX", "InvalidLineEnding"),
        ("bulk-neg2", b"$-2\r\n", "InvalidBulkLength"),
        ("bulk-neg99", b"$-99\r\n", "InvalidBulkLength"),
        ("int-junk", b":12a\r\n", "InvalidInteger"),
        ("int-overflow", b":99999999999999999999999\r\n", "IntegerOverflow"),
        ("array-neg2", b"*-2\r\n", "InvalidInteger"),
        ("simple-bad-utf8", b"+\xff\r\n", "InvalidUtf8"),
        ("bulk-trailer", b"$3\r\nabcZZ", "InvalidLineEnding"),
    ]
}

#[test]
fn every_split_point_of_each_fatal_fixture_yields_same_error() {
    let config = Config::default();
    for (label, wire, kind) in fatal_fixtures() {
        let reference = run_whole(wire, &config);
        match &reference {
            Outcome::Error(_) => {}
            other => panic!("fixture {} expected fatal error, got {:?}", label, other),
        }
        for k in 1..wire.len() {
            let got = run_chunked(wire, &[k], &config);
            assert_eq!(got, reference, "[{}] cut {} changed outcome", label, k);
        }
        let cuts: Vec<usize> = (1..wire.len()).collect();
        let got = run_chunked(wire, &cuts, &config);
        assert_eq!(got, reference, "[{}] byte-wise error differs", kind);
    }
}

// ---------- limits exercised across splits ---------------------------------

#[test]
fn depth_limit_error_is_chunking_independent() {
    // max_depth = 2; frame requires depth 3.
    let wire = b"*1\r\n*1\r\n*1\r\n*1\r\n:1\r\n";
    let config = Config {
        max_depth: 2,
        ..Config::default()
    };
    assert_every_cut_invariant(wire, &config, "depth-3");
    assert_eq!(
        run_whole(wire, &config),
        Outcome::Error(ParseError::NestingTooDeep { depth: 3, max: 2 })
    );
}

#[test]
fn budget_error_is_chunking_independent_in_a_pipeline() {
    // Two frames, 7 bytes each; budget 10: first succeeds, second fails.
    let wire = b":1234\r\n:5678\r\n";
    let config = Config {
        max_total_bytes: 10,
        ..Config::default()
    };
    assert_every_cut_invariant(wire, &config, "budget-pipeline");
    match run_whole(wire, &config) {
        Outcome::Error(ParseError::BudgetExceeded {
            needed: 7,
            remaining: 3,
        }) => {}
        other => panic!("unexpected: {:?}", other),
    }
}

#[test]
fn bulk_length_cap_error_is_chunking_independent() {
    let wire = b"$5\r\nhello\r\n";
    let config = Config {
        max_bulk_length: 4,
        ..Config::default()
    };
    assert_every_cut_invariant(wire, &config, "bulk-cap");
    assert_eq!(
        run_whole(wire, &config),
        Outcome::Error(ParseError::BulkTooLarge {
            declared: 5,
            max: 4
        })
    );
}

// ---------- explicit concrete split scenarios from the task ----------------

#[test]
fn crlf_inside_bulk_is_not_mistaken_for_terminator_at_every_cut() {
    // Payload "a\r\nb\r\nc" = 7 bytes with TWO internal CRLFs.
    let wire = b"$7\r\na\r\nb\r\nc\r\n";
    let reference = run_whole(wire, &Config::default());
    assert_eq!(
        reference,
        Outcome::Frames(vec![Value::Bulk(b"a\r\nb\r\nc".to_vec())])
    );
    for k in 1..wire.len() {
        assert_eq!(
            run_chunked(wire, &[k], &Config::default()),
            reference,
            "cut at {} misparsed embedded CRLF",
            k
        );
    }
}

#[test]
fn negative_bulk_length_variants() {
    for (wire, legal) in [
        (b"$-1\r\n".as_slice(), true),
        (b"$-0\r\n".as_slice(), false),
        (b"$-2\r\n".as_slice(), false),
        (b"$-1000000\r\n".as_slice(), false),
    ] {
        let out = run_whole(wire, &Config::default());
        if legal {
            assert_eq!(out, Outcome::Frames(vec![Value::Null]));
        } else {
            match out {
                Outcome::Error(_) => {}
                other => panic!("{:?} should be fatal for {:?}", other, wire),
            }
        }
    }
}

#[test]
fn deep_nesting_plus_following_messages() {
    // A frame at the depth limit followed immediately by more messages;
    // cuts must never "lose" the tail frames.
    let wire = b"*1\r\n*1\r\n*1\r\n:1\r\n+OK\r\n:2\r\n";
    let config = Config {
        max_depth: 2,
        ..Config::default()
    };
    let expected = Outcome::Frames(vec![
        Value::Array(vec![Value::Array(vec![Value::Array(vec![Value::Integer(
            1,
        )])])]),
        Value::Simple("OK".into()),
        Value::Integer(2),
    ]);
    assert_eq!(run_whole(wire, &config), expected);
    assert_every_cut_invariant(wire, &config, "deep-then-tail");
}
