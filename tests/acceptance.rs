//! Acceptance tests: randomised comparison against `BTreeSet`, container
//! boundaries, extremely dense/sparse inputs, and hostile declared lengths.
//!
//! A tiny, deterministic LCG is used so the suite is reproducible without any
//! external crate.

use std::collections::BTreeSet;

use rbitset::codec::{decode_from_slice, encode_to_vec, DecodeLimits, MAGIC, VERSION};
use rbitset::container::{ARRAY_MAX, BITMAP_WORDS};
use rbitset::set::IntSet;

/// Deterministic numerical-recipes LCG.
struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0
    }
    fn next_u32(&mut self) -> u32 {
        (self.next_u64() >> 32) as u32
    }
    /// Value in `[0, n)` (n must be > 0).
    fn below(&mut self, n: u32) -> u32 {
        self.next_u32() % n
    }
}

fn to_btree(s: &IntSet) -> BTreeSet<u32> {
    s.iter().collect()
}

fn big_limits() -> DecodeLimits {
    DecodeLimits {
        max_bytes: 1 << 40,
        max_values: 1 << 32,
    }
}

// --------------------------------------------------------------------------
// Randomised algebra vs the standard-library set
// --------------------------------------------------------------------------

fn random_universe(rng: &mut Rng, n: usize, modulo: u32) -> Vec<u32> {
    (0..n).map(|_| rng.below(modulo)).collect()
}

#[test]
fn random_algebra_matches_btreeset_across_densities() {
    let mut rng = Rng(0x1234_5678_9abc_def0);
    // (sample count, modulo) — from very sparse to extremely dense.
    let profiles = [
        (50usize, 1_000_000u32), // sparse
        (2_000, 100_000),
        (50_000, 100_000), // dense across many chunks
        (80_000, 70_000),  // extremely dense
        (5_000, 65_536),   // exercises single-chunk crossover
    ];
    for (n, m) in profiles {
        for seed_shift in 0u64..3 {
            let mut r1 = Rng(rng.next_u64() ^ (seed_shift << 40));
            let raw_a = random_universe(&mut r1, n, m);
            let raw_b = random_universe(&mut r1, n, m);
            let ra: BTreeSet<u32> = raw_a.iter().copied().collect();
            let rb: BTreeSet<u32> = raw_b.iter().copied().collect();
            let sa = IntSet::from_values(&raw_a);
            let sb = IntSet::from_values(&raw_b);

            assert_eq!(to_btree(&sa), ra, "A mismatch profile={n}/{m}");
            assert_eq!(to_btree(&sb), rb, "B mismatch profile={n}/{m}");
            assert_eq!(sa.len(), ra.len() as u64);
            assert_eq!(sb.len(), rb.len() as u64);

            let ru: BTreeSet<u32> = ra.union(&rb).copied().collect();
            let ri: BTreeSet<u32> = ra.intersection(&rb).copied().collect();
            let rd: BTreeSet<u32> = ra.difference(&rb).copied().collect();

            assert_eq!(to_btree(&sa.union(&sb)), ru, "union profile={n}/{m}");
            assert_eq!(to_btree(&sa.intersection(&sb)), ri, "inter profile={n}/{m}");
            assert_eq!(to_btree(&sa.difference(&sb)), rd, "diff profile={n}/{m}");

            // Membership cross-check for random probes.
            for _ in 0..200 {
                let p = r1.below(m);
                assert_eq!(sa.contains(p), ra.contains(&p));
            }
        }
    }
}

// --------------------------------------------------------------------------
// Container boundary conditions
// --------------------------------------------------------------------------

#[test]
fn array_bitmap_boundary_both_sides_within_one_chunk() {
    for size in [ARRAY_MAX - 1, ARRAY_MAX, ARRAY_MAX + 1] {
        let vals: Vec<u32> = (0..size as u32).collect();
        let s = IntSet::from_values(&vals);
        let c = s.container(0).expect("chunk 0");
        if size <= ARRAY_MAX {
            assert!(
                matches!(c, rbitset::container::Container::Array(_)),
                "size {size}"
            );
        } else {
            assert!(
                matches!(c, rbitset::container::Container::Bitmap(_)),
                "size {size}"
            );
        }
        let buf = encode_to_vec(&s).unwrap();
        let back = decode_from_slice(&buf, big_limits()).unwrap();
        assert_eq!(back.to_vec(), vals, "round trip size {size}");
    }
}

#[test]
fn chunk_key_boundaries() {
    let vals = [
        0u32,
        0x0000_ffff,
        0x0001_0000,
        0x7fff_ffff,
        0x8000_0000,
        0xffff_0000,
        0xffff_fffe,
        u32::MAX,
    ];
    let s = IntSet::from_values(&vals);
    let buf = encode_to_vec(&s).unwrap();
    let back = decode_from_slice(&buf, big_limits()).unwrap();
    assert_eq!(back.to_vec(), vals);
    assert_eq!(s.len(), vals.len() as u64);
}

#[test]
fn every_container_kind_pair_in_algebra() {
    // A sparse (<=4096) and dense (>4096) chunk with the same high key,
    // combined in all four (kind x kind) pairings.
    let sparse: Vec<u32> = (0..100u32).map(|i| i * 7).collect();
    let dense: Vec<u32> = (0..5000u32).collect();
    let pairs = [IntSet::from_values(&sparse), IntSet::from_values(&dense)];
    for a in &pairs {
        for b in &pairs {
            let ra = to_btree(a);
            let rb = to_btree(b);
            assert_eq!(
                to_btree(&a.union(b)),
                ra.union(&rb).copied().collect::<BTreeSet<_>>()
            );
            assert_eq!(
                to_btree(&a.intersection(b)),
                ra.intersection(&rb).copied().collect::<BTreeSet<_>>()
            );
            assert_eq!(
                to_btree(&a.difference(b)),
                ra.difference(&rb).copied().collect::<BTreeSet<_>>()
            );
        }
    }
}

#[test]
fn wire_bitmap_decodes_to_canonical_array_when_sparse() {
    // Construct a bitmap-on-the-wire chunk carrying only three values by
    // hand-encoding, then ensure the decoded in-memory form equals the array
    // form (container switching preserves semantics).
    let mut raw = Vec::new();
    raw.extend_from_slice(&MAGIC);
    raw.extend_from_slice(&[VERSION, 0]);
    raw.extend_from_slice(&1u32.to_le_bytes()); // chunks
    raw.extend_from_slice(&3u64.to_le_bytes()); // total
    raw.extend_from_slice(&42u16.to_le_bytes()); // key
    raw.push(1); // bitmap
    raw.extend_from_slice(&3u32.to_le_bytes()); // card
    let mut words = [0u64; BITMAP_WORDS];
    for v in [1u16, 65535, 1000] {
        words[(v >> 6) as usize] |= 1u64 << (v & 63);
    }
    for w in words {
        raw.extend_from_slice(&w.to_le_bytes());
    }
    let back = decode_from_slice(&raw, big_limits()).unwrap();
    let expect = IntSet::from_values(&[42u32 << 16 | 1, 42 << 16 | 1000, 42 << 16 | 65535]);
    assert_eq!(back, expect);
    assert!(matches!(
        back.container(42).unwrap(),
        rbitset::container::Container::Array(_)
    ));
}

// --------------------------------------------------------------------------
// Extremely dense and extremely sparse whole-universe data
// --------------------------------------------------------------------------

#[test]
fn one_value_per_every_chunk_round_trips() {
    // One value in each of the 65536 chunks: exercises the max chunk count
    // and chunk-key traversal cheaply (64 KiB of value payload).
    let mut s = IntSet::new();
    for hi in 0..=u16::MAX {
        let low = if hi % 2 == 0 { 0u16 } else { 65535 };
        s.push_group(hi, vec![low]);
    }
    assert_eq!(s.len(), 65_536);
    let buf = encode_to_vec(&s).unwrap();
    let back = decode_from_slice(&buf, big_limits()).unwrap();
    assert_eq!(back.len(), 65_536);
    assert_eq!(back, s);
}

#[test]
fn full_single_chunk_is_65536_values() {
    let vals: Vec<u32> = (0..=u16::MAX as u32).collect();
    let s = IntSet::from_values(&vals);
    assert_eq!(s.len(), 65_536);
    let buf = encode_to_vec(&s).unwrap();
    let back = decode_from_slice(&buf, big_limits()).unwrap();
    assert_eq!(back.len(), 65_536);
    assert_eq!(back.to_vec(), vals);
}

#[test]
fn extremely_sparse_handful_of_values_across_space() {
    // Given unsorted, expect ascending, de-duplicated output.
    let vals = [0u32, 1, u32::MAX, u32::MAX - 1, 1 << 31];
    let s = IntSet::from_values(&vals);
    let buf = encode_to_vec(&s).unwrap();
    let back = decode_from_slice(&buf, big_limits()).unwrap();
    assert_eq!(back.to_vec(), [0, 1, 1 << 31, u32::MAX - 1, u32::MAX]);
}

// --------------------------------------------------------------------------
// Hostile / malformed inputs
// --------------------------------------------------------------------------

fn header(chunks: u32, total: u64) -> Vec<u8> {
    let mut b = Vec::new();
    b.extend_from_slice(&MAGIC);
    b.extend_from_slice(&[VERSION, 0]);
    b.extend_from_slice(&chunks.to_le_bytes());
    b.extend_from_slice(&total.to_le_bytes());
    b
}

#[test]
fn hostile_huge_declared_chunk_count() {
    let mut b = header(u32::MAX, 0);
    // give it one byte so it is clearly not a clean EOF during header
    b.push(0);
    let err = decode_from_slice(&b, big_limits()).unwrap_err();
    assert!(matches!(
        err,
        rbitset::Error::Malformed(_, "chunk count exceeds the 16-bit key space")
    ));
}

#[test]
fn hostile_declared_total_exceeds_value_budget() {
    let b = header(0, u64::from(u32::MAX));
    let limits = DecodeLimits {
        max_bytes: 1 << 30,
        max_values: 10,
    };
    assert!(matches!(
        decode_from_slice(&b, limits),
        Err(rbitset::Error::LengthLimitExceeded { .. })
    ));
}

#[test]
fn hostile_array_length_far_beyond_available_data() {
    for declared in [4097u16, 10_000, u16::MAX] {
        let mut b = header(1, u64::from(declared.min(4096)) + 1);
        b.extend_from_slice(&0u16.to_le_bytes());
        b.push(0);
        b.extend_from_slice(&declared.to_le_bytes());
        let err = decode_from_slice(&b, big_limits()).unwrap_err();
        // Values > 4096 are rejected structurally; smaller lies hit EOF.
        assert!(
            matches!(
                err,
                rbitset::Error::Malformed(_, "array container declares more than 4096 values")
                    | rbitset::Error::UnexpectedEof { .. }
            ),
            "declared {declared}: got {err:?}"
        );
    }
}

#[test]
fn hostile_bitmap_length_claim_without_payload() {
    let mut b = header(1, 65_536);
    b.extend_from_slice(&0u16.to_le_bytes());
    b.push(1); // bitmap
    b.extend_from_slice(&65_536u32.to_le_bytes());
    // no 8 KiB payload follows
    assert!(matches!(
        decode_from_slice(&b, big_limits()),
        Err(rbitset::Error::UnexpectedEof { .. })
    ));
}

#[test]
fn hostile_bitmap_cardinality_overflow() {
    let mut b = header(1, 65_537);
    b.extend_from_slice(&0u16.to_le_bytes());
    b.push(1);
    b.extend_from_slice(&65_537u32.to_le_bytes());
    let mut words = vec![0u8; BITMAP_WORDS * 8];
    // zeroed payload is harmless but card 65537 is impossible
    b.append(&mut words);
    assert!(matches!(
        decode_from_slice(&b, big_limits()),
        Err(rbitset::Error::Malformed(
            _,
            "bitmap cardinality cannot exceed 65536"
        ))
    ));
}

#[test]
fn byte_budget_must_bound_read_ahead() {
    let vals: Vec<u32> = (0..100_000u32).collect();
    let buf = encode_to_vec(&IntSet::from_values(&vals)).unwrap();
    // Budget smaller than even the first bitmap payload.
    let limits = DecodeLimits {
        max_bytes: 32,
        max_values: 1 << 32,
    };
    assert!(matches!(
        decode_from_slice(&buf, limits),
        Err(rbitset::Error::ByteLimitExceeded)
    ));
}

#[test]
fn trailing_garbage_after_complete_stream_is_not_consumed_by_set() {
    let mut buf = encode_to_vec(&IntSet::from_values(&[1, 2])).unwrap();
    let clean_len = buf.len();
    buf.extend_from_slice(b"GARBAGE");
    // Decoder trusts the chunk count, not the byte length; trailing bytes are
    // simply unread by the slice reader. Values must still be exact.
    let back = decode_from_slice(&buf, big_limits()).unwrap();
    assert_eq!(back.len(), 2);
    assert_eq!(back.to_vec(), [1, 2]);
    assert_eq!(&buf[clean_len..], b"GARBAGE");
}

// --------------------------------------------------------------------------
// Streaming interface bounds memory to one chunk
// --------------------------------------------------------------------------

#[test]
fn decode_from_cursor_matches_slice() {
    use std::io::Cursor;
    let s = IntSet::from_values(
        &(0..20_000u32)
            .map(|i| i.wrapping_mul(2246822519) % 200_000)
            .collect::<Vec<_>>(),
    );
    let buf = encode_to_vec(&s).unwrap();
    let via_slice = decode_from_slice(&buf, big_limits()).unwrap();
    let via_cursor = rbitset::codec::decode_set(Cursor::new(buf.clone()), big_limits()).unwrap();
    assert_eq!(via_slice, via_cursor);
}
