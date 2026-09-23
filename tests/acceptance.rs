//! Acceptance criteria, exercised against the real on-disk repository.
//!
//! Covers:
//! - incremental root equals a full rebuild root;
//! - first/last range proofs verify, verifier reads no other blocks;
//! - empty file;
//! - forged length;
//! - displaced/misaligned proof nodes;
//! - incremental update touches only O(height) level nodes;
//! - crash recovery through every journaled step on a real filesystem.

use std::path::{Path, PathBuf};

use merkle_store::merkle::{self, empty_root, leaf_hash, ProofError, ProofNode, RangeProof};
use merkle_store::store::Store;
use merkle_store::vfs::{FaultyVfs, Op, RealVfs};

fn full_rebuild_root(data: &[u8], bs: u64) -> [u8; 32] {
    let leaves: Vec<[u8; 32]> = data
        .chunks(bs as usize)
        .enumerate()
        .map(|(i, c)| leaf_hash(i, c))
        .collect();
    merkle::rebuild_root(&leaves)
}

fn tempdir(tag: &str) -> PathBuf {
    let mut d = std::env::temp_dir();
    let unique = format!(
        "merkle-store-it-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    d.push(unique);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn open_real(dir: &Path) -> Store<RealVfs> {
    Store::open(RealVfs::new(), dir).unwrap()
}

// 1. Incremental root == full rebuild root -------------------------------

#[test]
fn incremental_matches_full_rebuild_after_mixed_updates() {
    let dir = tempdir("incr");
    let bs = 7u64;
    let mut s = Store::create(RealVfs::new(), &dir, bs).unwrap();

    let payload: Vec<u8> = (0..137u32).map(|i| (i % 251) as u8).collect();
    let mut expected = Vec::new();
    for (i, chunk) in payload.chunks(bs as usize).enumerate() {
        s.put_block(i as u64, chunk).unwrap();
        expected.extend_from_slice(chunk);
        assert_eq!(s.root().unwrap(), full_rebuild_root(&expected, bs));
    }

    // Overwrite the first, a middle, and the last block.
    let overwrite = |s: &mut Store<RealVfs>, expected: &mut Vec<u8>, i: usize, bytes: &[u8]| {
        s.put_block(i as u64, bytes).unwrap();
        let start = i * bs as usize;
        // Only the last block may shrink the payload.
        let last_idx = expected.len().div_ceil(bs as usize) - 1;
        if i == last_idx && start + bytes.len() < expected.len() {
            expected.truncate(start + bytes.len());
        }
        expected[start..start + bytes.len()].copy_from_slice(bytes);
        assert_eq!(s.root().unwrap(), full_rebuild_root(expected, bs));
    };
    overwrite(&mut s, &mut expected, 0, b"ZZZZZZZ");
    overwrite(&mut s, &mut expected, 10, b"middle!");
    let n = expected.len().div_ceil(bs as usize);
    overwrite(&mut s, &mut expected, n - 1, b"tail"); // shortens last block

    // Independent verification path: force a full rebuild and compare roots.
    let committed = s.root().unwrap();
    s.force_full_rebuild().unwrap();
    assert_eq!(s.root().unwrap(), committed);
    assert_eq!(s.root().unwrap(), full_rebuild_root(&expected, bs));
}

// 2. First / last range proofs -------------------------------------------

#[test]
fn first_and_last_range_proofs_verify_without_reading_other_blocks() {
    let dir = tempdir("ranges");
    let bs = 4u64;
    let data = b"the quick brown fox jumps over"; // 30 bytes -> 8 blocks
    let mut s = Store::create(RealVfs::new(), &dir, bs).unwrap();
    s.reset(data).unwrap();
    let root = s.root().unwrap();
    let n = s.block_count();
    assert_eq!(n, 8);

    for (st, en) in [(0u64, 1u64), (n - 1, n)] {
        let proof = s.prove_range(st, en).unwrap();
        // The verifier is given ONLY the requested block bytes.
        let blocks: Vec<Vec<u8>> = (st..en).map(|i| s.read_block(i).unwrap()).collect();
        merkle::verify(&proof, bs, data.len() as u64, &root, &blocks).unwrap();
    }

    // A multi-block interior range also verifies.
    let proof = s.prove_range(2, 6).unwrap();
    let blocks: Vec<Vec<u8>> = (2..6).map(|i| s.read_block(i).unwrap()).collect();
    merkle::verify(&proof, bs, data.len() as u64, &root, &blocks).unwrap();

    // The last block is the short one (30 = 7*4 + 2).
    let last = s.read_block(n - 1).unwrap();
    assert_eq!(last.len(), 2);
}

// 3. Empty file ------------------------------------------------------------

#[test]
fn empty_file_has_sentinel_root_and_verifies() {
    let dir = tempdir("empty");
    let s = Store::create(RealVfs::new(), &dir, 16).unwrap();
    assert_eq!(s.block_count(), 0);
    assert_eq!(s.data_len(), 0);
    assert_eq!(s.root().unwrap(), empty_root());

    // The canonical empty proof: range [0,0), no nodes, against the sentinel.
    let proof = RangeProof {
        n: 0,
        start: 0,
        end: 0,
        nodes: vec![],
    };
    merkle::verify(&proof, 16, 0, &empty_root(), &[]).unwrap();

    // Empty proof against a non-empty root must fail.
    assert_eq!(
        merkle::verify(&proof, 16, 0, &[1u8; 32], &[]).unwrap_err(),
        ProofError::EmptyRootMismatch
    );

    // Building data then resetting to empty restores the sentinel.
    let dir2 = tempdir("empty2");
    let mut s2 = Store::create(RealVfs::new(), &dir2, 16).unwrap();
    s2.reset(b"hello").unwrap();
    assert_ne!(s2.root().unwrap(), empty_root());
    s2.reset(b"").unwrap();
    assert_eq!(s2.root().unwrap(), empty_root());
}

// 4. Forged length ---------------------------------------------------------

#[test]
fn forged_length_is_rejected() {
    let dir = tempdir("forgelen");
    let bs = 4u64;
    let data = b"abcde"; // 5 bytes -> 2 blocks: "abcd", "e"
    let mut s = Store::create(RealVfs::new(), &dir, bs).unwrap();
    s.reset(data).unwrap();
    let root = s.root().unwrap();
    assert_eq!(s.block_count(), 2);

    // Honest last-block proof (block 1, one byte).
    let proof = s.prove_range(1, 2).unwrap();
    let good = vec![b"e".to_vec()];
    merkle::verify(&proof, bs, 5, &root, &good).unwrap();

    // 4a. Declared length that implies a different leaf count.
    assert_eq!(
        merkle::verify(&proof, bs, 8, &root, &good).unwrap_err(),
        ProofError::LengthMismatch
    );
    assert_eq!(
        merkle::verify(&proof, bs, 4, &root, &good).unwrap_err(),
        ProofError::LengthMismatch
    );

    // 4b. Padding the short last block to full size with a consistent forged
    // length is caught at the leaf hash (committed block length/content).
    assert!(matches!(
        merkle::verify(&proof, bs, 8, &root, &[b"e\0\0\0".to_vec()]).unwrap_err(),
        ProofError::LengthMismatch | ProofError::LeafMismatch { .. }
    ));

    // 4c. Wrong trusted root with an otherwise valid proof.
    assert_eq!(
        merkle::verify(&proof, bs, 5, &[0u8; 32], &good).unwrap_err(),
        ProofError::RootMismatch
    );
}

// 5. Displaced / misaligned proofs ----------------------------------------

#[test]
fn misaligned_proofs_are_rejected() {
    let dir = tempdir("misalign");
    let bs = 4u64;
    let data = b"abcdefghijkl"; // 12 bytes, 3 blocks
    let mut s = Store::create(RealVfs::new(), &dir, bs).unwrap();
    s.reset(data).unwrap();
    let root = s.root().unwrap();
    let good = s.prove_range(1, 2).unwrap();
    let blocks = vec![b"efgh".to_vec()];
    merkle::verify(&good, bs, 12, &root, &blocks).unwrap();

    // 5a. Swap sibling nodes.
    let mut p = good.clone();
    p.nodes.swap(1, 2);
    assert!(matches!(
        merkle::verify(&p, bs, 12, &root, &blocks).unwrap_err(),
        ProofError::MisalignedNode { .. }
    ));

    // 5b. Retag a node to the wrong index.
    let mut p = good.clone();
    p.nodes[1].index = 5;
    assert!(matches!(
        merkle::verify(&p, bs, 12, &root, &blocks).unwrap_err(),
        ProofError::MisalignedNode { .. }
    ));

    // 5c. Truncated / extended proofs.
    let mut p = good.clone();
    p.nodes.pop();
    assert_eq!(
        merkle::verify(&p, bs, 12, &root, &blocks).unwrap_err(),
        ProofError::ProofTooShort
    );
    let mut p = good.clone();
    p.nodes.push(ProofNode {
        level: 3,
        index: 3,
        hash: [9u8; 32],
    });
    assert_eq!(
        merkle::verify(&p, bs, 12, &root, &blocks).unwrap_err(),
        ProofError::ProofTooLong
    );

    // 5d. Present a proof generated for a different file length.
    let mut s2 = Store::create(RealVfs::new(), &tempdir("misalign2"), bs).unwrap();
    s2.reset(b"abcdefghijklm").unwrap(); // 13 bytes, still 4? no -> 4 blocks
    let foreign = s2.prove_range(1, 2).unwrap();
    // The foreign proof's tree shape may still structurally verify up to a
    // different root; it must at minimum fail the root binding.
    let err = merkle::verify(&foreign, bs, 12, &root, &blocks).unwrap_err();
    assert!(
        matches!(
            err,
            ProofError::RootMismatch
                | ProofError::LengthMismatch
                | ProofError::MisalignedNode { .. }
                | ProofError::ProofTooShort
                | ProofError::LeafMismatch { .. }
        ),
        "unexpected acceptance of foreign proof: {err:?}"
    );
}

// 6. Incremental update cost ----------------------------------------------

#[test]
fn incremental_update_writes_only_one_path() {
    let inner = merkle_store::vfs::MemVfs::new();
    let bs = 1u64;
    // Build a 256-leaf tree via a full reset.
    {
        let mut s = Store::create(inner.clone(), Path::new("/cost"), bs).unwrap();
        let data: Vec<u8> = (0..256u16).map(|i| i as u8).collect();
        s.reset(&data).unwrap();
    }
    // Overwrite a single leaf on an instrumented VFS.
    let vfs = FaultyVfs::new(inner.clone());
    let mut s = Store::open(vfs.clone(), Path::new("/cost")).unwrap();
    s.put_block(100, &[0xAB]).unwrap();

    // Exactly one 32-byte write per tree level on the affected path; the
    // tree has 256 leaves -> 9 levels (0..=8).
    let mut level_writes = 0u64;
    for lvl in 0..16 {
        let path = format!("level-{lvl}.bin");
        let n = vfs.call_count(Op::WriteAt, &path);
        level_writes += n;
        if lvl <= 8 {
            assert_eq!(n, 1, "expected exactly one write to {path}");
        } else {
            assert_eq!(n, 0, "no level {lvl} file should exist");
        }
    }
    assert_eq!(level_writes, 9, "one node per level on the path");
    // data.bin gets exactly one pwrite as well.
    assert_eq!(vfs.call_count(Op::WriteAt, "data.bin"), 1);
}

// 7. Crash recovery through every journaled step on a real filesystem ------

#[test]
fn real_fs_crash_replay_at_every_step() {
    // Two-block append; instrumented writes fail at call N. After reopen the
    // repository is internally consistent (root matches a full rebuild).
    for rule in 1u64..=5 {
        let dir = tempdir(&format!("crash-{rule}"));
        {
            let mut base = Store::create(RealVfs::new(), &dir, 4).unwrap();
            base.put_block(0, b"abcd").unwrap();
        }
        {
            let vfs = FaultyVfs::new(RealVfs::new())
                .fail_on(Op::WriteAtomic, None, rule)
                .fail_on(Op::WriteAt, None, rule);
            let mut s = Store::open(vfs, &dir).unwrap();
            let _ = s.put_block(1, b"efgh");
        }
        let mut s = open_real(&dir);
        let data = s.read_data().unwrap();
        let root = s.root().unwrap();
        // Whatever survived, the committed root must equal a full rebuild of
        // exactly the bytes present in data.bin.
        assert_eq!(root, full_rebuild_root(&data, 4), "rule {rule}");
        assert!(
            data == b"abcd" || data == b"abcdefgh",
            "rule {rule}: {data:?}"
        );
        // The store is usable after recovery.
        s.force_full_rebuild().unwrap();
        assert_eq!(s.root().unwrap(), full_rebuild_root(&data, 4));
    }
}

// 8. Cross-check verifier against the store over many shapes --------------

#[test]
fn exhaustive_range_verification_small_trees() {
    let dir = tempdir("exhaustive");
    let _ = &dir;
    for bs in 1u64..=5 {
        for nblocks in 0usize..10 {
            let d = tempdir(&format!("ex-{bs}-{nblocks}"));
            let payload: Vec<u8> = (0..(nblocks as u64 * bs))
                .map(|i| (i % 199) as u8)
                .collect();
            let mut s = Store::create(RealVfs::new(), &d, bs).unwrap();
            s.reset(&payload).unwrap();
            let root = s.root().unwrap();
            if nblocks == 0 {
                let p = s.prove_range(0, 0).unwrap();
                merkle::verify(&p, bs, 0, &root, &[]).unwrap();
                continue;
            }
            for st in 0..nblocks {
                for en in st + 1..=nblocks {
                    let p = s.prove_range(st as u64, en as u64).unwrap();
                    let blocks: Vec<Vec<u8>> =
                        (st..en).map(|i| s.read_block(i as u64).unwrap()).collect();
                    merkle::verify(&p, bs, payload.len() as u64, &root, &blocks)
                        .unwrap_or_else(|e| panic!("bs={bs} n={nblocks} {st}..{en}: {e}"));
                }
            }
        }
    }
}
