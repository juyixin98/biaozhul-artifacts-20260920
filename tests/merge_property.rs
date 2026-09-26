//! Focused property-style tests for the merge internals and streaming APIs.

use cdict::decoder::Decoder;
use cdict::encoder::Encoder;
use cdict::merge::Merger;
use cdict::segment::NULL_ID;
use cdict::Limits;

fn limits() -> Limits {
    Limits::default()
}

#[test]
fn streaming_encoder_decoder_match_materialized() {
    // Encode row-by-row into an in-memory writer; decode row-by-row.
    let source: Vec<Option<String>> = (0..2000)
        .map(|i| match i % 7 {
            0 => None,
            1 => Some(String::new()),
            2 => Some(format!("v{}", i % 50)),
            _ => Some(format!("fixed-{}", i % 5)),
        })
        .collect();

    let mut buf: Vec<u8> = Vec::new();
    {
        let mut enc = Encoder::new(&mut buf, limits(), 128);
        for row in &source {
            enc.push_row(row.as_deref()).unwrap();
        }
        let stats = enc.finish().unwrap();
        assert_eq!(stats.rows, 2000);
        assert!(stats.segments >= 15);
    }

    let mut dec = Decoder::new(&buf[..], limits()).unwrap();
    let mut got = Vec::new();
    while let Some(row) = dec.next_row().unwrap() {
        got.push(row.map(str::to_string));
    }
    let stats = dec.finish();
    assert_eq!(stats.rows, 2000);
    assert_eq!(got, source);
}

#[test]
fn cross_segment_remapping_uses_same_global_id() {
    // Build two streams where the shared value "S" has DIFFERENT local ids,
    // then verify the merged id bytes are identical for every "S" row.
    let mut a: Vec<u8> = Vec::new();
    {
        // dict order: ["zebra", "S"] -> S local id 2
        let mut e = Encoder::new(&mut a, limits(), 0);
        for v in ["zebra", "S", "zebra"] {
            e.push_row(Some(v)).unwrap();
        }
        e.finish().unwrap();
    }
    let mut b: Vec<u8> = Vec::new();
    {
        // dict order: ["S", "mango"] -> S local id 1
        let mut e = Encoder::new(&mut b, limits(), 0);
        for v in ["S", "mango"] {
            e.push_row(Some(v)).unwrap();
        }
        e.finish().unwrap();
    }

    let mut out: Vec<u8> = Vec::new();
    let mut merger = Merger::new(limits(), 0);
    merger.begin(&mut out).unwrap();
    merger.add_stream(&mut &a[..], &mut out).unwrap();
    merger.add_stream(&mut &b[..], &mut out).unwrap();
    merger.finish(&mut out).unwrap();

    // First-seen global order: zebra(1), S(2), mango(3).
    let mut dec = Decoder::new(&out[..], limits()).unwrap();
    let mut rows = Vec::new();
    while let Some(r) = dec.next_row().unwrap() {
        rows.push(r.map(str::to_string));
    }
    assert_eq!(
        rows.iter().map(|r| r.as_deref()).collect::<Vec<_>>(),
        vec![
            Some("zebra"),
            Some("S"),
            Some("zebra"),
            Some("S"),
            Some("mango"),
        ]
    );
}

#[test]
fn null_is_always_id_zero() {
    assert_eq!(NULL_ID, 0);
}

#[test]
fn empty_streams_merge_then_decode_is_empty() {
    let mut out: Vec<u8> = Vec::new();
    let merger = Merger::new(limits(), 10);
    merger.begin(&mut out).unwrap();
    // zero input streams
    let stats = merger.finish(&mut out).unwrap();
    assert_eq!(stats.rows, 0);
    let mut dec = Decoder::new(&out[..], limits()).unwrap();
    assert!(dec.next_row().unwrap().is_none());
}

#[test]
fn decoder_rejects_segment_count_limit() {
    // 10 rows, one per segment; cap segments at 3 -> must fail on segment 4.
    let source: Vec<Option<String>> = (0..10).map(|i| Some(format!("v{i}"))).collect();
    let mut buf = Vec::new();
    {
        let mut e = Encoder::new(&mut buf, Limits::default(), 1);
        for row in &source {
            e.push_row(row.as_deref()).unwrap();
        }
        e.finish().unwrap();
    }
    let tight = Limits {
        max_segments: 3,
        ..Limits::default()
    };
    let mut dec = Decoder::new(&buf[..], tight).unwrap();
    let mut err = None;
    loop {
        match dec.next_row() {
            Ok(Some(_)) => continue,
            Ok(None) => break,
            Err(e) => {
                err = Some(e);
                break;
            }
        }
    }
    assert!(matches!(err, Some(cdict::Error::LimitExceeded(_))));
}
