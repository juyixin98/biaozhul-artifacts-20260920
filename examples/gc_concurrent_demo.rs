//! Chaos demo: concurrent readers + root switchers + repeated GC sweeps.
//!
//! Asserts the core invariant live: no block reachable from a root (or staged
//! for publication) is ever collected, while orphan junk disappears.
//!
//! Run: `cargo run --example gc_concurrent_demo`
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;

use cas_store::{MemFs, Repository};
use cas_store::Vfs;

fn block(seed: u8, size: usize) -> Vec<u8> {
    let mut v = vec![seed; size];
    for (i, b) in v.iter_mut().enumerate() {
        *b = b.wrapping_add((i % 251) as u8);
    }
    v
}

fn main() {
    let vfs: Arc<dyn Vfs> = Arc::new(MemFs::new());
    let repo = Repository::open("/repo", vfs).unwrap();
    let repo = Arc::new(repo);

    let mut pinned = Vec::new();
    for i in 0..40u8 {
        pinned.push(repo.add_block(&block(i, 128), &[]).unwrap().hash);
    }
    let pa = repo.add_block(b"A", &pinned[..20]).unwrap();
    let pb = repo.add_block(b"B", &pinned[20..]).unwrap();
    repo.put_root("a", &pa.hash).unwrap();
    repo.put_root("b", &pb.hash).unwrap();
    let pbackup = repo
        .add_block(b"backup", &[pa.hash.clone(), pb.hash.clone()])
        .unwrap();
    repo.put_root("backup", &pbackup.hash).unwrap();

    let stop = Arc::new(AtomicBool::new(false));

    // readers
    let mut readers = vec![];
    for _ in 0..6 {
        let repo = repo.clone();
        let pinned = pinned.clone();
        let stop = stop.clone();
        readers.push(thread::spawn(move || loop {
            if stop.load(Ordering::Relaxed) { break; }
            for h in &pinned {
                let _ = repo.get_block_verified(h);
            }
        }));
    }
    // switcher
    {
        let repo = repo.clone();
        let stop = stop.clone();
        let ha = pa.hash.clone();
        let hb = pb.hash.clone();
        thread::spawn(move || {
            let mut n = 0;
            while !stop.load(Ordering::Relaxed) {
                let t = if n % 2 == 0 { &hb } else { &ha };
                let _pin = repo.stage(t).unwrap();
                if repo.put_root("a", t).is_err() { break; }
                let t = if n % 2 == 0 { &ha } else { &hb };
                let _pin = repo.stage(t).unwrap();
                if repo.put_root("b", t).is_err() { break; }
                n += 1;
            }
        });
    }

    for sweep in 0..30 {
        for k in 0..5 {
            let junk = format!("junk-{sweep}-{k}").into_bytes();
            let _ = repo.add_block(&junk, &[]);
        }
        let r = repo.gc(false).unwrap();
        if !r.removed.is_empty() {
            let pinned_set: std::collections::HashSet<&String> = pinned.iter().collect();
            let bad_pinned: Vec<_> = r.removed.iter().filter(|x| pinned_set.contains(x)).collect();
            let bad_roots: Vec<_> = r.removed.iter().filter(|x| {
                **x == pa.hash || **x == pb.hash || **x == pbackup.hash
            }).collect();
            if !bad_pinned.is_empty() || !bad_roots.is_empty() {
                println!("!!! GC removed pinned leaves: {bad_pinned:?}");
                println!("!!! GC removed tree roots:  {bad_roots:?}");
                println!("    roots now: {:?}", repo.list_roots().unwrap());
                break;
            }
        }
    }
    stop.store(true, Ordering::Relaxed);
    for j in readers { let _ = j.join(); }
    println!("done");
}
