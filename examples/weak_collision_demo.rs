//! Standalone demonstration of the weak-checksum collision guarantee.
//!
//! It constructs two *different* blocks P and Q that share the same 32-bit
//! weak rolling checksum, places P in the basis and Q at the head of the
//! target, then shows that the delimiter does NOT emit a copy for Q: the
//! weak hit is rejected by the BLAKE3 strong hash and Q travels literally.
//! The patched result is still byte-identical to the target.
//!
//! Run: `cargo run --release --example weak_collision_demo`

use artifact_delta::checksum;
use artifact_delta::delta;
use artifact_delta::protocol::{Op, Signature};
use artifact_delta::hex;
use std::collections::HashMap;

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

fn find_collision(n: usize) -> (Vec<u8>, Vec<u8>) {
    let mut rng = Rng(0xC011_1510_DEAD_BEEF);
    let mut buckets: HashMap<u32, Vec<Vec<u8>>> = HashMap::new();
    loop {
        let mut b = vec![0u8; n];
        rng.fill(&mut b);
        let w = checksum::weak(&b);
        let bucket = buckets.entry(w).or_default();
        if let Some(existing) = bucket.iter().find(|e| **e != b) {
            assert_ne!(checksum::strong(existing), checksum::strong(&b));
            return (existing.clone(), b);
        }
        bucket.push(b);
    }
}

fn main() {
    let n = 64usize;
    let (p, q) = find_collision(n);

    println!("block size               : {n}");
    println!("P == Q                   : {}", p == q);
    println!("weak(P)                  : {:08x}", checksum::weak(&p));
    println!("weak(Q)                  : {:08x}  (collision)", checksum::weak(&q));
    println!("strong(P) BLAKE3         : {}", hex::encode(&checksum::strong(&p)));
    println!("strong(Q) BLAKE3         : {}", hex::encode(&checksum::strong(&q)));
    println!("strong equal             : {}", checksum::strong(&p) == checksum::strong(&q));

    // Basis starts with P; target starts with Q (same weak, different strong).
    let mut basis = p.clone();
    basis.extend_from_slice(&vec![9u8; 6 * n]);
    let mut target = q.clone();
    target.extend_from_slice(&vec![9u8; 6 * n]);

    let sig = Signature::build(&basis, n);
    assert_eq!(sig.blocks[0].weak, checksum::weak(&q));
    let d = delta::compute_delta(&target, &sig).unwrap();

    println!("\nfirst delta op           : {}", match &d.ops[0] {
        Op::Literal { .. } => "literal (weak hit rejected by strong hash) ✓",
        Op::Copy { block_index } => panic!("BUG: fake copy of block {block_index}"),
    });
    println!("literal bytes            : {} (Q had to travel)", d.literal_bytes());
    println!("copy blocks              : {}", d.copy_count());

    let out = delta::apply_delta(&basis, &d).unwrap();
    assert_eq!(out, target);
    println!("\npatched == target        : {}", out == target);
    println!("target BLAKE3            : {}", hex::encode(&checksum::strong(&target)));
}
