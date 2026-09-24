//! Randomized model test: run a stream of puts/deletes against both the disk
//! index and an in-memory `BTreeMap` and require them to agree at every step,
//! while structural invariants (directory refs, local depths) always hold.
//!
//! Uses a tiny deterministic LCG so a failing seed is reproducible.
use ext_hash_index::{Config, HashKind, Index};
use std::collections::BTreeMap;
use std::path::PathBuf;

struct Lcg(u64);
impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn tmp(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "ext-hash-model-{}-{}-{}",
        tag,
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn assert_invariants(idx: &mut Index, model_len: usize) {
    let st = idx.stats().unwrap();
    let gd = st.global_depth;
    let mut refs_sum = 0usize;
    let mut total_entries = 0usize;
    for b in &st.buckets {
        assert!(b.local_depth <= gd);
        assert!(b.entries <= st.bucket_capacity);
        assert_eq!(b.slot_refs, 1usize << (gd - b.local_depth));
        refs_sum += b.slot_refs;
        total_entries += b.entries;
    }
    assert_eq!(refs_sum, st.directory_slots);
    assert_eq!(total_entries, model_len);
}

fn run_scenario(capacity: usize, hash: HashKind, n_keys: u64, ops: u64, seed: u64) {
    let dir = tmp(&format!("{}-{}", capacity, seed));
    let path = dir.join("m.db");
    let cfg = Config {
        bucket_capacity: capacity,
        max_depth: 20,
        hash,
        key_max: 128,
        val_max: 256,
    };
    let mut idx = Index::create(&path, &cfg).unwrap();
    let mut model: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
    let mut rng = Lcg(seed);

    for step in 0..ops {
        let key = (rng.below(n_keys)).to_string();
        let kb = key.as_bytes();
        let op = rng.below(100);
        if op < 62 {
            // put
            let val = format!("val{}-{}", rng.below(1000), step);
            let inserted = idx.put(kb, val.as_bytes()).unwrap();
            assert_eq!(inserted, !model.contains_key(kb), "step {}", step);
            model.insert(kb.to_vec(), val.as_bytes().to_vec());
        } else {
            // delete
            let existed = idx.delete(kb).unwrap();
            assert_eq!(existed, model.remove(kb).is_some(), "step {}", step);
        }

        // Full scan agreement.
        let st = idx.stats().unwrap();
        assert_invariants_core(&st, &mut idx, &model, step);
    }

    // Reopen and re-verify (plain open, no recovery).
    drop(idx);
    let mut idx = Index::open(&path).unwrap();
    assert!(idx.recovered_intent().is_none());
    for (k, v) in &model {
        assert_eq!(idx.get(k).unwrap().as_ref(), Some(v));
    }
    assert_invariants(&mut idx, model.len());
}

fn assert_invariants_core(
    st: &ext_hash_index::Stats,
    idx: &mut Index,
    model: &BTreeMap<Vec<u8>, Vec<u8>>,
    step: u64,
) {
    let gd = st.global_depth;
    let mut refs_sum = 0usize;
    let mut seen: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
    for b in &st.buckets {
        assert!(b.local_depth <= gd, "step {}", step);
        assert!(b.entries <= st.bucket_capacity, "step {}", step);
        assert_eq!(b.slot_refs, 1usize << (gd - b.local_depth), "step {}", step);
        refs_sum += b.slot_refs;
        for k in &b.keys {
            let v = idx.get(k.as_bytes()).unwrap().expect("bucket key gettable");
            assert!(
                seen.insert(k.as_bytes().to_vec(), v).is_none(),
                "key duplicated across buckets at step {}",
                step
            );
        }
    }
    assert_eq!(refs_sum, st.directory_slots, "step {}", step);
    assert_eq!(seen.len(), model.len(), "step {}", step);
    assert_eq!(&seen, model, "step {}", step);
}

#[test]
fn model_small_keyspace_many_doublings_and_shrinks() {
    // Small key space with heavy churn forces repeated doublings and
    // cascading merges/shrinks; bucket reuse via the free list is exercised.
    run_scenario(2, HashKind::U64LowBits, 16, 4000, 12345);
}

#[test]
fn model_capacity4_lowbits() {
    run_scenario(4, HashKind::U64LowBits, 80, 5000, 777);
}

#[test]
fn model_capacity8_fnv() {
    run_scenario(8, HashKind::Fnv1a64, 300, 6000, 424242);
}

#[test]
fn model_capacity1_fnv_stress() {
    // Capacity 1: every insert doubles+split; deletes always merge.
    run_scenario(1, HashKind::Fnv1a64, 24, 2000, 99);
}

#[test]
fn model_u64_collision_patterns_survive() {
    // Keys that differ only in high bits still remain distinct with FNV;
    // u64lowbits with a small key space is the structured case above.
    run_scenario(3, HashKind::Fnv1a64, 64, 3000, 555);
}
