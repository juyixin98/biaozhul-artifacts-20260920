//! Shared helpers for integration tests.
#![allow(dead_code)]

use ifix::control;
use ifix::format::Limits;
use serde_json::{json, Value};

/// Build an index entirely in memory from a tree JSON value.
pub fn build_from_value(root: Value) -> Vec<u8> {
    let request = json!({ "root": root, "log2_page": 5 });
    let text = serde_json::to_string(&request).unwrap();
    control::build_to_memory(&text, &Limits::default()).expect("build should succeed")
}

/// Build with a chosen fanout exponent.
pub fn build_from_value_fanout(root: Value, log2_page: u8) -> Vec<u8> {
    let request = json!({ "root": root, "log2_page": log2_page });
    let text = serde_json::to_string(&request).unwrap();
    control::build_to_memory(&text, &Limits::default()).expect("build should succeed")
}

/// A moderately deep tree shared by several tests:
/// /
/// ├── alpha/
/// │   ├── one.txt   (blob)
/// │   └── two.txt   (blob)
/// ├── empty.txt     (empty file)
/// └── zeta/
///     └── deep/
///         └── three.txt
pub fn sample_tree() -> Value {
    json!({
        "name": "",
        "type": "dir",
        "children": [
            {
                "name": "alpha", "type": "dir",
                "children": [
                    {"name": "one.txt", "type": "file", "content_b64": "aGVsbG8="},
                    {"name": "two.txt", "type": "file", "content_b64": "d29ybGQ="}
                ]
            },
            {"name": "empty.txt", "type": "file"},
            {
                "name": "zeta", "type": "dir",
                "children": [
                    {
                        "name": "deep", "type": "dir",
                        "children": [
                            {"name": "three.txt", "type": "file",
                             "content_b64": "ZGVlcCBibG9i"}
                        ]
                    }
                ]
            }
        ]
    })
}

/// Flip one byte at `offset` in `bytes`.
pub fn flip_byte(bytes: &mut [u8], offset: usize) {
    bytes[offset] ^= 0xFF;
}

/// Overwrite bytes at `offset` (little-endian writes for integer fields).
pub fn write_u32(bytes: &mut [u8], offset: usize, value: u32) {
    bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}
pub fn write_u64(bytes: &mut [u8], offset: usize, value: u64) {
    bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}
pub fn write_u16(bytes: &mut [u8], offset: usize, value: u16) {
    bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
}

pub fn open_mem(bytes: Vec<u8>) -> ifix::reader::IndexFile<ifix::storage::MemStorage> {
    control::open_memory(bytes, Limits::default()).expect("file should validate")
}
