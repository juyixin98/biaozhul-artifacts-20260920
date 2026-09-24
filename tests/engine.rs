//! Engine-level tests: rolling signature -> delta -> apply round-trips,
//! duplicate blocks, insertion/deletion shifts, and a *constructed weak
//! checksum collision* that must be rejected by the strong digest.

use artifact_delta::delta::diff;
use artifact_delta::patch::{apply_patch, Patch};
use artifact_delta::signature::{strong_hash, Signature};
use artifact_delta::weak::Rollsum;

/// Build basis signature, diff target, apply, assert byte-identical output.
fn roundtrip(basis: &[u8], target: &[u8], block_len: u32) -> artifact_delta::delta::DeltaStats {
    let sig = Signature::build(basis, block_len).unwrap();
    let delta = diff(&sig, basis.len() as u64, target);
    let patch = Patch::new(
        &delta,
        sig.block_len,
        basis.len() as u64,
        strong_hash(basis),
    );
    let bytes = patch.encode();
    let parsed = Patch::decode(&bytes).expect("patch must decode");
    let rebuilt = apply_patch(&parsed, basis).expect("apply must succeed");
    assert_eq!(rebuilt, target, "reconstructed content must be byte-identical");
    assert_eq!(strong_hash(&rebuilt), strong_hash(target));
    delta.stats
}

/// Deterministic pseudo-random bytes.
fn rng_bytes(seed: u64, n: usize) -> Vec<u8> {
    let mut x = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    let mut out = Vec::with_capacity(n);
    while out.len() < n {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        out.extend_from_slice(&x.to_le_bytes());
    }
    out.truncate(n);
    out
}

#[test]
fn identical_artifacts_match_every_block() {
    let basis = rng_bytes(1, 10_000);
    let stats = roundtrip(&basis, &basis, 256);
    assert_eq!(stats.blocks_matched as usize, basis.len().div_ceil(256));
    assert_eq!(stats.literal_bytes, 0);
    assert_eq!(stats.bytes_from_basis, basis.len() as u64);
    assert_eq!(stats.strong_rejections, 0);
}

#[test]
fn insertion_at_head() {
    // New bytes prepended; old blocks shift right and are found by rolling
    // (no jump). Because the insertion length is not a multiple of S, the
    // block grid at the tail shifts by 64 bytes: 62 full old blocks (7936
    // bytes) are reused and the remaining 64 old bytes ride along in one
    // literal run together with the 14-byte header — standard rsync residue.
    let basis = rng_bytes(7, 8_000);
    let mut target = b"HEADER-PREFIX-".to_vec();
    target.extend_from_slice(&basis);
    let s = 128u32;
    let stats = roundtrip(&basis, &target, s);
    assert_eq!(stats.blocks_matched, 62);
    assert_eq!(stats.bytes_from_basis, 62 * 128);
    assert_eq!(stats.literal_bytes, 14 + (basis.len() as u64 % s as u64));
    assert_eq!(
        stats.bytes_from_basis + stats.literal_bytes,
        target.len() as u64
    );
}

#[test]
fn insertion_at_head_block_aligned_is_near_perfect() {
    // When the inserted header length IS a multiple of the block size, every
    // old block stays grid-aligned and reuse is exact (zero old literal).
    let basis = rng_bytes(71, 128 * 50);
    let header = vec![0xABu8; 128 * 2];
    let mut target = header.clone();
    target.extend_from_slice(&basis);
    let stats = roundtrip(&basis, &target, 128);
    assert_eq!(stats.blocks_matched, 50);
    assert_eq!(stats.bytes_from_basis, basis.len() as u64);
    assert_eq!(stats.literal_bytes, header.len() as u64);
}

#[test]
fn insertion_in_middle() {
    // Insert a full block of fresh bytes: it cannot match anything and is
    // emitted as exactly that block of literals; blocks on both sides stay
    // reusable thanks to rolling.
    let basis = rng_bytes(9, 9_000);
    let insertion = rng_bytes(9001, 500);
    let mut target = basis.clone();
    target.splice(3_000..3_000, insertion.iter().copied());
    let stats = roundtrip(&basis, &target, 500);
    assert_eq!(stats.literal_bytes, 500);
    assert_eq!(
        stats.bytes_from_basis + stats.literal_bytes,
        target.len() as u64
    );
}

#[test]
fn small_insertion_in_middle_roundtrips() {
    // A sub-block insertion can absorb some surrounding old bytes into the
    // literal run (classic rsync behavior); correctness is still exact and
    // the accounting identity holds.
    let basis = rng_bytes(92, 9_000);
    let mut target = basis.clone();
    target.splice(3_333..3_333, b"*** INSERTED MIDDLE CHUNK ***".iter().copied());
    let stats = roundtrip(&basis, &target, 500);
    assert_eq!(
        stats.bytes_from_basis + stats.literal_bytes,
        target.len() as u64
    );
    assert!(stats.literal_bytes >= 29);
    assert!(stats.literal_bytes <= 29 + 2 * 500);
}

#[test]
fn local_deletion() {
    // Remove an interior run; blocks after the hole shift left and must match.
    let basis = rng_bytes(11, 12_000);
    let mut target = basis[..2_048].to_vec();
    target.extend_from_slice(&basis[3_072..]);
    let stats = roundtrip(&basis, &target, 256);
    assert_eq!(stats.bytes_from_basis, target.len() as u64);
    assert_eq!(stats.literal_bytes, 0);
}

#[test]
fn delete_and_insert_combined() {
    let basis = rng_bytes(13, 15_000);
    let mut target = basis[..1_000].to_vec();
    target.extend_from_slice(b"NEW-NEW-NEW");
    target.extend_from_slice(&basis[1_500..9_000]);
    target.extend_from_slice(b"END");
    target.extend_from_slice(&basis[10_000..]);
    let stats = roundtrip(&basis, &target, 333);
    assert_eq!(
        stats.bytes_from_basis + stats.literal_bytes,
        target.len() as u64
    );
    // most content should be reused
    assert!(stats.bytes_from_basis > (target.len() as f64 * 0.9) as u64);
}

#[test]
fn duplicate_blocks_all_resolve() {
    // Basis repeats the same 100-byte block many times: the weak map must
    // keep ALL duplicate candidates, and every target occurrence must resolve.
    let unit = rng_bytes(21, 100);
    let basis = unit.repeat(40);
    // Rearrange (same blocks, different order) + duplicates.
    let mut target = unit.repeat(5);
    target.extend_from_slice(&unit); // head duplication too
    target.extend_from_slice(&unit.repeat(34)[..]);
    let stats = roundtrip(&basis, &target, 100);
    assert_eq!(stats.blocks_matched as usize, target.len() / 100);
    assert_eq!(stats.literal_bytes, 0);
}

#[test]
fn empty_basis_and_empty_target() {
    let stats = roundtrip(b"", b"", 64);
    assert_eq!(stats.target_len, 0);
    assert_eq!(stats.blocks_matched, 0);

    // empty basis -> all-new target
    let stats = roundtrip(b"", b"fresh content here", 64);
    assert_eq!(stats.literal_bytes, 18);
}

#[test]
fn target_smaller_than_one_block() {
    let basis = rng_bytes(3, 500);
    // tail of basis equals the whole tiny target -> short block cannot match
    // (basis has no short block), so expect literal, but still exact rebuild.
    let target = rng_bytes(4, 50);
    let stats = roundtrip(&basis, &target, 128);
    assert_eq!(stats.literal_bytes, 50);
    assert_eq!(stats.blocks_matched, 0);
}

#[test]
fn short_trailing_block_is_reused() {
    // Basis 300 bytes with S=128 => short last block of 44 bytes.
    let basis = rng_bytes(5, 300);
    let mut target = rng_bytes(6, 128 * 2);
    target.extend_from_slice(&basis[256..]); // append the 44-byte tail
    let stats = roundtrip(&basis, &target, 128);
    assert_eq!(stats.bytes_from_basis, 44, "short tail reused");
}

#[test]
fn overlapping_full_match_and_short_residue() {
    // n=1000, S=300: basis = 3 full blocks + a 100-byte short block.
    // Build a target whose full block C matches at [650,950) AND whose final
    // 100 bytes equal the basis short block D. The two regions OVERLAP on
    // [900,950). The matcher must not emit both refs (that would make the op
    // stream reconstruct 1050 bytes and fail decode). It keeps the full-block
    // ref and sends the rest as literal; rebuild stays byte-exact.
    let s = 300u32;
    let c = rng_bytes(601, 300); // full block content
    let e = rng_bytes(602, 50); // final 50 target bytes
    let mut d = c[250..300].to_vec(); // D = C's last 50 ++ E
    d.extend_from_slice(&e);
    let mut basis = rng_bytes(600, 300);
    basis.extend_from_slice(&rng_bytes(603, 300));
    basis.extend_from_slice(&c); // block index 2
    basis.extend_from_slice(&d); // short block index 3, length 100
    assert_eq!(basis.len(), 1000);

    let prefix = rng_bytes(604, 650);
    let mut target = prefix;
    target.extend_from_slice(&c); // [650,950)
    target.extend_from_slice(&e); // [950,1000)
    assert_eq!(target.len(), 1000);
    assert_eq!(&target[900..1000], &d[..]); // tail really does equal D

    let stats = roundtrip(&basis, &target, s); // panics if the patch overlaps
    assert_eq!(
        stats.bytes_from_basis + stats.literal_bytes,
        target.len() as u64
    );
    // The full 300-byte block is reused; the overlapping 100-byte short block
    // is not double-referenced.
    assert_eq!(stats.blocks_matched, 1);
    assert_eq!(stats.bytes_from_basis, 300);
    assert_eq!(stats.literal_bytes, 700);
}

#[test]
fn random_mutations_roundtrip() {
    use std::collections::HashMap;
    let mut rng = 12345u64;
    let mut next = || {
        rng ^= rng << 13;
        rng ^= rng >> 7;
        rng ^= rng << 17;
        rng
    };
    for iter in 0..200 {
        let base_len = (next() % 5_000) as usize;
        let basis = rng_bytes(next(), base_len);
        let s = 1 + (next() % 400) as u32;

        // Random edit script.
        let mut target = basis.clone();
        for _ in 0..(next() % 6) {
            match next() % 3 {
                0 if !target.is_empty() => {
                    let at = (next() as usize) % target.len();
                    let del = 1 + (next() as usize) % 50;
                    target.drain(at..(at + del).min(target.len()));
                }
                1 => {
                    let at = (next() as usize) % (target.len() + 1);
                    let ins = rng_bytes(next(), 1 + (next() % 40) as usize);
                    target.splice(at..at, ins.iter().copied());
                }
                2 if !target.is_empty() => {
                    let at = (next() as usize) % target.len();
                    target[at] = (target[at] ^ 0xFF) | 1;
                }
                _ => {}
            }
        }
        let stats = roundtrip(&basis, &target, s);
        assert_eq!(
            stats.bytes_from_basis + stats.literal_bytes,
            target.len() as u64,
            "iteration {iter}"
        );
        // Sanity: matched bytes must never exceed the artifact.
        assert!(stats.bytes_from_basis <= target.len() as u64);
        let _ = HashMap::<u32, u32>::new();
    }
}

/// Construct two distinct blocks of length 5 with the SAME weak checksum.
///
/// With S=5:
///   a = Σ x[i]
///   b = 5x0 + 4x1 + 3x2 + 2x3 + x4
/// block B = block A + deltas (1, -3, 3, -1, 0):
///   Δa = 1 - 3 + 3 - 1 = 0
///   Δb = 5·1 + 4·(-3) + 3·3 + 2·(-1) = 5 - 12 + 9 - 2 = 0
/// All byte values stay in 0..=255.
#[test]
fn constructed_weak_collision_is_rejected_by_strong_hash() {
    let a: [u8; 5] = [10, 20, 30, 40, 50];
    let b: [u8; 5] = [11, 17, 33, 39, 50];
    assert_ne!(a, b);
    let wa = Rollsum::checksum(&a);
    let wb = Rollsum::checksum(&b);
    assert_eq!(
        wa, wb,
        "the two blocks are constructed to collide on the weak checksum"
    );
    assert_ne!(strong_hash(&a), strong_hash(&b), "but not on BLAKE3");

    // Basis = collision block A + random other block.
    let mut basis = a.to_vec();
    basis.extend_from_slice(&rng_bytes(99, 5)); // another 5-byte block
    // Target starts with collision block B then identical second block.
    let mut target = b.to_vec();
    target.extend_from_slice(&basis[5..]);

    let sig = Signature::build(&basis, 5).unwrap();
    let delta = diff(&sig, basis.len() as u64, &target);

    // The first window weak-hits block 0, must be strongly rejected.
    assert!(
        delta.stats.weak_hits >= 1,
        "weak collision should at least produce a weak hit, got {:?}",
        delta.stats
    );
    assert!(
        delta.stats.strong_rejections >= 1,
        "weak-colliding but different content must be rejected via strong hash"
    );
    // Block B is new literal content; block 1 is still referenced.
    assert_eq!(delta.stats.blocks_matched, 1);
    assert_eq!(delta.stats.literal_bytes, 5);

    let patch = Patch::new(
        &delta,
        5,
        basis.len() as u64,
        strong_hash(&basis),
    );
    let parsed = Patch::decode(&patch.encode()).unwrap();
    let rebuilt = apply_patch(&parsed, &basis).unwrap();
    assert_eq!(rebuilt, target, "collision case rebuilds byte-exact target");
}

#[test]
fn wrong_basis_is_rejected() {
    let basis = rng_bytes(77, 500);
    let target = rng_bytes(78, 500);
    let sig = Signature::build(&basis, 64).unwrap();
    let delta = diff(&sig, basis.len() as u64, &target);
    let patch = Patch::new(&delta, 64, basis.len() as u64, strong_hash(&basis));
    let wrong_basis = rng_bytes(79, 500);
    let err = apply_patch(&patch, &wrong_basis).unwrap_err();
    assert!(matches!(err, artifact_delta::Error::BasisMismatch));
}

#[test]
fn malformed_messages_are_rejected() {
    assert!(Signature::decode(b"").is_err());
    assert!(Signature::decode(b"NOPE..............").is_err());
    assert!(Patch::decode(b"").is_err());
    let basis = rng_bytes(80, 300);
    let sig = Signature::build(&basis, 50).unwrap();
    let delta = diff(&sig, basis.len() as u64, &basis);
    let patch = Patch::new(&delta, 50, basis.len() as u64, strong_hash(&basis));
    let mut bytes = patch.encode();
    let flip = bytes.len() - 6; // flip a byte inside the last REF/late op
    bytes[flip] ^= 0xA5;
    // Either decode fails or application fails the hash check.
    if let Ok(p) = Patch::decode(&bytes) {
        assert!(apply_patch(&p, &basis).is_err());
    }
}
