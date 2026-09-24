//! Integration tests for the incremental parser: real encoder, random chunking,
//! sticky/half packets, magic-in-payload, truncation, oversize declarations,
//! continuous noise, CRC corruption, sequence wraparound and memory bounds.

use serialframe::parser::{
    encode_frame, Event, FeedError, Frame, Parser, HEADER_LEN, MAGIC0, MAGIC1, TRAILER_LEN,
};

/// Deterministic PRNG (SplitMix64-style LCG) — no external test deps.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed)
    }
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0 ^ (self.0 >> 33)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n as u64) as usize
    }
}

fn collect_frames(events: &[Event]) -> Vec<Frame> {
    events
        .iter()
        .filter_map(|e| match e {
            Event::Frame(f) => Some(f.clone()),
            _ => None,
        })
        .collect()
}

fn feed_all(p: &mut Parser, chunk: &[u8]) -> Vec<Event> {
    // The parser contract rejects a single chunk larger than its hard bound;
    // feed in bound-sized slices so arbitrarily large inputs are still testable.
    let mut events = Vec::new();
    let step = p.hard_bound().max(1);
    for slice in chunk.chunks(step) {
        events.extend(p.feed(slice).expect("feed must not fail for in-bound chunks"));
    }
    events
}

/// The central property test: a stream of randomly-sized frames (payloads may
/// contain magic bytes), split into random chunks, must decode to exactly the
/// original frames — with no event other than Frame (and possibly Gap-free
/// in-order delivery).
#[test]
fn random_chunking_roundtrip() {
    let mut rng = Rng::new(0x5EED_1234);
    for trial in 0..200 {
        // Build a random stream of frames, remembering frame-boundary offsets
        // (noise may only be injected at boundaries — noise *inside* a frame
        // legitimately destroys that frame's CRC).
        let n_frames = 1 + rng.below(8);
        let mut expected = Vec::new();
        let mut stream = Vec::new();
        let mut boundaries = vec![0usize];
        for i in 0..n_frames {
            let seq = i as u16;
            let len = rng.below(300);
            let mut payload = Vec::with_capacity(len);
            for _ in 0..len {
                // Bias toward magic bytes to stress resync.
                payload.push(match rng.below(8) {
                    0 => MAGIC0,
                    1 => MAGIC1,
                    _ => rng.next_u64() as u8,
                });
            }
            stream.extend_from_slice(&encode_frame(seq, &payload).unwrap());
            expected.push(Frame::new(seq, payload));
            boundaries.push(stream.len());
        }

        // Random chunking, sometimes including noise between chunks — but only
        // when the feed position sits exactly on a frame boundary.
        let mut parser = Parser::new();
        let mut got = Vec::new();
        let mut pos = 0;
        while pos < stream.len() {
            let step = 1 + rng.below(64);
            let end = (pos + step).min(stream.len());
            got.extend(feed_all(&mut parser, &stream[pos..end]));
            pos = end;
            if boundaries.contains(&pos) && rng.below(4) == 0 {
                // interleave a noise burst at a frame boundary
                let noise_len = rng.below(16);
                let noise: Vec<u8> = (0..noise_len)
                    .map(|_| match rng.below(4) {
                        0 => MAGIC0, // single 0xA5s are legal noise
                        _ => rng.next_u64() as u8,
                    })
                    .collect();
                got.extend(feed_all(&mut parser, &noise));
            }
        }
        assert_eq!(
            collect_frames(&got),
            expected,
            "trial {trial}: decoded frames differ from encoded frames"
        );
        // In-order sequence => no gap events expected.
        assert!(
            !got.iter().any(|e| matches!(e, Event::Gap { .. })),
            "trial {trial}: unexpected gap in in-order stream"
        );
    }
}

#[test]
fn payload_containing_magic_and_frame_like_bytes() {
    // Payload contains a full *valid-looking* inner frame (real encoder output).
    let inner = encode_frame(99, b"nested").unwrap();
    let outer = encode_frame(1, &inner).unwrap();
    let mut p = Parser::new();
    let events = feed_all(&mut p, &outer);
    assert_eq!(collect_frames(&events), vec![Frame::new(1, inner)]);
}

#[test]
fn truncation_waits_then_completes() {
    let wire = encode_frame(7, b"truncate me").unwrap();
    let mut p = Parser::new();
    // Feed everything but the last byte.
    let ev = feed_all(&mut p, &wire[..wire.len() - 1]);
    assert!(ev.is_empty(), "truncated frame must not be delivered");
    assert_eq!(p.buffered_len(), wire.len() - 1);
    // Final byte completes it.
    let ev = feed_all(&mut p, &wire[wire.len() - 1..]);
    assert_eq!(collect_frames(&ev), vec![Frame::new(7, b"truncate me".to_vec())]);
}

#[test]
fn oversize_declaration_rejected_before_allocation_and_resyncs() {
    let mut p = Parser::with_max_payload(64);
    let mut stream = Vec::new();
    stream.push(MAGIC0);
    stream.push(MAGIC1);
    stream.extend_from_slice(&0xFFFFu16.to_be_bytes()); // declared 65535 > 64
    stream.extend_from_slice(&0xBEEFu16.to_be_bytes()); // bogus seq
    stream.extend_from_slice(&[0x11; 32]); // junk belonging to the bogus frame
    stream.extend_from_slice(&encode_frame(3, b"real").unwrap());

    let events = feed_all(&mut p, &stream);
    assert!(
        events
            .iter()
            .any(|e| matches!(e, Event::Oversize { declared: 65535, max: 64 })),
        "expected oversize event, got {events:?}"
    );
    assert_eq!(collect_frames(&events), vec![Frame::new(3, b"real".to_vec())]);
    // Buffer never exceeded its hard bound; nothing oversized was allocated.
    assert!(p.buffered_len() <= p.hard_bound());
}

#[test]
fn bad_crc_does_not_swallow_following_valid_frame() {
    let mut bad = encode_frame(1, b"payload").unwrap();
    bad[7] ^= 0x01; // corrupt one payload byte
    let good = encode_frame(2, b"intact").unwrap();
    let mut stream = bad.clone();
    stream.extend_from_slice(&good);

    let mut p = Parser::new();
    let events = feed_all(&mut p, &stream);
    assert!(
        events
            .iter()
            .any(|e| matches!(e, Event::CrcMismatch { seq: 1, .. })),
        "expected crc_mismatch, got {events:?}"
    );
    assert_eq!(collect_frames(&events), vec![Frame::new(2, b"intact".to_vec())]);
}

#[test]
fn bad_crc_where_corruption_hides_a_magic() {
    // A corrupt frame whose *payload* contains magic bytes: resync must not
    // lock onto the inner magic and must still find the trailing valid frame.
    let mut bad = encode_frame(1, &[MAGIC0, MAGIC1, 0, 1, 0xAA]).unwrap();
    let last = bad.len() - 1;
    bad[last] ^= 0xFF; // corrupt CRC
    let good = encode_frame(2, b"after").unwrap();
    let mut stream = bad;
    stream.extend_from_slice(&good);

    let mut p = Parser::new();
    let events = feed_all(&mut p, &stream);
    assert_eq!(collect_frames(&events), vec![Frame::new(2, b"after".to_vec())]);
}

#[test]
fn continuous_noise_stays_bounded_and_resyncs() {
    let mut p = Parser::with_max_payload(128);
    let mut rng = Rng::new(42);
    // ~1 MiB of noise (some of it magic-looking), in random chunks.
    let mut total_noise = 0usize;
    for _ in 0..2048 {
        let n = 1 + rng.below(1024);
        let chunk: Vec<u8> = (0..n)
            .map(|_| match rng.below(6) {
                0 => MAGIC0,
                1 => MAGIC1,
                _ => rng.next_u64() as u8,
            })
            .collect();
        total_noise += n;
        let events = feed_all(&mut p, &chunk);
        assert!(
            !events.iter().any(|e| matches!(e, Event::Frame(_))),
            "noise must never decode as a frame"
        );
        assert!(
            p.buffered_len() <= p.hard_bound(),
            "buffer {} exceeded hard bound {}",
            p.buffered_len(),
            p.hard_bound()
        );
    }
    assert!(total_noise > 900_000);
    // Now a valid frame must still be decoded immediately.
    let wire = encode_frame(5, b"post-noise").unwrap();
    let events = feed_all(&mut p, &wire);
    assert_eq!(collect_frames(&events), vec![Frame::new(5, b"post-noise".to_vec())]);
}

#[test]
fn sequence_gap_and_wraparound_reporting() {
    let mut p = Parser::new();
    let mut events = Vec::new();
    for seq in [65533u16, 65534, 65535, 0, 2, 3] {
        events.extend(feed_all(&mut p, &encode_frame(seq, b"x").unwrap()));
    }
    // Exactly one gap: seq 1 was skipped between 0 and 2.
    let gaps: Vec<_> = events
        .iter()
        .filter_map(|e| match e {
            Event::Gap { from, to, count } => Some((*from, *to, *count)),
            _ => None,
        })
        .collect();
    assert_eq!(gaps, vec![(1, 2, 1)]);
    assert_eq!(collect_frames(&events).len(), 6);
    assert_eq!(p.expected_seq(), Some(4));
}

#[test]
fn sequence_rewind_is_reported_and_expectation_kept() {
    let mut p = Parser::new();
    feed_all(&mut p, &encode_frame(10, b"a").unwrap());
    let events = feed_all(&mut p, &encode_frame(3, b"old").unwrap());
    assert!(
        events
            .iter()
            .any(|e| matches!(e, Event::SeqRewind { received: 3, expected_seq: 11 })),
        "expected seq_rewind, got {events:?}"
    );
    // The frame is still delivered, expectation not moved.
    assert_eq!(collect_frames(&events), vec![Frame::new(3, b"old".to_vec())]);
    assert_eq!(p.expected_seq(), Some(11));
    // Next in-order frame produces no gap.
    let events = feed_all(&mut p, &encode_frame(11, b"b").unwrap());
    assert!(!events.iter().any(|e| matches!(e, Event::Gap { .. })));
}

#[test]
fn wraparound_gap_count_is_exact() {
    let mut p = Parser::new();
    feed_all(&mut p, &encode_frame(65534, b"a").unwrap());
    // Skip 65535 and 0 entirely: receive 1 next.
    let events = feed_all(&mut p, &encode_frame(1, b"b").unwrap());
    let gap = events.iter().find_map(|e| match e {
        Event::Gap { from, to, count } => Some((*from, *to, *count)),
        _ => None,
    });
    assert_eq!(gap, Some((65535, 1, 2)));
}

#[test]
fn chunk_larger_than_hard_bound_is_rejected_not_consumed() {
    let mut p = Parser::with_max_payload(64);
    let big = vec![0u8; p.hard_bound() + 1];
    match p.feed(&big) {
        Err(FeedError::ChunkTooLarge { len, bound }) => {
            assert_eq!(len, p.hard_bound() + 1);
            assert_eq!(bound, p.hard_bound());
        }
        other => panic!("expected ChunkTooLarge, got {other:?}"),
    }
    assert_eq!(p.buffered_len(), 0, "rejected chunk must not be buffered");
    // Parser still works afterwards.
    let wire = encode_frame(1, b"ok").unwrap();
    let events = feed_all(&mut p, &wire);
    assert_eq!(collect_frames(&events), vec![Frame::new(1, b"ok".to_vec())]);
}

#[test]
fn max_payload_boundary() {
    let mut p = Parser::with_max_payload(100);
    let ok = encode_frame(1, &[0xAB; 100]).unwrap();
    let events = feed_all(&mut p, &ok);
    assert_eq!(collect_frames(&events).len(), 1);

    let mut p2 = Parser::with_max_payload(100);
    let too_big = encode_frame(1, &[0xAB; 101]).unwrap();
    let events = feed_all(&mut p2, &too_big);
    assert!(events
        .iter()
        .any(|e| matches!(e, Event::Oversize { declared: 101, max: 100 })));
    assert!(collect_frames(&events).is_empty());
}

#[test]
fn buffer_never_exceeds_hard_bound_across_random_feeds() {
    let mut rng = Rng::new(0xB0B);
    let mut p = Parser::with_max_payload(256);
    let mut stream = Vec::new();
    // Mixed valid frames + noise + corrupt frames.
    for i in 0..50u16 {
        stream.extend_from_slice(&encode_frame(i, &vec![i as u8; 1 + (i as usize % 200)]).unwrap());
        if i % 3 == 0 {
            stream.extend_from_slice(&[0x00, 0xFF, MAGIC0, 0x12]);
        }
        if i % 5 == 0 {
            let mut bad = encode_frame(i, b"corrupt").unwrap();
            bad[6] ^= 0x10;
            stream.extend_from_slice(&bad);
        }
    }
    let mut pos = 0;
    while pos < stream.len() {
        let step = 1 + rng.below(37);
        let end = (pos + step).min(stream.len());
        p.feed(&stream[pos..end]).unwrap();
        assert!(p.buffered_len() <= p.hard_bound());
        pos = end;
    }
}

#[test]
fn half_magic_at_chunk_boundary() {
    let wire = encode_frame(9, b"boundary").unwrap();
    let mut p = Parser::new();
    // Feed up to and including the first magic byte only.
    let ev = feed_all(&mut p, &wire[..1]);
    assert!(ev.is_empty());
    assert_eq!(p.buffered_len(), 1);
    let ev = feed_all(&mut p, &wire[1..]);
    assert_eq!(collect_frames(&ev), vec![Frame::new(9, b"boundary".to_vec())]);
}

#[test]
fn empty_feed_is_noop() {
    let mut p = Parser::new();
    assert!(p.feed(&[]).unwrap().is_empty());
}

#[test]
fn frame_sizes_accounting() {
    let wire = encode_frame(0, &[1, 2, 3]).unwrap();
    assert_eq!(wire.len(), HEADER_LEN + 3 + TRAILER_LEN);
}
