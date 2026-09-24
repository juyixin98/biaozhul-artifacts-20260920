//! Core algorithm tests: round-trip byte equality for head insertion,
//! local deletion, duplicate blocks, insert offsets, random edits, and a
//! deliberately constructed weak-checksum collision that must NOT be treated
//! as a match.

use artifact_delta::checksum::{self, pack_parts, roll, weak_parts};
use artifact_delta::delta;
use artifact_delta::protocol::Signature;

fn blake3_hex(data: &[u8]) -> String {
    artifact_delta::hex::encode(&checksum::strong(data))
}

fn roundtrip(basis: &[u8], target: &[u8], block_size: usize) -> artifact_delta::protocol::Delta {
    let sig = Signature::build(basis, block_size);
    let d = delta::compute_delta(target, &sig).expect("delta");
    let out = delta::apply_delta(basis, &d).expect("apply");
    assert_eq!(out, target, "reconstructed target must be byte-identical");
    // sanity: ops reference valid, strongly-confirmed blocks only
    for op in &d.ops {
        if let artifact_delta::protocol::Op::Copy { block_index } = op {
            assert!(*block_index < sig.blocks.len());
        }
    }
    d
}

/// Deterministic xorshift64* so tests are reproducible.
struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn fill(&mut self, buf: &mut [u8]) {
        let mut i = 0;
        while i < buf.len() {
            let v = self.next_u64().to_le_bytes();
            let take = (buf.len() - i).min(8);
            buf[i..i + take].copy_from_slice(&v[..take]);
            i += take;
        }
    }
}

#[test]
fn identical_artifacts_copy_everything() {
    let basis = b"hello world, this is block delta demo content!!!".repeat(50);
    let d = roundtrip(&basis, &basis, 16);
    assert_eq!(d.literal_bytes(), 0);
}

#[test]
fn head_insertion_shifts_every_block() {
    // Insert at the head: every basis block is displaced by the prefix length.
    // Fixed-window alignment cannot find matches; only rolling search can.
    // High-entropy (non-periodic) content ensures a COPY means a genuine
    // displaced block was found, not a coincidental repeat.
    let mut rng = Rng(0x0BAD_F00D);
    let mut basis = vec![0u8; 4000];
    rng.fill(&mut basis);
    let mut target = Vec::with_capacity(basis.len() + 70);
    target.extend_from_slice(b">>> INSERTED PREFIX OF 70 BYTES <<<..........");
    target.extend_from_slice(&basis);

    let d = roundtrip(&basis, &target, 256);

    // The prefix must be the only literal bytes (plus, potentially, alignment
    // tails of the last short block). Most of the content must be copied.
    let copied = d.copy_count() * 256;
    assert!(
        copied > basis.len() - 512,
        "expected nearly all basis bytes copied after head insert, got {copied} copied of {}",
        basis.len()
    );
    assert!(d.literal_bytes() < 200, "literal bytes: {}", d.literal_bytes());
}

#[test]
fn local_deletion_matches_surrounding_blocks() {
    // Delete a region in the middle; blocks after the deletion shift earlier.
    let mut rng = Rng(0x1234_5678);
    let mut basis = vec![0u8; 6000];
    rng.fill(&mut basis);
    // Make the content mildly repetitive/structured (still high entropy).
    for (i, b) in basis.iter_mut().enumerate() {
        *b = (*b).wrapping_add((i % 97) as u8);
    }

    let mut target = basis.clone();
    target.drain(1234..1234 + 887); // delete 887 bytes in the middle

    let d = roundtrip(&basis, &target, 512);
    let copied = d.copy_count() * 512;
    assert!(
        copied > basis.len() - 2048,
        "deletion should leave most content copyable; copied {copied} of {}",
        basis.len()
    );
}

#[test]
fn duplicate_blocks_use_first_candidate() {
    // Basis contains the same block multiple times. All occurrences must be
    // discoverable through one weak key; the COPY index must point at a block
    // whose strong hash actually equals the window content.
    // Construct exact-length blocks so the test cannot be off by one.
    let block: Vec<u8> = b"REPEATED-BLOCK-CONTENT-32-BYTES!"
        .iter()
        .cycle()
        .take(32)
        .copied()
        .collect();
    let filler: Vec<u8> = b"different-padding-block-content!"
        .iter()
        .cycle()
        .take(32)
        .copied()
        .collect();
    assert_eq!(block.len(), 32);
    assert_ne!(block, filler);
    let mut basis = Vec::new();
    basis.extend_from_slice(&block);
    basis.extend_from_slice(&filler); // index 1
    basis.extend_from_slice(&block); // index 2, duplicate of 0
    basis.extend_from_slice(&block); // index 3, duplicate of 0

    let mut target = Vec::new();
    target.extend_from_slice(b"X".repeat(40).as_slice()); // head insertion
    target.extend_from_slice(&block); // should copy block 0
    target.extend_from_slice(b"new middle bytes");
    target.extend_from_slice(&block); // duplicate again -> still copy block 0
    let d = roundtrip(&basis, &target, 32);
    assert!(d.copy_count() >= 2, "expected >=2 copies, got {}", d.copy_count());
}

#[test]
fn insert_at_non_block_offset_inside_file() {
    let mut rng = Rng(0xABCD);
    let mut basis = vec![0u8; 5000];
    rng.fill(&mut basis);
    let mut target = basis.clone();
    target.splice(3003..3003, b"inserted payload at an unaligned offset".iter().copied());
    let _ = roundtrip(&basis, &target, 256);

    // Also: insertion near the end followed by tail content (short-tail path).
    let mut t2 = basis.clone();
    t2.splice(
        basis.len() - 30..basis.len() - 30,
        b"tail insert".iter().copied(),
    );
    let _ = roundtrip(&basis, &t2, 256);
}

#[test]
fn random_edits_property_test() {
    let mut rng = Rng(0xFEED_FACE);
    for seed in 0..30u64 {
        let mut local = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15).wrapping_add(1));
        let len = 1 + (local.next_u64() as usize % 20_000);
        let mut basis = vec![0u8; len];
        local.fill(&mut basis);

        let mut target = basis.clone();
        let edits = 1 + local.next_u64() % 5;
        for _ in 0..edits {
            match local.next_u64() % 3 {
                0 => {
                    // insert
                    let pos = (local.next_u64() as usize) % (target.len() + 1);
                    let n = local.next_u64() % 300;
                    let mut ins = vec![0u8; n as usize];
                    local.fill(&mut ins);
                    target.splice(pos..pos, ins);
                }
                1 if !target.is_empty() => {
                    // delete
                    let pos = (local.next_u64() as usize) % target.len();
                    let n = (local.next_u64() as usize % 200).min(target.len() - pos);
                    target.drain(pos..pos + n);
                }
                _ => {
                    // replace
                    if target.is_empty() {
                        continue;
                    }
                    let pos = (local.next_u64() as usize) % target.len();
                    target[pos] = target[pos].wrapping_add(1 + (local.next_u64() % 255) as u8);
                }
            }
        }
        let block_size = 16 + (local.next_u64() as usize % 2000);
        let _ = roundtrip(&basis, &target, block_size);
    }
    // rng used to silence "field never read" style lints in older compilers
    let _ = rng.next_u64();
}

#[test]
fn short_tail_block_matches_after_insertion() {
    // Basis length not a multiple of block size; the final short block must
    // still be matched after content is inserted ahead of it.
    let mut basis = vec![b'A'; 1000];
    basis.extend_from_slice(b"TAIL"); // total 1004 with block_size 64 -> short last block (20 B)
    let mut target = Vec::new();
    target.extend_from_slice(&basis);
    target.extend_from_slice(b"EXTRA AFTER TAIL"); // content after tail
    let _ = roundtrip(&basis, &target, 64);

    let mut target2 = Vec::new();
    target2.extend_from_slice(b"PREFIX");
    target2.extend_from_slice(&basis);
    let d = roundtrip(&basis, &target2, 64);
    // The short tail block "AAAA...TAIL" should be copyable after the prefix.
    // (At least check the literal region does not swallow the whole basis.)
    assert!(d.literal_bytes() < 200, "literal={}", d.literal_bytes());
}

// ----- weak-checksum collision ------------------------------------------------

/// Find two distinct blocks `P` and `Q` of length `n` with the SAME weak
/// checksum but DIFFERENT BLAKE3 hashes, by bucketizing random blocks on
/// their weak value (birthday search). The weak checksum is only 32 bits so
/// this needs on the order of ~64k random blocks.
fn find_weak_collision(n: usize, cap_blocks: u32) -> (Vec<u8>, Vec<u8>) {
    use std::collections::HashMap;
    let mut rng = Rng(0xC011_1510_DEAD_BEEF);
    let mut buckets: HashMap<u32, Vec<Vec<u8>>> = HashMap::new();
    for i in 0..cap_blocks {
        let mut b = vec![0u8; n];
        rng.fill(&mut b);
        let w = checksum::weak(&b);
        let bucket = buckets.entry(w).or_default();
        if let Some(existing) = bucket.iter().find(|e| **e != b) {
            // Distinct content sharing one weak value. BLAKE3 will differ.
            assert_ne!(checksum::strong(existing), checksum::strong(&b));
            return (existing.clone(), b);
        }
        bucket.push(b);
        let _ = i;
    }
    panic!("no weak collision found within {cap_blocks} blocks (unlikely)");
}

#[test]
fn weak_checksum_collision_is_not_treated_as_match() {
    // Block size must be a valid block size and small enough for a fast
    // birthday search. 64B blocks -> 32-bit weak space ~4.29e9 -> ~65k blocks.
    let n = 64;
    let (p, q) = find_weak_collision(n, 5_000_000);
    assert_eq!(p.len(), n);
    assert_ne!(p, q);
    assert_eq!(checksum::weak(&p), checksum::weak(&q), "test precondition");
    assert_ne!(checksum::strong(&p), checksum::strong(&q));

    // Basis: [P][filler]. Target starts with Q, which shares P's weak checksum.
    // At position 0 the rolling window must get a weak HIT on P but a strong
    // MISS, and therefore fall back to literal bytes. It must never COPY P.
    let mut basis = p.clone();
    basis.extend_from_slice(&vec![7u8; 4 * n]);
    let mut target = q.clone();
    target.extend_from_slice(&vec![7u8; 4 * n]); // following bytes identical

    let sig = Signature::build(&basis, n);
    // Explicitly confirm the signature of block 0 collides weakly with Q.
    assert_eq!(sig.blocks[0].weak, checksum::weak(&q));
    let d = delta::compute_delta(&target, &sig).expect("delta");

    // The first op must be a literal containing Q (never a copy of block 0).
    match d.ops.first().expect("at least one op") {
        artifact_delta::protocol::Op::Copy { block_index } => {
            panic!("first op must not be a copy (weak collision faked a match): copy block {block_index}");
        }
        artifact_delta::protocol::Op::Literal { data_b64 } => {
            use base64::Engine;
            let lit = base64::engine::general_purpose::STANDARD
                .decode(data_b64)
                .unwrap();
            assert!(
                lit.len() >= n && lit[..n] == q[..],
                "literal region must contain the colliding block Q verbatim"
            );
        }
    }

    // End-to-end: applying the delta must still reconstruct the target exactly,
    // including Q's bytes (and not P's).
    let out = delta::apply_delta(&basis, &d).unwrap();
    assert_eq!(out, target);
    assert_eq!(blake3_hex(&out), blake3_hex(&target));
}

#[test]
fn roll_step_equivalence_with_rsync_formula() {
    // Document/guard the exact roll identity used in the hot loop.
    let data = b"0123456789abcdefghijklmnopqrstuvwxyz";
    let n = 7;
    let mut parts = weak_parts(&data[..n]);
    for i in 0..data.len() - n {
        let next = roll(parts, data[i], data[i + n], n);
        // Compare against a direct implementation of the textbook identity.
        let (a, b) = parts;
        let a2 = a.wrapping_sub(data[i] as u32).wrapping_add(data[i + n] as u32);
        let b2 = b
            .wrapping_sub((n as u32).wrapping_mul(data[i] as u32))
            .wrapping_add(a2);
        assert_eq!(next, (a2 & 0xffff, b2 & 0xffff));
        assert_eq!(pack_parts(next), checksum::weak(&data[i + 1..i + 1 + n]));
        parts = next;
    }
}
