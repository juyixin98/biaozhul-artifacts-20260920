//! Shared helpers for integration tests: in-memory table construction and a
//! small deterministic PRNG for reproducible "random" suites.

#![allow(dead_code)]

use psst::io::{MemReader, MemStore, MemWriter};
use psst::table::{Options, Table, TableBuilder};
use psst::Result;
use std::collections::BTreeMap;

/// xorshift64* — tiny, fast, deterministic; no rand dependency needed.
pub struct Rng {
    state: u64,
}

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng {
            state: if seed == 0 {
                0x9e37_79b9_7f4a_7c15
            } else {
                seed
            },
        }
    }

    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.state = x;
        x.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    pub fn below(&mut self, bound: u64) -> u64 {
        self.next_u64() % bound
    }

    /// Fill `out` with `n` pseudo-random bytes.
    pub fn bytes(&mut self, out: &mut Vec<u8>, n: usize) {
        out.clear();
        while out.len() < n {
            let chunk = self.next_u64().to_le_bytes();
            let take = (n - out.len()).min(8);
            out.extend_from_slice(&chunk[..take]);
        }
    }
}

/// Build a table in memory from already-sorted unique pairs.
pub fn build_mem(sorted: &[(Vec<u8>, Vec<u8>)], options: Options) -> Result<(MemStore, u64)> {
    let store = MemStore::new();
    let writer = MemWriter::new(store.clone());
    let mut builder = TableBuilder::new(writer, options)?;
    for (k, v) in sorted {
        builder.add(k, v)?;
    }
    let stats = builder.finish()?;
    assert_eq!(stats.bytes as usize, store.len());
    Ok((store, stats.bytes))
}

pub fn open_mem(store: &MemStore) -> Result<Table<MemReader>> {
    let size = store.len() as u64;
    Table::open(MemReader::new(store.clone()), size)
}

/// Deterministic random sorted map of `count` entries.
pub fn random_map(rng: &mut Rng, count: usize) -> BTreeMap<Vec<u8>, Vec<u8>> {
    let mut map = BTreeMap::new();
    while map.len() < count {
        let klen = 1 + (rng.below(24) as usize);
        let mut k = Vec::new();
        rng.bytes(&mut k, klen);
        let vlen = rng.below(32) as usize;
        let mut v = Vec::new();
        rng.bytes(&mut v, vlen);
        map.insert(k, v);
    }
    map
}

/// BTreeMap contents as sorted pairs.
pub fn map_pairs(map: &BTreeMap<Vec<u8>, Vec<u8>>) -> Vec<(Vec<u8>, Vec<u8>)> {
    map.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
}

/// Options that force small blocks so the index is genuinely sparse.
pub fn tiny_options() -> Options {
    Options {
        block_size: 120,
        restart_interval: 4,
    }
}
