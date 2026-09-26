//! Acceptance tests: the hybrid bitmap set against `BTreeSet<u32>` as the
//! reference oracle, across random data, container-type boundaries,
//! extreme densities, and hostile encoded input.

use std::collections::BTreeSet;

use rbitmap::bitmap::{Bitmap, Limits};
use rbitmap::container::ARRAY_LIMIT;
use rbitmap::error::RbError;

/// Deterministic xorshift64* — no external RNG dependency.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Rng(seed | 1)
    }
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545F4914F6CDD1D)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn oracle_ops(
    a: &BTreeSet<u32>,
    b: &BTreeSet<u32>,
) -> (BTreeSet<u32>, BTreeSet<u32>, BTreeSet<u32>) {
    (
        a.union(b).copied().collect(),
        a.intersection(b).copied().collect(),
        a.difference(b).copied().collect(),
    )
}

fn bitmap_ops(a: &Bitmap, b: &Bitmap) -> (Bitmap, Bitmap, Bitmap) {
    (a.union(b), a.intersect(b), a.difference(b))
}

fn assert_same(actual: &Bitmap, expected: &BTreeSet<u32>, ctx: &str) {
    let got: BTreeSet<u32> = actual.iter().collect();
    assert_eq!(
        &got,
        expected,
        "{ctx}: mismatch (got {} values, want {})",
        got.len(),
        expected.len()
    );
    assert_eq!(actual.len(), expected.len(), "{ctx}: cardinality");
    // Round-trip through the binary codec must be lossless too.
    let decoded = Bitmap::decode(&actual.encode(), Limits::default()).expect("decode");
    let got2: BTreeSet<u32> = decoded.iter().collect();
    assert_eq!(&got2, expected, "{ctx}: codec round-trip");
}

/// Random sets over several density regimes, verified against BTreeSet.
#[test]
fn random_sets_match_btree_oracle() {
    let mut rng = Rng::new(0x5EED);
    for trial in 0..200 {
        // Regime rotates: full 32-bit space, one container, dense cluster,
        // boundary-straddling sizes.
        let regime = trial % 4;
        let make = |rng: &mut Rng, n: usize| -> BTreeSet<u32> {
            let mut s = BTreeSet::new();
            while s.len() < n {
                let v = match regime {
                    0 => rng.next() as u32,
                    1 => (rng.below(65536)) as u32, // single high key
                    2 => {
                        // dense cluster: a base plus small offset
                        let base = rng.below(1 << 20) as u32;
                        base.wrapping_add(rng.below(64) as u32)
                    }
                    _ => {
                        // straddle the array/bitmap threshold in one key
                        let key = rng.below(65536) as u32;
                        (key << 16) | (rng.below((ARRAY_LIMIT * 2) as u64) as u32)
                    }
                };
                s.insert(v);
            }
            s
        };
        let na = (rng.below(2000)) as usize;
        let nb = (rng.below(2000)) as usize;
        let a_ref = make(&mut rng, na);
        let b_ref = make(&mut rng, nb);
        let a = Bitmap::from_values(a_ref.iter().copied());
        let b = Bitmap::from_values(b_ref.iter().copied());

        let (ru, ri, rd) = oracle_ops(&a_ref, &b_ref);
        let (bu, bi, bd) = bitmap_ops(&a, &b);
        assert_same(&bu, &ru, "union");
        assert_same(&bi, &ri, "intersect");
        assert_same(&bd, &rd, "difference");

        // Membership agrees for a sample of probe values.
        for _ in 0..50 {
            let probe = rng.next() as u32;
            assert_eq!(
                a.contains(probe),
                a_ref.contains(&probe),
                "contains({probe})"
            );
        }
    }
}

/// Container switching: crossing the 4096 threshold in both directions
/// must preserve the exact value set.
#[test]
fn container_switching_preserves_semantics() {
    let key: u32 = 7 << 16;
    let mut bm = Bitmap::new();
    // Fill to exactly the limit: still an array.
    for i in 0..ARRAY_LIMIT as u32 {
        bm.insert(key | i);
    }
    assert_eq!(bm.array_container_count(), 1);
    assert_eq!(bm.bitmap_container_count(), 0);

    // One more: upgrade to bitmap.
    bm.insert(key | ARRAY_LIMIT as u32);
    assert_eq!(bm.bitmap_container_count(), 1);
    for i in 0..=ARRAY_LIMIT as u32 {
        assert!(bm.contains(key | i), "after upgrade missing {i}");
    }

    // Remove back down to the limit: downgrade to array.
    assert!(bm.remove(key | ARRAY_LIMIT as u32));
    assert_eq!(bm.array_container_count(), 1);
    assert_eq!(bm.bitmap_container_count(), 0);
    for i in 0..ARRAY_LIMIT as u32 {
        assert!(bm.contains(key | i), "after downgrade missing {i}");
    }
    assert!(!bm.contains(key | ARRAY_LIMIT as u32));

    // Empty containers disappear entirely.
    for i in 0..ARRAY_LIMIT as u32 {
        assert!(bm.remove(key | i));
    }
    assert!(bm.is_empty());
    assert_eq!(bm.container_count(), 0);
}

/// Boundary values: 0, u32::MAX, and the exact container edges.
#[test]
fn boundary_values() {
    let edge: Vec<u32> = vec![
        0,
        1,
        65535,
        65536,
        65537,
        u32::MAX - 1,
        u32::MAX,
        (ARRAY_LIMIT as u32) << 16,
        ((ARRAY_LIMIT as u32) << 16) | 65535,
    ];
    let bm = Bitmap::from_values(edge.iter().copied());
    for &v in &edge {
        assert!(bm.contains(v), "missing {v}");
    }
    assert!(!bm.contains(2));
    assert!(!bm.contains(u32::MAX - 2));
    let roundtrip = Bitmap::decode(&bm.encode(), Limits::default()).unwrap();
    assert_eq!(roundtrip, bm);
    let vals: Vec<u32> = bm.iter().collect();
    let mut sorted = edge.clone();
    sorted.sort_unstable();
    assert_eq!(vals, sorted, "iteration must be ascending");
}

/// Extremely dense data: full containers (all 65536 lows for several keys).
#[test]
fn extremely_dense_sets() {
    let mut a = Bitmap::new();
    let mut b = Bitmap::new();
    for key in 0u32..4 {
        for low in 0u32..65536 {
            a.insert((key << 16) | low);
            if low % 2 == 0 {
                b.insert((key << 16) | low);
            }
        }
    }
    assert_eq!(a.len(), 4 * 65536);
    assert_eq!(b.len(), 4 * 32768);

    let inter = a.intersect(&b);
    assert_eq!(inter.len(), b.len());
    let diff = a.difference(&b);
    assert_eq!(diff.len(), 4 * 32768);
    let uni = a.union(&b);
    assert_eq!(uni.len(), a.len());

    // Codec round-trip on dense data.
    let decoded = Bitmap::decode(&a.encode(), Limits::default()).unwrap();
    assert_eq!(decoded.len(), a.len());
    assert!(decoded.contains((3 << 16) | 65535));
}

/// Extremely sparse data: single values scattered across the full u32 range.
#[test]
fn extremely_sparse_sets() {
    let mut rng = Rng::new(0xC0FFEE);
    let mut a = Bitmap::new();
    let mut b = Bitmap::new();
    let mut ref_a = BTreeSet::new();
    let mut ref_b = BTreeSet::new();
    for _ in 0..64 {
        let va = rng.next() as u32;
        let vb = rng.next() as u32;
        a.insert(va);
        b.insert(vb);
        ref_a.insert(va);
        ref_b.insert(vb);
    }
    let (ru, ri, rd) = oracle_ops(&ref_a, &ref_b);
    assert_same(&a.union(&b), &ru, "sparse union");
    assert_same(&a.intersect(&b), &ri, "sparse intersect");
    assert_same(&a.difference(&b), &rd, "sparse difference");
}

/// Insert/remove interleavings against the oracle, including duplicates.
#[test]
fn incremental_updates_match_oracle() {
    let mut rng = Rng::new(42);
    let mut bm = Bitmap::new();
    let mut oracle = BTreeSet::new();
    for step in 0..20_000u32 {
        let v = (rng.below(1 << 22)) as u32; // forces both container kinds
        if rng.below(3) == 0 {
            assert_eq!(bm.remove(v), oracle.remove(&v), "remove step {step}");
        } else {
            assert_eq!(bm.insert(v), oracle.insert(v), "insert step {step}");
        }
        if step % 5000 == 0 {
            assert_eq!(bm.len(), oracle.len());
        }
    }
    let got: BTreeSet<u32> = bm.iter().collect();
    assert_eq!(got, oracle);
}

// ---------- hostile / malformed input ----------

fn encode_valid() -> Vec<u8> {
    Bitmap::from_values([1u32, 2, 3, 70000, u32::MAX]).encode()
}

#[test]
fn decode_rejects_bad_magic_version_flags() {
    let good = encode_valid();

    let mut bad = good.clone();
    bad[0] = b'X';
    assert!(Bitmap::decode(&bad, Limits::default()).is_err());

    let mut bad = good.clone();
    bad[4] = 99; // version
    assert!(Bitmap::decode(&bad, Limits::default()).is_err());

    let mut bad = good.clone();
    bad[5] = 1; // flags
    assert!(Bitmap::decode(&bad, Limits::default()).is_err());
}

#[test]
fn decode_rejects_truncation_at_every_point() {
    let good = encode_valid();
    for cut in 0..good.len() {
        assert!(
            Bitmap::decode(&good[..cut], Limits::default()).is_err(),
            "truncated at {cut} bytes must fail"
        );
    }
}

#[test]
fn decode_rejects_trailing_garbage() {
    let mut bad = encode_valid();
    bad.push(0x00);
    assert!(Bitmap::decode(&bad, Limits::default()).is_err());
}

#[test]
fn decode_rejects_malicious_container_count() {
    // Valid header, but nkeys claims more containers than the limit allows.
    let mut buf = Vec::new();
    buf.extend_from_slice(b"RBM1");
    buf.push(1);
    buf.push(0);
    // varint for 70000 > default 65536
    rbitmap::codec::write_varint(&mut buf, 70000);
    let err = Bitmap::decode(&buf, Limits::default()).unwrap_err();
    assert!(matches!(err, RbError::LengthExceeded { .. }), "{err}");
}

#[test]
fn decode_rejects_malicious_array_length() {
    // Header with one key, array tag, then a count far beyond 65536.
    let mut buf = Vec::new();
    buf.extend_from_slice(b"RBM1");
    buf.push(1);
    buf.push(0);
    rbitmap::codec::write_varint(&mut buf, 1); // one key
    buf.extend_from_slice(&0u16.to_le_bytes()); // key 0
    buf.push(0x01); // array tag
    rbitmap::codec::write_varint(&mut buf, 1_000_000_000); // hostile count
    let err = Bitmap::decode(&buf, Limits::default()).unwrap_err();
    assert!(matches!(err, RbError::LengthExceeded { .. }), "{err}");
}

#[test]
fn decode_rejects_unsorted_or_duplicate_array_values() {
    let mut buf = Vec::new();
    buf.extend_from_slice(b"RBM1");
    buf.push(1);
    buf.push(0);
    rbitmap::codec::write_varint(&mut buf, 1);
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.push(0x01);
    rbitmap::codec::write_varint(&mut buf, 2);
    buf.extend_from_slice(&5u16.to_le_bytes());
    buf.extend_from_slice(&5u16.to_le_bytes()); // duplicate
    assert!(Bitmap::decode(&buf, Limits::default()).is_err());
}

#[test]
fn decode_rejects_duplicate_and_out_of_order_keys() {
    let mut buf = Vec::new();
    buf.extend_from_slice(b"RBM1");
    buf.push(1);
    buf.push(0);
    rbitmap::codec::write_varint(&mut buf, 2);
    for key in [7u16, 7u16] {
        buf.extend_from_slice(&key.to_le_bytes());
        buf.push(0x01);
        rbitmap::codec::write_varint(&mut buf, 1);
        buf.extend_from_slice(&1u16.to_le_bytes());
    }
    assert!(matches!(
        Bitmap::decode(&buf, Limits::default()),
        Err(RbError::DuplicateKey(7))
    ));

    // Out of order.
    let mut buf = Vec::new();
    buf.extend_from_slice(b"RBM1");
    buf.push(1);
    buf.push(0);
    rbitmap::codec::write_varint(&mut buf, 2);
    for key in [9u16, 3u16] {
        buf.extend_from_slice(&key.to_le_bytes());
        buf.push(0x01);
        rbitmap::codec::write_varint(&mut buf, 1);
        buf.extend_from_slice(&1u16.to_le_bytes());
    }
    assert!(matches!(
        Bitmap::decode(&buf, Limits::default()),
        Err(RbError::OutOfOrderKeys { .. })
    ));
}

#[test]
fn decode_rejects_unknown_container_tag() {
    let mut buf = Vec::new();
    buf.extend_from_slice(b"RBM1");
    buf.push(1);
    buf.push(0);
    rbitmap::codec::write_varint(&mut buf, 1);
    buf.extend_from_slice(&0u16.to_le_bytes());
    buf.push(0x7F); // unknown tag
    assert!(matches!(
        Bitmap::decode(&buf, Limits::default()),
        Err(RbError::InvalidTag { tag: 0x7F, .. })
    ));
}

#[test]
fn decode_output_budget_limits_cardinality() {
    // A dense set decodes fine under the default budget but must fail when
    // the caller tightens max_values below its cardinality.
    let mut bm = Bitmap::new();
    for low in 0u32..65536 {
        bm.insert(low);
    }
    let bytes = bm.encode();
    let tight = Limits::new(65536, 1000);
    let err = Bitmap::decode(&bytes, tight).unwrap_err();
    assert!(matches!(err, RbError::LengthExceeded { .. }), "{err}");
}

#[test]
fn decode_stream_matches_decode() {
    let bm = Bitmap::from_values((0..5000u32).chain([u32::MAX, 1 << 20]));
    let bytes = bm.encode();
    let streamed = Bitmap::decode_stream(&mut &bytes[..], Limits::default()).unwrap();
    assert_eq!(streamed, bm);
}

#[test]
fn encode_stream_matches_encode() {
    let mut bm = Bitmap::new();
    for i in 0..9000u32 {
        bm.insert(i);
    }
    bm.insert(u32::MAX);
    let mut buf = Vec::new();
    bm.encode_stream(&mut buf).unwrap();
    assert_eq!(buf, bm.encode());
}

#[test]
fn empty_set_roundtrip() {
    let bm = Bitmap::new();
    let bytes = bm.encode();
    assert_eq!(bytes.len(), 7); // magic + version + flags + varint(0)
    let decoded = Bitmap::decode(&bytes, Limits::default()).unwrap();
    assert!(decoded.is_empty());
}
