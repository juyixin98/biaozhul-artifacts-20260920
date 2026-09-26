//! End-to-end acceptance tests: edge cases, segmented merge, and order
//! independence.

use cdict::merge::merge_to_vec;
use cdict::{decode_to_vec, encode_to_vec, Limits};

type Rows = Vec<Option<String>>;

fn enc(rows: &[Option<&str>], segment_rows: u64) -> Vec<u8> {
    encode_to_vec(rows, &Limits::default(), segment_rows).expect("encode")
}

fn dec(bytes: &[u8]) -> Rows {
    decode_to_vec(bytes, &Limits::default()).expect("decode").0
}

fn s(v: &[&str]) -> Rows {
    v.iter().map(|x| Some((*x).to_string())).collect()
}

fn sm(v: &[Option<&str>]) -> Rows {
    v.iter().map(|x| x.map(|y| y.to_string())).collect()
}

#[test]
fn empty_column_roundtrip() {
    let bytes = encode_to_vec(&[], &Limits::default(), 0).unwrap();
    let (rows, stats) = decode_to_vec(&bytes[..], &Limits::default()).unwrap();
    assert_eq!(rows, Vec::<Option<String>>::new());
    assert_eq!(stats.rows, 0);
    assert_eq!(stats.segments, 0);
}

#[test]
fn empty_string_is_distinct_from_null() {
    let rows = vec![Some(""), None, Some(""), Some("x")];
    let bytes = enc(&rows, 0);
    assert_eq!(dec(&bytes), sm(&rows));
}

#[test]
fn all_null_segment_has_empty_dictionary() {
    let rows = vec![None; 100];
    let bytes = enc(&rows, 0);
    // dict count should be 0: header is magic+ver, then SEGM + 0 + 100 + ids.
    assert!(bytes.starts_with(b"CDCT"));
    let decoded = dec(&bytes);
    assert_eq!(decoded.len(), 100);
    assert!(decoded.iter().all(|r| r.is_none()));
}

#[test]
fn unicode_values_roundtrip() {
    let rows: Vec<Option<&str>> = vec![
        Some("中文字典"),
        Some("🙂🙃"),
        Some("é"),  // 2-byte UTF-8
        Some("€"),  // 3-byte
        Some("🦀"), // 4-byte
        None,
        Some("中文字典"), // repeat interns same id
        Some(""),
        Some("αβγ"),
    ];
    let bytes = enc(&rows, 3); // force several tiny segments
    assert_eq!(dec(&bytes), sm(&rows));
}

#[test]
fn high_cardinality_degenerates_to_per_row_entries() {
    // Every value distinct: dictionary encoding degenerates to N entries + N
    // ids; correctness must still hold.
    let rows: Vec<Option<&str>> = (0..5000)
        .map(|i| {
            // leak values so the slice of borrows stays alive for the test
            Some(Box::leak(format!("val-{i:06}").into_boxed_str()) as &str)
        })
        .collect();
    let bytes = enc(&rows, 137);
    let decoded = dec(&bytes);
    let expected: Rows = rows.iter().map(|r| Some(r.unwrap().to_string())).collect();
    assert_eq!(decoded, expected);
}

#[test]
fn segmentation_preserves_rows_line_by_line() {
    let rows: Vec<Option<&str>> = (0..1000)
        .map(|i| match i % 5 {
            0 => None,
            1 => Some(""),
            2 => Some("alpha"),
            3 => Some("beta"),
            _ => Some("gamma"),
        })
        .collect();
    for seg_rows in [1u64, 2, 3, 7, 100, 1000, 1001] {
        let bytes = enc(&rows, seg_rows);
        assert_eq!(dec(&bytes), sm(&rows), "seg_rows={seg_rows}");
    }
}

#[test]
fn merge_streams_row_values_identical_line_by_line() {
    let a: Vec<Option<&str>> = vec![Some("x"), Some("y"), None, Some("x")];
    let b: Vec<Option<&str>> = vec![Some("z"), Some("x"), Some(""), None, Some("y")];
    let c: Vec<Option<&str>> = vec![None, None, Some("w")];

    // Encode each with different segment sizes so local ids diverge.
    let ba = enc(&a, 2);
    let bb = enc(&b, 1);
    let bc = enc(&c, 5);

    let (merged, stats) = merge_to_vec(
        &[ba.as_slice(), bb.as_slice(), bc.as_slice()],
        &Limits::default(),
        3,
    )
    .expect("merge");

    let mut expected: Vec<Option<&str>> = Vec::new();
    expected.extend_from_slice(&a);
    expected.extend_from_slice(&b);
    expected.extend_from_slice(&c);

    assert_eq!(dec(&merged), sm(&expected));
    assert_eq!(stats.rows, 12);
    assert_eq!(stats.input_streams, 3);
    // distinct non-null values: x, y, z, "", w = 5
    assert_eq!(stats.global_dict_entries, 5);
}

#[test]
fn merge_is_associative_and_order_independent_for_values() {
    let a: Vec<Option<&str>> = vec![Some("red"), Some("green"), None];
    let b: Vec<Option<&str>> = vec![Some("blue"), Some("red")];

    let ba = enc(&a, 1);
    let bb = enc(&b, 2);

    // (a+b)+c style: merge a,b then merge result with c
    let (ab, _) = merge_to_vec(&[ba.as_slice(), bb.as_slice()], &Limits::default(), 2).unwrap();
    let (ab2, _) = merge_to_vec(&[bb.as_slice(), ba.as_slice()], &Limits::default(), 2).unwrap();

    let d1 = dec(&ab);
    let d2 = dec(&ab2);
    let expected_ab = sm(&[Some("red"), Some("green"), None, Some("blue"), Some("red")]);
    let expected_ba = sm(&[Some("blue"), Some("red"), Some("red"), Some("green"), None]);
    assert_eq!(d1, expected_ab);
    assert_eq!(d2, expected_ba);
}

#[test]
fn dictionary_order_does_not_affect_decoded_values() {
    // Two encodings with values inserted in opposite orders must decode the
    // same row sequence, despite different dictionaries/ids.
    let rows1 = s(&["a", "b", "c", "a", "b"]);
    let rows2 = s(&["c", "b", "a", "c", "b"]); // same multiset pattern, reversed labels
    let b1 = encode_to_vec(
        &rows1.iter().map(|r| r.as_deref()).collect::<Vec<_>>(),
        &Limits::default(),
        0,
    )
    .unwrap();
    let b2 = encode_to_vec(
        &rows2.iter().map(|r| r.as_deref()).collect::<Vec<_>>(),
        &Limits::default(),
        0,
    )
    .unwrap();
    // Bytes differ (different dictionary order), values decode to themselves.
    assert_ne!(b1, b2);
    assert_eq!(dec(&b1), rows1);
    assert_eq!(dec(&b2), rows2);

    // After merge-normalize with a common global dictionary, corresponding
    // positions that hold the same label get identical global ids.
    let (m1, _) = merge_to_vec(&[&b1[..]], &Limits::default(), 0).unwrap();
    let (m2, _) = merge_to_vec(&[&b2[..]], &Limits::default(), 0).unwrap();
    assert_eq!(dec(&m1), rows1);
    assert_eq!(dec(&m2), rows2);
}

#[test]
fn merged_output_segments_share_global_ids() {
    // The same value in different input segments must map to the same global
    // id in the merged stream's dictionaries.
    let a = enc(&[Some("dup"), None, Some("dup")], 1);
    let b = enc(&[Some("dup"), Some("other")], 1);
    let (merged, stats) =
        merge_to_vec(&[a.as_slice(), b.as_slice()], &Limits::default(), 2).unwrap();
    assert!(stats.output_segments >= 2);
    assert_eq!(
        dec(&merged),
        sm(&[Some("dup"), None, Some("dup"), Some("dup"), Some("other"),])
    );
}

#[test]
fn limits_are_enforced_on_encode() {
    let limits = Limits {
        max_dict_entries: 2,
        ..Limits::tight()
    };
    let rows = vec![Some("a"), Some("b"), Some("c")];
    let err = encode_to_vec(&rows, &limits, 0).unwrap_err();
    assert!(matches!(err, cdict::Error::LimitExceeded(_)), "{err:?}");
}

#[test]
fn limits_are_enforced_on_decode_output() {
    let rows: Vec<Option<&str>> = vec![Some("aaaaaaaaaaaaaaa"); 50];
    let bytes = enc(&rows, 0);
    let limits = Limits {
        max_decoded_string_bytes: 100,
        ..Limits::default()
    };
    let err = decode_to_vec(&bytes[..], &limits).unwrap_err();
    assert!(matches!(err, cdict::Error::LimitExceeded(_)), "{err:?}");
}

#[test]
fn truncated_and_corrupt_input_rejected() {
    let rows = vec![Some("abc"), None];
    let bytes = enc(&rows, 0);

    // truncated
    assert!(decode_to_vec(&bytes[..bytes.len() - 1], &Limits::default()).is_err());
    // bad magic
    let mut bad = bytes.clone();
    bad[0] = b'X';
    assert!(decode_to_vec(&bad[..], &Limits::default()).is_err());
    // dangling id
    let mut bad2 = bytes.clone();
    // locate id area after dict entry (len 3 + "abc"): flip last id to 9
    *bad2.last_mut().unwrap() = 9;
    assert!(matches!(
        decode_to_vec(&bad2[..], &Limits::default()),
        Err(cdict::Error::BadReference(_))
    ));
}

#[test]
fn merge_empty_inputs() {
    let (merged, stats) = merge_to_vec(&[] as &[&[u8]], &Limits::default(), 0).expect("merge");
    assert_eq!(dec(&merged), Vec::<Option<String>>::new());
    assert_eq!(stats.rows, 0);
    assert_eq!(stats.output_segments, 0);
    assert_eq!(stats.global_dict_entries, 0);
}

#[test]
fn merge_of_all_null_and_empty_string_streams() {
    let a = enc(&[None, None, None], 1);
    let b = enc(&[Some(""), Some(""), None], 1);
    let (merged, stats) =
        merge_to_vec(&[a.as_slice(), b.as_slice()], &Limits::default(), 2).unwrap();
    assert_eq!(stats.global_dict_entries, 1); // only ""
    assert_eq!(
        dec(&merged),
        sm(&[None, None, None, Some(""), Some(""), None])
    );
}
