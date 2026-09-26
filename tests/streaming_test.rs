//! Tests for streamed `content_file` blobs and for allocation-safety against
//! files that declare huge counts/sizes they do not actually contain.

mod common;

use common::*;
use ifix::format::Limits;
use ifix::reader::IndexFile;
use ifix::storage::MemStorage;
use serde_json::json;
use std::io::Cursor;

#[test]
fn content_file_blobs_are_streamed_and_read_back() {
    // A payload larger than the writer's 64 KiB copy buffer, to prove
    // streaming rather than a single read.
    let dir = std::env::temp_dir().join(format!(
        "ifix-stream-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();

    let payload: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
    std::fs::write(dir.join("big.bin"), &payload).unwrap();

    let out_path = dir.join("out.ifix");
    let request = json!({
        "command": "build",
        "output": out_path.to_string_lossy(),
        "root": {
            "name": "", "type": "dir",
            "children": [
                {"name": "big.bin", "type": "file", "content_file": "big.bin"}
            ]
        }
    });
    let result = ifix::control::dispatch(&request, &dir).expect("streamed build");
    assert!(result["file_len"].as_u64().unwrap() >= 200_000);

    let idx = IndexFile::open(
        ifix::storage::FileStorage::open(&out_path).unwrap(),
        Limits::default(),
    )
    .expect("validates");
    let mut got = Cursor::new(Vec::new());
    let n = idx.read_blob("/big.bin", &mut got).unwrap();
    assert_eq!(n, 200_000);
    assert_eq!(got.get_ref().as_slice(), payload.as_slice());

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn content_file_rejects_path_traversal() {
    let dir = std::env::temp_dir().join(format!("ifix-trav-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let request = json!({
        "command": "build",
        "output": dir.join("o.ifix").to_string_lossy(),
        "root": {
            "name": "", "type": "dir",
            "children": [
                {"name": "x", "type": "file", "content_file": "../../etc/passwd"}
            ]
        }
    });
    let err = ifix::control::dispatch(&request, &dir).unwrap_err();
    assert_eq!(err.code(), "JSON");

    let request = json!({
        "command": "build",
        "output": dir.join("o.ifix").to_string_lossy(),
        "root": {
            "name": "", "type": "dir",
            "children": [
                {"name": "x", "type": "file", "content_file": "/etc/passwd"}
            ]
        }
    });
    let err = ifix::control::dispatch(&request, &dir).unwrap_err();
    assert_eq!(err.code(), "JSON");

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn tiny_file_declaring_huge_counts_is_rejected_without_allocating() {
    let good = build_from_value(sample_tree());

    // node_count at u32::MAX: node_block = count*48 cannot tile the file.
    let mut b = good.clone();
    write_u32(&mut b, 12, u32::MAX);
    let err = IndexFile::open(MemStorage::new(b), Limits::default()).unwrap_err();
    assert!(
        matches!(err.code(), "REGION_TILING" | "LIMIT_NODES"),
        "got {}",
        err.code()
    );

    // data_bytes at u64::MAX/2: 48 + data cannot tile.
    let mut b = good.clone();
    write_u64(&mut b, 16, u64::MAX / 2);
    let err = IndexFile::open(MemStorage::new(b), Limits::default()).unwrap_err();
    assert_eq!(err.code(), "REGION_TILING");

    // A 48-byte file consisting of just a plausible-looking header claiming
    // thousands of nodes must be rejected before any per-node allocation.
    let mut hdr = vec![0u8; 48];
    hdr[0..6].copy_from_slice(b"IFIX1\0");
    hdr[7] = 8;
    hdr[8..10].copy_from_slice(&1u16.to_le_bytes());
    hdr[12..16].copy_from_slice(&1_000_000u32.to_le_bytes());
    // nodes_offset = 48, toc_offset = 48 (zero data)
    hdr[24..32].copy_from_slice(&48u64.to_le_bytes());
    hdr[32..40].copy_from_slice(&48u64.to_le_bytes());
    let err = IndexFile::open(MemStorage::new(hdr), Limits::default()).unwrap_err();
    assert_eq!(err.code(), "REGION_TILING");
}

#[test]
fn declared_node_count_over_configured_limit_is_rejected() {
    // A real 51-node build opened under a tight max_nodes cap.
    let mut children = Vec::new();
    for i in 0..50u32 {
        children.push(json!({"name": format!("f{i}"), "type": "file"}));
    }
    let root = json!({"name": "", "type": "dir", "children": children});
    let bytes = build_from_value(root);
    let tight = Limits {
        max_nodes: 10,
        ..Limits::default()
    };
    let err = IndexFile::open(MemStorage::new(bytes), tight).unwrap_err();
    assert_eq!(err.code(), "LIMIT_NODES");
}
