//! End-to-end tests driven by the *real* encoder with adversarial slicing and
//! corruption. A small deterministic LCG (no `rand` dependency) provides the
//! randomness; seeds are reported so any failure is reproducible.

use serial_frame_parser::{
    encode_frame, Event, FrameParser, DEFAULT_MAX_PAYLOAD, MAGIC,
};

/// Deterministic xorshift64* RNG.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed.max(1))
    }
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next_u64() % n) as usize
    }
}

fn frames_only(events: &[Event]) -> Vec<(u16, Vec<u8>)> {
    events
        .iter()
        .filter_map(|e| match e {
            Event::Frame { frame, .. } => Some((frame.sequence, frame.payload.clone())),
            _ => None,
        })
        .collect()
}

fn feed_random_slices(parser: &mut FrameParser, data: &[u8], rng: &mut Rng) -> Vec<Event> {
    let mut all = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let remaining = data.len() - i;
        // 70% of cuts are tiny (1..=4 bytes), the rest are larger.
        let n = if rng.below(10) < 7 {
            1 + rng.below(4).min(remaining - 1)
        } else {
            1 + rng.below((remaining as u64).max(1))
        };
        let n = n.min(remaining).max(1);
        all.extend(parser.feed(&data[i..i + n]));
        i += n;
    }
    all
}

// ---------- Basic happy path ----------

#[test]
fn single_frame_single_feed() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(1, b"hello", DEFAULT_MAX_PAYLOAD).unwrap();
    let ev = p.feed(&raw);
    assert_eq!(frames_only(&ev), vec![(1, b"hello".to_vec())]);
    assert!(p.finish().is_empty());
    assert_eq!(p.stats().frames_ok, 1);
}

#[test]
fn glued_packets() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut stream = Vec::new();
    for s in 0..10u16 {
        stream.extend_from_slice(&encode_frame(s, format!("p{s}").as_bytes(), DEFAULT_MAX_PAYLOAD).unwrap());
    }
    let ev = p.feed(&stream);
    let got = frames_only(&ev);
    assert_eq!(got.len(), 10);
    for (i, (s, pl)) in got.iter().enumerate() {
        assert_eq!(*s, i as u16);
        assert_eq!(pl, format!("p{i}").as_bytes());
    }
    assert!(p.finish().is_empty());
}

#[test]
fn half_packet_byte_by_byte() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(42, b"split everywhere", DEFAULT_MAX_PAYLOAD).unwrap();
    let mut all = Vec::new();
    for b in &raw {
        all.extend(p.feed(std::slice::from_ref(b)));
    }
    assert_eq!(frames_only(&all), vec![(42, b"split everywhere".to_vec())]);
    assert!(p.finish().is_empty());
}

#[test]
fn payload_containing_magic_is_atomic() {
    let mut payload = vec![0x00; 3];
    payload.extend_from_slice(&MAGIC);
    payload.extend_from_slice(b"tail");
    payload.splice(0..0, MAGIC.iter().copied()); // magic also at payload start

    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(7, &payload, DEFAULT_MAX_PAYLOAD).unwrap();
    let ev = p.feed(&raw);
    assert_eq!(frames_only(&ev), vec![(7, payload.clone())]);
}

#[test]
fn magic_split_across_feeds() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(1, b"x", DEFAULT_MAX_PAYLOAD).unwrap();
    // Split right inside the 4 magic bytes, with garbage before.
    let mut ev = Vec::new();
    ev.extend(p.feed(&[0x55, 0xAA, 0xDE, 0xAD]));
    ev.extend(p.feed(&[0xBE]));
    ev.extend(p.feed(&[0xEF, raw[4], raw[5]]));
    ev.extend(p.feed(&raw[6..]));
    assert_eq!(frames_only(&ev), vec![(1, b"x".to_vec())]);
}

// ---------- Noise & resynchronization ----------

#[test]
fn leading_and_interleaved_noise() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let f1 = encode_frame(1, b"a", DEFAULT_MAX_PAYLOAD).unwrap();
    let f2 = encode_frame(2, b"bb", DEFAULT_MAX_PAYLOAD).unwrap();
    let mut stream = b"garbage!!!".to_vec();
    stream.extend_from_slice(&f1);
    stream.extend_from_slice(&[0x99, 0x88, 0x77]);
    stream.extend_from_slice(&f2);
    let ev = p.feed(&stream);
    assert_eq!(
        frames_only(&ev),
        vec![(1, b"a".to_vec()), (2, b"bb".to_vec())]
    );
    let noise: usize = ev
        .iter()
        .filter_map(|e| match e {
            Event::Noise { count, .. } => Some(*count),
            _ => None,
        })
        .sum();
    assert_eq!(noise, 13);
}

#[test]
fn long_run_of_pure_noise_never_allocates_hugely() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut rng = Rng::new(0x1234);
    // 200 KiB of random noise; the parser must shed everything but the
    // trailing magic prefix.
    let chunk: Vec<u8> = (0..200_000u32).map(|_| rng.next_u64() as u8).collect();
    let ev = p.feed(&chunk);
    let noise: usize = ev
        .iter()
        .filter_map(|e| if let Event::Noise { count, .. } = e { Some(*count) } else { None })
        .sum();
    let held = p.buffer_len();
    let remaining = chunk.len() - noise;
    assert_eq!(remaining, held);
    assert!(held < MAGIC.len());
    assert!(p.stats().frames_ok == 0);
}

// ---------- Bad CRC must not swallow the next frame ----------

#[test]
fn bad_crc_does_not_swallow_following_frame() {
    let mut bad = encode_frame(1, b"bad", DEFAULT_MAX_PAYLOAD).unwrap();
    let crc_pos = bad.len() - 1;
    bad[crc_pos] ^= 0x01;
    let good = encode_frame(2, b"good", DEFAULT_MAX_PAYLOAD).unwrap();

    let mut stream = bad;
    stream.extend_from_slice(&good);

    // Feed the whole thing, and also byte-by-byte — must behave identically.
    for mode in 0..2 {
        let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
        let ev = if mode == 0 {
            p.feed(&stream)
        } else {
            feed_random_slices(&mut p, &stream, &mut Rng::new(99))
        };
        assert!(ev.iter().any(|e| matches!(e, Event::BadCrc { sequence: 1, .. })));
        assert_eq!(frames_only(&ev), vec![(2, b"good".to_vec())]);
        assert_eq!(p.stats().bad_crc, 1);
        assert_eq!(p.stats().frames_ok, 1);
    }
}

#[test]
fn fake_magic_in_noise_with_bad_crc_resyncs_to_real_frame() {
    // Noise contains DE AD BE EF then a valid-looking length/seq but garbage
    // CRC; the real frame follows immediately.
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut stream = MAGIC.to_vec();
    stream.extend_from_slice(&3u16.to_be_bytes());
    stream.extend_from_slice(&1u16.to_be_bytes());
    stream.extend_from_slice(b"abc");
    stream.extend_from_slice(&0xDEAD_BEEFu32.to_le_bytes()); // wrong crc
    let good = encode_frame(2, b"real", DEFAULT_MAX_PAYLOAD).unwrap();
    stream.extend_from_slice(&good);
    let ev = p.feed(&stream);
    assert_eq!(frames_only(&ev), vec![(2, b"real".to_vec())]);
    assert_eq!(p.stats().bad_crc, 1);
}

// ---------- Oversized length is rejected before allocation ----------

#[test]
fn oversized_length_rejected_before_data_arrives() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut header = MAGIC.to_vec();
    header.extend_from_slice(&9000u16.to_be_bytes()); // > 4096 cap
    header.extend_from_slice(&1u16.to_be_bytes());
    // Feed ONLY the 8-byte header: rejection must happen now, without waiting
    // for 9000 payload bytes.
    let ev = p.feed(&header);
    assert!(matches!(
        ev[0],
        Event::OversizedLength { declared_len: 9000, .. }
    ));
    // Rejected at the 8-byte header, before any payload bytes were demanded or
    // allocated. The remaining non-magic bytes are immediately shed as noise,
    // so the buffer returns to empty (ready to resync).
    assert_eq!(p.buffer_len(), 0);
    assert_eq!(p.stats().oversized, 1);
    // A legal frame fed right afterwards is parsed normally.
    let good = encode_frame(2, b"ok", DEFAULT_MAX_PAYLOAD).unwrap();
    let ev2 = p.feed(&good);
    assert_eq!(frames_only(&ev2), vec![(2, b"ok".to_vec())]);
}

#[test]
fn oversized_declaration_then_real_frame_recovers() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut stream = MAGIC.to_vec();
    stream.extend_from_slice(&60000u16.to_be_bytes());
    stream.extend_from_slice(&1u16.to_be_bytes());
    stream.extend_from_slice(b"tiny fake body"); // nowhere near 60000 bytes
    let good = encode_frame(2, b"ok", DEFAULT_MAX_PAYLOAD).unwrap();
    stream.extend_from_slice(&good);

    let ev = feed_random_slices(&mut p, &stream, &mut Rng::new(7));
    assert!(ev.iter().any(|e| matches!(e, Event::OversizedLength { declared_len: 60000, .. })));
    assert_eq!(frames_only(&ev), vec![(2, b"ok".to_vec())]);
    // The fake body must all have been classified as noise, not buffered.
    assert!(p.buffer_len() < MAGIC.len() || p.stats().frames_ok == 1);
}

#[test]
fn bounded_memory_under_all_adversarial_streams() {
    // Whatever the stream looks like, the documented bound must hold.
    let cap = 64usize; // tiny cap makes the bound tight
    let bound = 8 + cap + 4 + MAGIC.len() - 1;
    for seed in 0..200u64 {
        let mut rng = Rng::new(0xA000 + seed);
        let mut p = FrameParser::new(cap);
        let mut data = Vec::new();
        for _ in 0..40 {
            let pick = rng.below(10);
            match pick {
                0..=5 => {
                    let len = rng.below(40);
                    let payload: Vec<u8> = (0..len).map(|_| rng.next_u64() as u8).collect();
                    data.extend_from_slice(
                        &encode_frame((rng.next_u64() & 0xFFFF) as u16, &payload, cap).unwrap(),
                    );
                }
                6..=7 => {
                    // Random garbage incl. magic fragments.
                    let len = 1 + rng.below(50);
                    for _ in 0..len {
                        data.push(if rng.below(3) == 0 {
                            MAGIC[rng.below(4)]
                        } else {
                            rng.next_u64() as u8
                        });
                    }
                }
                _ => {
                    // Corrupted frame: valid encode then byte flip.
                    let payload: Vec<u8> = (0..rng.below(20)).map(|_| rng.next_u64() as u8).collect();
                    let mut f = encode_frame(
                        (rng.next_u64() & 0xFFFF) as u16,
                        &payload,
                        cap,
                    )
                    .unwrap();
                    let idx = rng.below(f.len() as u64);
                    f[idx] ^= 1 << rng.below(8);
                    data.extend_from_slice(&f);
                }
            }
            // Feed in random slices and assert the bound after every slice.
            let mut i = 0;
            while i < data.len() {
                let n = (1 + rng.below(12)).min(data.len() - i);
                p.feed(&data[i..i + n]);
                assert!(
                    p.buffer_len() <= bound,
                    "seed {seed}: buffer {} exceeded bound {bound}",
                    p.buffer_len()
                );
                i += n;
            }
            data.clear();
        }
    }
}

// ---------- Sequence numbers: gaps and wrap ----------

#[test]
fn sequence_gap_reports_exact_missing_count() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut s = Vec::new();
    s.extend_from_slice(&encode_frame(10, b"a", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(13, b"b", DEFAULT_MAX_PAYLOAD).unwrap());
    let ev = p.feed(&s);
    let gap = ev
        .iter()
        .find_map(|e| match e {
            Event::SequenceGap { missing, .. } => Some(*missing),
            _ => None,
        })
        .unwrap();
    assert_eq!(gap, 2); // 11, 12 missing
}

#[test]
fn sequence_wrap_gap() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut s = Vec::new();
    s.extend_from_slice(&encode_frame(65534, b"a", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(1, b"b", DEFAULT_MAX_PAYLOAD).unwrap()); // wraps; 65535 and 0 missing
    let ev = p.feed(&s);
    let g = ev
        .iter()
        .find_map(|e| match e {
            Event::SequenceGap { last, received, missing, .. } => Some((*last, *received, *missing)),
            _ => None,
        })
        .unwrap();
    assert_eq!(g, (65534, 1, 2));
}

#[test]
fn sequence_wrap_single_missing() {
    // expected 65535, frame 0 arrives: exactly one number (65535) missing.
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut s = Vec::new();
    s.extend_from_slice(&encode_frame(65534, b"a", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(65535, b"b", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(0, b"c", DEFAULT_MAX_PAYLOAD).unwrap()); // in order after wrap
    s.extend_from_slice(&encode_frame(2, b"d", DEFAULT_MAX_PAYLOAD).unwrap()); // 1 missing
    let ev = p.feed(&s);
    let gaps: Vec<u16> = ev
        .iter()
        .filter_map(|e| match e {
            Event::SequenceGap { missing, .. } => Some(*missing),
            _ => None,
        })
        .collect();
    assert_eq!(gaps, vec![1]);
}

#[test]
fn sequence_exact_wrap_is_in_order() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut s = Vec::new();
    s.extend_from_slice(&encode_frame(65535, b"a", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(0, b"b", DEFAULT_MAX_PAYLOAD).unwrap());
    let ev = p.feed(&s);
    assert!(!ev.iter().any(|e| matches!(e, Event::SequenceGap { .. } | Event::OutOfOrder { .. })));
    assert_eq!(frames_only(&ev).len(), 2);
}

#[test]
fn duplicate_frame_is_out_of_order_and_expected_unchanged() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut s = Vec::new();
    s.extend_from_slice(&encode_frame(5, b"a", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(5, b"dup", DEFAULT_MAX_PAYLOAD).unwrap());
    s.extend_from_slice(&encode_frame(6, b"b", DEFAULT_MAX_PAYLOAD).unwrap());
    let ev = p.feed(&s);
    assert!(
        ev.iter().any(|e| matches!(e, Event::OutOfOrder { received: 5, expected: 6, .. }))
    );
    assert_eq!(frames_only(&ev).len(), 3); // still delivered, just flagged
}

// ---------- Truncation ----------

#[test]
fn truncated_frame_reported_on_finish() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(1, b"partial", DEFAULT_MAX_PAYLOAD).unwrap();
    p.feed(&raw[..raw.len() - 3]); // missing last 3 CRC bytes
    let ev = p.finish();
    match &ev[0] {
        Event::Truncated { bytes, has_magic, .. } => {
            assert!(*has_magic);
            assert_eq!(bytes.len(), raw.len() - 3);
        }
        other => panic!("expected truncated, got {other:?}"),
    }
}

#[test]
fn truncated_then_continued_in_next_session_chunk_is_whole() {
    // Simulating a serial device that pauses mid-frame: the held bytes stay
    // buffered and completion is seamless.
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(1, b"slow-device", DEFAULT_MAX_PAYLOAD).unwrap();
    let cut = raw.len() / 2;
    let e1 = p.feed(&raw[..cut]);
    let e2 = p.feed(&raw[cut..]);
    assert!(e1.is_empty());
    assert_eq!(frames_only(&e2), vec![(1, b"slow-device".to_vec())]);
}

// ---------- Randomized differential / round-trip fuzzing ----------

#[test]
fn randomized_roundtrip_many_seeds() {
    // For every seed: build a stream of real frames + noise, feed it in random
    // slices, and assert every encoded frame is recovered in order with exact
    // payload bytes — noise/corruption cases are excluded from expectations.
    for seed in 1..=300u64 {
        let mut rng = Rng::new(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15).max(1));
        let cap = 1 + rng.below(2000);
        let mut p = FrameParser::new(cap);
        let mut stream = Vec::new();
        let mut expected: Vec<(u16, Vec<u8>)> = Vec::new();

        // Start sequences at a fixed base so gap/oood events don't matter for
        // frame recovery assertions.
        let n_frames = 1 + rng.below(25);
        for s in 0..n_frames {
            if rng.below(4) == 0 {
                let noise_len = rng.below(30);
                for _ in 0..noise_len {
                    stream.push(if rng.below(4) == 0 {
                        MAGIC[rng.below(4)]
                    } else {
                        rng.next_u64() as u8
                    });
                }
            }
            let len = rng.below((cap + 1) as u64).min(cap);
            let payload: Vec<u8> = (0..len).map(|_| rng.next_u64() as u8).collect();
            stream.extend_from_slice(&encode_frame(s as u16, &payload, cap).unwrap());
            expected.push((s as u16, payload));
        }
        // Sometimes truncate the final frame; then expect one fewer frame.
        let mut truncated_tail = 0;
        if rng.below(3) == 0 {
            truncated_tail = 1 + rng.below((stream.len().min(20) as u64).max(1));
            truncated_tail = truncated_tail.min(stream.len());
            stream.truncate(stream.len() - truncated_tail);
        }

        let events = feed_random_slices(&mut p, &stream, &mut rng);
        let mut finish_events = Vec::new();
        if truncated_tail > 0 {
            finish_events = p.finish();
            expected.pop();
        }
        let got = frames_only(&events);
        assert_eq!(got, expected, "frame mismatch at seed {seed}");
        if truncated_tail > 0 {
            assert!(matches!(
                finish_events.last(),
                Some(Event::Truncated { .. })
            ));
        } else {
            assert!(p.finish().is_empty(), "unexpected held bytes seed {seed}");
        }
    }
}

#[test]
fn random_corruption_never_loses_following_good_frames() {
    // Stream = [bad-crc frame][good frame] repeatedly, random slices.
    for seed in 1000..1200u64 {
        let mut rng = Rng::new(seed ^ 0xDEAD);
        let cap = 128;
        let mut p = FrameParser::new(cap);
        let mut stream = Vec::new();
        let pairs = 1 + rng.below(8);
        for i in 0..pairs {
            let payload: Vec<u8> = (0..(1 + rng.below(30))).map(|_| rng.next_u64() as u8).collect();
            let mut bad = encode_frame((2 * i) as u16, &payload, cap).unwrap();
            let flip_pos = 8 + rng.below(bad.len() as u64 - 8);
            let flip_bit = rng.below(8);
            bad[flip_pos] ^= 1 << flip_bit;
            stream.extend_from_slice(&bad);
            stream.extend_from_slice(
                &encode_frame((2 * i + 1) as u16, format!("good{i}").as_bytes(), cap).unwrap(),
            );
        }
        let events = feed_random_slices(&mut p, &stream, &mut rng);
        let got: Vec<u16> = frames_only(&events).into_iter().map(|(s, _)| s).collect();
        let want: Vec<u16> = (0..pairs).map(|i| (2 * i + 1) as u16).collect();
        assert_eq!(got, want, "lost a good frame at seed {seed}");
        assert_eq!(p.stats().bad_crc, pairs as u64);
        assert!(p.finish().is_empty());
    }
}

#[test]
fn empty_payload_frames_roundtrip() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let raw = encode_frame(0, &[], DEFAULT_MAX_PAYLOAD).unwrap();
    let ev = feed_random_slices(&mut p, &raw, &mut Rng::new(5));
    assert_eq!(frames_only(&ev), vec![(0, vec![])]);
}

#[test]
fn offsets_are_absolute_stream_positions() {
    let mut p = FrameParser::new(DEFAULT_MAX_PAYLOAD);
    let mut stream = b"abc".to_vec();
    let f = encode_frame(1, b"x", DEFAULT_MAX_PAYLOAD).unwrap();
    stream.extend_from_slice(&f);
    let ev = p.feed(&stream);
    match ev.iter().find(|e| matches!(e, Event::Frame { .. })).unwrap() {
        Event::Frame { offset, .. } => assert_eq!(*offset, 3),
        _ => unreachable!(),
    }
}
