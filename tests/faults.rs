//! Failure injection (via [`FaultFs`]) and integrity enforcement.

mod common;

use std::sync::Arc;

use cas_store::{Fault, FaultOp, RealFs, Repository};
use common::{block, real_repo};

#[test]
fn injected_write_fault_surfaces_and_repo_stays_usable() {
    // Fail the very first write (block data temp file) once.
    let h = common::fault_repo(vec![Fault {
        path_contains: "/blocks/",
        op: FaultOp::Write,
        once: Some(1),
    }]);
    let data = block(1, 128);
    let first = h.repo.add_block(&data, &[]);
    assert!(first.is_err(), "injected write fault must propagate");
    // No half-published block exists.
    let v = h.repo.verify().unwrap();
    assert_eq!(v.blocks_total, 0);
    // Retry succeeds.
    let info = h.repo.add_block(&data, &[]).unwrap();
    assert_eq!(h.repo.get_block_verified(&info.hash).unwrap(), data);
}

#[test]
fn injected_create_fault_is_recoverable() {
    let h = common::fault_repo(vec![Fault {
        path_contains: "/blocks/",
        op: FaultOp::Create,
        once: Some(1),
    }]);
    let data = block(2, 64);
    assert!(h.repo.add_block(&data, &[]).is_err());
    let info = h.repo.add_block(&data, &[]).unwrap();
    assert_eq!(h.repo.get_block(&info.hash).unwrap(), data);
}

#[test]
fn corrupted_block_on_disk_is_detected() {
    // Tamper with bytes after publication: verified reads and /verify catch it.
    let h = real_repo("corrupt");
    let info = h.repo.add_block(b"original content here", &[]).unwrap();
    let data_path = h
        .temp
        .as_ref()
        .unwrap()
        .path
        .join("store/blocks")
        .join(&info.hash[0..2])
        .join(&info.hash);

    // Direct overwrite of immutable bytes (simulates disk corruption /
    // malicious local modification).
    let mut bytes = std::fs::read(&data_path).unwrap();
    bytes[0] ^= 0xff;
    std::fs::write(&data_path, bytes).unwrap();

    // Plain read returns corrupted bytes (store can't know unless asked)...
    let raw = h.repo.get_block(&info.hash).unwrap();
    assert_ne!(raw, b"original content here");
    // ...but verified reads and the integrity scan MUST detect it.
    assert!(matches!(
        h.repo.get_block_verified(&info.hash).unwrap_err(),
        cas_store::StoreError::Corrupt { .. }
    ));
    let v = h.repo.verify().unwrap();
    assert!(v.corrupt.contains(&info.hash));
}

#[test]
fn reopening_corrupt_format_marker_is_rejected() {
    let dir = common::TempDir::new("bad-format");
    let store = dir.path.join("store");
    std::fs::create_dir_all(store.join("blocks")).unwrap();
    std::fs::write(store.join("format"), b"something else v9\n").unwrap();
    let err = match Repository::open(store, Arc::new(RealFs::new())) {
        Ok(_) => panic!("expected NotARepository error"),
        Err(e) => e,
    };
    assert!(matches!(err, cas_store::StoreError::NotARepository(_)));
}

#[test]
fn dedup_of_corrupted_existing_block_is_reported_corrupt() {
    let h = real_repo("dedup-corrupt");
    let data = block(5, 256);
    let info = h.repo.add_block(&data, &[]).unwrap();
    let p = h
        .temp
        .as_ref()
        .unwrap()
        .path
        .join("store/blocks")
        .join(&info.hash[0..2])
        .join(&info.hash);
    let mut b = std::fs::read(&p).unwrap();
    b[0] ^= 0x01;
    std::fs::write(&p, b).unwrap();
    // Re-uploading identical (correct) content finds corrupt existing bytes.
    let err = h.repo.add_block(&data, &[]).unwrap_err();
    assert!(matches!(err, cas_store::StoreError::Corrupt { .. }));
}

#[test]
fn memfs_atomic_publish_has_no_torn_observability() {
    // Repeated root republishing under MemFs: readers must only ever see a
    // parseable manifest pointing at an existing block.
    use std::sync::atomic::{AtomicBool, Ordering};
    let h = common::mem_repo();
    let mut hashes = Vec::new();
    for i in 0..10 {
        hashes.push(h.repo.add_block(format!("v{i}").as_bytes(), &[]).unwrap().hash);
    }
    h.repo.put_root("main", &hashes[0]).unwrap();

    let stop = Arc::new(AtomicBool::new(false));
    let repo = Arc::new(h.repo.clone());
    let reader = {
        let repo = Arc::clone(&repo);
        let stop = Arc::clone(&stop);
        let hashes = hashes.clone();
        std::thread::spawn(move || {
            let mut checks = 0;
            while !stop.load(Ordering::Relaxed) {
                if let Ok(m) = repo.get_root("main") {
                    assert!(hashes.contains(&m.hash), "observed invalid root state");
                    assert!(repo.block_exists(&m.hash));
                }
                checks += 1;
            }
            checks
        })
    };
    for (i, hsh) in hashes.iter().enumerate() {
        h.repo.put_root("main", hsh).unwrap();
        let _ = i;
    }
    stop.store(true, Ordering::Relaxed);
    assert!(reader.join().unwrap() > 0);
}
