use ext_hash_index::{Config, HashKind, Index, IndexError};
use std::path::PathBuf;

fn tmp_dir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "ext-hash-smoke-{}-{}-{}",
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

#[test]
fn smoke_split_merge_shrink_lowbits_capacity4() {
    let dir = tmp_dir("smoke");
    let path = dir.join("i.db");
    let cfg = Config {
        bucket_capacity: 4,
        max_depth: 20,
        hash: HashKind::U64LowBits,
        key_max: 128,
        val_max: 256,
    };
    let mut idx = Index::create(&path, &cfg).unwrap();

    for k in 0u64..4 {
        assert!(idx.put(k.to_string().as_bytes(), b"v").unwrap());
    }
    // 5th insert splits + doubles the directory.
    idx.put(b"4", b"v").unwrap();
    let st = idx.stats().unwrap();
    assert_eq!(st.global_depth, 1);
    assert_eq!(st.bucket_count, 2);
    // key 4 (bit0==1) is alone, 0..3 remain in the even bucket.
    for k in 0u64..5 {
        assert_eq!(
            idx.get(k.to_string().as_bytes()).unwrap(),
            Some(b"v".to_vec())
        );
    }
    drop(idx);

    // Reopen: data persists, no recovery needed.
    let mut idx = Index::open(&path).unwrap();
    assert!(idx.recovered_intent().is_none());
    for k in 0u64..5 {
        assert_eq!(
            idx.get(k.to_string().as_bytes()).unwrap(),
            Some(b"v".to_vec())
        );
    }

    // Delete everything: cascading merges must shrink back to depth 0.
    for k in 0u64..5 {
        assert!(idx.delete(k.to_string().as_bytes()).unwrap());
    }
    let st = idx.stats().unwrap();
    assert_eq!(st.global_depth, 0);
    assert_eq!(st.bucket_count, 1);
    for b in &st.buckets {
        assert_eq!(b.entries, 0);
    }
}

#[test]
fn smoke_constant_hash_collision_error() {
    let dir = tmp_dir("coll");
    let path = dir.join("i.db");
    let cfg = Config {
        bucket_capacity: 3,
        max_depth: 20,
        hash: HashKind::Constant,
        key_max: 128,
        val_max: 256,
    };
    let mut idx = Index::create(&path, &cfg).unwrap();
    idx.put(b"alpha", b"1").unwrap();
    idx.put(b"beta", b"2").unwrap();
    idx.put(b"gamma", b"3").unwrap();
    // Bucket full; the 4th distinct key hashes to the same value.
    let e = idx.put(b"delta", b"4").unwrap_err();
    match e {
        IndexError::CollisionCapacity {
            bucket_capacity,
            hash,
            ..
        } => {
            assert_eq!(bucket_capacity, 3);
            assert_eq!(hash, 0);
        }
        other => panic!("expected collision error, got: {}", other),
    }
    // First three remain readable; the failed insert changed nothing.
    assert_eq!(idx.get(b"alpha").unwrap(), Some(b"1".to_vec()));
    assert_eq!(idx.get(b"delta").unwrap(), None);
    let st = idx.stats().unwrap();
    assert_eq!(st.global_depth, 0);
    assert_eq!(st.bucket_count, 1);
}

#[test]
fn smoke_overwrite_and_delete_missing() {
    let dir = tmp_dir("ow");
    let path = dir.join("i.db");
    let cfg = Config {
        bucket_capacity: 2,
        max_depth: 20,
        hash: HashKind::Fnv1a64,
        key_max: 128,
        val_max: 256,
    };
    let mut idx = Index::create(&path, &cfg).unwrap();
    assert!(idx.put(b"k", b"one").unwrap());
    assert!(!idx.put(b"k", b"two").unwrap());
    assert_eq!(idx.get(b"k").unwrap(), Some(b"two".to_vec()));
    assert!(!idx.delete(b"missing").unwrap());
    assert!(idx.delete(b"k").unwrap());
    assert_eq!(idx.get(b"k").unwrap(), None);
}
