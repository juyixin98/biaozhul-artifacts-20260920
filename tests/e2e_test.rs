//! End-to-end functional tests: build -> validate -> lookup/list/read,
//! including mmap parity against the ordinary read backend.

mod common;

use common::*;
use ifix::control;
use ifix::format::Limits;
use std::io::Cursor;

#[test]
fn builds_and_lists_root() {
    let bytes = build_from_value(sample_tree());
    let idx = open_mem(bytes);
    let root = idx.lookup("/").expect("root lookup");
    assert_eq!(root.id, 0);
    assert!(root.is_dir);
    assert_eq!(root.child_count, 3);

    let entries = idx.list("/").expect("list root");
    let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();
    assert_eq!(names, ["alpha", "empty.txt", "zeta"]);
}

#[test]
fn lookup_nested_path_uses_index() {
    let bytes = build_from_value(sample_tree());
    let idx = open_mem(bytes);

    let f = idx.lookup("/zeta/deep/three.txt").expect("nested lookup");
    assert!(!f.is_dir);
    assert_eq!(f.size, 9); // "deep blob"

    assert!(matches!(
        idx.lookup("/alpha/missing.txt"),
        Err(ifix::IfixError::NotFound { .. })
    ));
    assert!(matches!(
        idx.lookup("/nope"),
        Err(ifix::IfixError::NotFound { .. })
    ));
}

#[test]
fn reads_blob_bytes_back() {
    let bytes = build_from_value(sample_tree());
    let idx = open_mem(bytes);

    let mut out = Cursor::new(Vec::new());
    let n = idx
        .read_blob("/alpha/one.txt", &mut out)
        .expect("read blob");
    assert_eq!(n, 5);
    assert_eq!(out.get_ref(), b"hello");

    let mut out = Cursor::new(Vec::new());
    idx.read_blob("/zeta/deep/three.txt", &mut out).unwrap();
    assert_eq!(out.get_ref(), b"deep blob");
}

#[test]
fn empty_file_has_zero_size_and_no_blob() {
    let bytes = build_from_value(sample_tree());
    let idx = open_mem(bytes);
    let f = idx.lookup("/empty.txt").unwrap();
    assert_eq!(f.size, 0);
    let mut out = Cursor::new(Vec::new());
    let n = idx.read_blob("/empty.txt", &mut out).unwrap();
    assert_eq!(n, 0);
    assert!(out.get_ref().is_empty());
}

#[test]
fn lookup_is_bounded_by_max_depth_limit() {
    let bytes = build_from_value(sample_tree());
    // Tree depth: / -> zeta -> deep -> three.txt (3 edges). A cap of 2 is
    // already enforced while validating the graph.
    let tight = Limits {
        max_depth: 2,
        ..Limits::default()
    };
    let err = control::open_memory(bytes, tight).unwrap_err();
    assert_eq!(err.code(), "LIMIT_DEPTH");

    // A permissive cap opens fine and serves shallow paths.
    let bytes = build_from_value(sample_tree());
    let idx = control::open_memory(bytes, Limits::default()).unwrap();
    assert!(idx.lookup("/alpha/one.txt").is_ok());
    assert!(idx.lookup("/zeta/deep/three.txt").is_ok());
}

#[test]
fn oversized_blob_refused_by_output_limit() {
    let root = serde_json::json!({
        "name": "", "type": "dir",
        "children": [
            {"name": "big", "type": "file", "content_b64": "AAAA"} // 3 bytes
        ]
    });
    let bytes = build_from_value(root);
    let tight = Limits {
        max_output_bytes: 2,
        ..Limits::default()
    };
    let err = control::open_memory(bytes, tight).unwrap_err();
    // A file that cannot be served within the output cap fails validation.
    assert_eq!(err.code(), "LIMIT_OUTPUT");
}

#[test]
fn read_backend_and_mmap_backend_agree() {
    // Build to a real file, open twice (read + mmap) and compare responses.
    let dir = std::env::temp_dir().join(format!("ifix-e2e-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("sample.ifix");

    let request = serde_json::json!({
        "command": "build",
        "output": path.to_string_lossy(),
        "root": sample_tree(),
        "log2_page": 8
    });
    let base = std::path::PathBuf::from(".");
    let built = ifix::control::dispatch(&request, &base).expect("build");
    assert!(built["file_len"].as_u64().unwrap() > 0); // raw data, not envelope

    let file_bytes = std::fs::read(&path).unwrap();

    let read_idx = ifix::reader::IndexFile::open(
        ifix::storage::FileStorage::open(&path).unwrap(),
        Limits::default(),
    )
    .expect("read backend validates");
    let mmap_idx = ifix::reader::IndexFile::open(
        ifix::storage::MmapStorage::open(&path).unwrap(),
        Limits::default(),
    )
    .expect("mmap backend validates");

    assert_eq!(read_idx.toc_height(), mmap_idx.toc_height());
    let paths = [
        "/",
        "/alpha",
        "/alpha/one.txt",
        "/zeta/deep/three.txt",
        "/empty.txt",
    ];
    for p in paths {
        let a = read_idx.lookup(p).unwrap();
        let b = mmap_idx.lookup(p).unwrap();
        assert_eq!(a.id, b.id, "id mismatch at {p}");
        assert_eq!(a.size, b.size, "size mismatch at {p}");
        assert_eq!(a.name, b.name, "name mismatch at {p}");
        let mut ba = Cursor::new(Vec::new());
        let mut bb = Cursor::new(Vec::new());
        if !a.is_dir {
            read_idx.read_blob(p, &mut ba).unwrap();
            mmap_idx.read_blob(p, &mut bb).unwrap();
            assert_eq!(ba.get_ref(), bb.get_ref(), "blob mismatch at {p}");
        }
    }

    // Byte-for-byte identical to an in-memory build of the same tree and
    // fanout.
    let mem_bytes = build_from_value_fanout(sample_tree(), 8);
    assert_eq!(file_bytes, mem_bytes, "file build and memory build differ");

    std::fs::remove_dir_all(&dir).ok();
}

#[test]
fn every_node_is_reachable_through_the_toc() {
    // Force a multi-level TOC (fanout 32) with >32 nodes, then resolve every
    // node id through the multi-layer index and confirm record addresses.
    let mut children = Vec::new();
    for i in 0..200u32 {
        children.push(serde_json::json!({
            "name": format!("f{i:04}.txt"), "type": "file"
        }));
    }
    let root = serde_json::json!({"name": "", "type": "dir", "children": children});
    let bytes = build_from_value_fanout(root, 5);
    let idx = open_mem(bytes);
    assert!(idx.toc_height() >= 2, "expected a multi-level TOC");
    for id in idx.iter_ids() {
        // Resolve through the actual multi-layer navigation and cross-check
        // against the fixed-record arithmetic.
        let offset = idx.resolve_record_offset(id).expect("TOC resolve");
        assert_eq!(offset, idx.nodes_offset() + (id as u64) * 48);
    }
}
