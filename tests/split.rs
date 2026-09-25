//! Split-position acceptance tests.
//!
//! For **every** corpus entry and **every** byte position `k`
//! (`0..=input.len()`), the input is fed to a fresh framer as two
//! pieces `[0,k)` then `[k,n)`. The outcome must be identical to
//! feeding the input whole — same completed request count/bodies for
//! valid streams, same [`ErrorKind`] for invalid streams.
//!
//! Additional schedules (one-byte feeds; an irregular multi-piece
//! pattern) cover cases a single cut cannot.

mod common;

use common::{
    cases, drive_collect, byte_schedule, irregular_schedule, split_schedule, whole_schedule,
    Outcome,
};
use http_framing::ErrorKind;

fn assert_same_as_whole(
    name: &str,
    input: &[u8],
    outcome: &Outcome,
    limits: &http_framing::Limits,
    schedule: Vec<usize>,
) {
    let whole = drive_collect(input, &whole_schedule(input.len()), limits.clone());
    let split = drive_collect(input, &schedule, limits.clone());
    match (outcome, whole, split) {
        (Outcome::Valid, Ok(w), Ok(s)) => {
            assert_eq!(
                w.len(),
                s.len(),
                "{name}: request count differs for schedule {:?}",
                summarize(&schedule)
            );
            for (a, b) in w.iter().zip(s.iter()) {
                assert_eq!(
                    a, b,
                    "{name}: framed request differs for schedule {:?}",
                    summarize(&schedule)
                );
            }
        }
        (Outcome::Invalid(kind), Err(wk), Err(sk)) => {
            assert_eq!(wk, *kind, "{name}: whole feed gave wrong kind");
            assert_eq!(
                sk, *kind,
                "{name}: split gave {sk:?}, expected {kind:?}; schedule {:?}",
                summarize(&schedule)
            );
        }
        (Outcome::Valid, Err(e), _) => {
            panic!("{name}: corpus mislabelled valid; whole feed errored {e:?}");
        }
        (Outcome::Invalid(kind), Ok(reqs), _) => {
            panic!(
                "{name}: corpus mislabelled invalid ({kind:?}); whole feed produced {} requests",
                reqs.len()
            );
        }
        (Outcome::Valid, Ok(_), Err(e)) => {
            panic!(
                "{name}: split feeding changed outcome to error {e:?}; schedule {:?}",
                summarize(&schedule)
            );
        }
        (Outcome::Invalid(kind), Err(wk), Ok(reqs)) => {
            panic!(
                "{name}: whole feed rejected ({wk:?}) but split accepted {} requests \
                 (expected {kind:?}); schedule {:?}",
                reqs.len(),
                summarize(&schedule)
            );
        }
    }
}

fn summarize(s: &[usize]) -> String {
    if s.len() > 12 {
        format!("{} pieces, first {:?}...", s.len(), &s[..8])
    } else {
        format!("{s:?}")
    }
}

#[test]
fn every_byte_position_split() {
    let mut positions = 0usize;
    for case in cases() {
        let n = case.input.len();
        for k in 0..=n {
            assert_same_as_whole(
                case.name,
                &case.input,
                &case.outcome,
                &case.limits,
                split_schedule(n, k),
            );
            positions += 1;
        }
    }
    eprintln!("validated {positions} byte-cut positions across the corpus");
}

#[test]
fn one_byte_at_a_time_matches() {
    for case in cases() {
        assert_same_as_whole(
            case.name,
            &case.input,
            &case.outcome,
            &case.limits,
            byte_schedule(case.input.len()),
        );
    }
}

#[test]
fn irregular_pieces_match() {
    for case in cases() {
        assert_same_as_whole(
            case.name,
            &case.input,
            &case.outcome,
            &case.limits,
            irregular_schedule(case.input.len()),
        );
    }
}

#[test]
fn whole_feed_outcomes_are_labelled_correctly() {
    // Belt-and-braces: a pure outcome check with explicit listing so a
    // mislabelled corpus entry shows up even if a comparison helper
    // above ever softened.
    for case in cases() {
        let got = drive_collect(
            &case.input,
            &whole_schedule(case.input.len()),
            case.limits.clone(),
        );
        match (&case.outcome, got) {
            (Outcome::Valid, Ok(_)) => {}
            (Outcome::Invalid(k), Err(actual)) => assert_eq!(
                actual, *k,
                "case {} expected {k:?}",
                case.name
            ),
            (Outcome::Valid, Err(e)) => panic!("{} unexpectedly errored: {e:?}", case.name),
            (Outcome::Invalid(k), Ok(_)) => {
                panic!("{} expected error {k:?} but framed", case.name)
            }
        }
    }
}

#[test]
fn all_error_kinds_exercised_except_none() {
    // The corpus should touch every variant; new variants added to
    // ErrorKind must be consciously classified here.
    use ErrorKind::*;
    let mut covered = std::collections::HashSet::new();
    for case in cases() {
        if let Outcome::Invalid(k) = case.outcome {
            covered.insert(k);
        }
    }
    let all = [
        BadLineEnding,
        MalformedRequestLine,
        InvalidMethod,
        InvalidTarget,
        UnsupportedVersion,
        HeaderMissingColon,
        InvalidHeaderName,
        InvalidHeaderValue,
        ObsoleteLineFolding,
        AmbiguousWhitespace,
        DuplicateContentLength,
        InvalidContentLength,
        InvalidTransferEncoding,
        TeWithContentLength,
        ChunkSizeInvalid,
        ChunkTooLarge,
        ChunkTerminator,
        TrailerForbiddenField,
        RequestLineTooLarge,
        HeadersTooLarge,
        BodyTooLarge,
        Incomplete,
    ];
    let missing: Vec<_> = all.iter().filter(|k| !covered.contains(k)).collect();
    assert!(
        missing.is_empty(),
        "corpus never exercises error kinds: {missing:?}"
    );
}
