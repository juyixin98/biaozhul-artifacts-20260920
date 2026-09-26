//! Malformed-input tests: mutated offsets/lengths, cycles, overlapping node
//! and data intervals, truncation and bad magic. Every case must be rejected
//! with a structured error code, never a panic or oversized allocation.

#![allow(dead_code)]

mod common;

use common::*;
use ifix::crc::Crc32;
use ifix::format::Limits;
use ifix::reader::IndexFile;
use ifix::storage::MemStorage;

// BFS node ids in sample_tree():
//   0 root, 1 alpha(dir), 2 empty.txt, 3 zeta(dir),
//   4 one.txt, 5 two.txt, 6 deep(dir), 7 three.txt
const ROOT: u32 = 0;
const ALPHA: u32 = 1;
const EMPTY: u32 = 2;
const ZETA: u32 = 3;
const ONE: u32 = 4;
const TWO: u32 = 5;
const DEEP: u32 = 6;
const THREE: u32 = 7;

fn nodes_offset(bytes: &[u8]) -> u64 {
    u64::from_le_bytes(bytes[24..32].try_into().unwrap())
}
fn toc_offset(bytes: &[u8]) -> u64 {
    u64::from_le_bytes(bytes[32..40].try_into().unwrap())
}
fn record_at(bytes: &[u8], id: u32) -> usize {
    nodes_offset(bytes) as usize + id as usize * 48
}

/// Rewrite a record's first 36 bytes and recompute its self_check, modeling
/// a crafted file whose author can satisfy per-record CRCs at will.
fn mutate_record_fixed<F: Fn(&mut [u8])>(bytes: &mut [u8], id: u32, f: F) {
    let base = record_at(bytes, id);
    f(&mut bytes[base..base + 48]);
    let check = Crc32::checksum(&bytes[base..base + 36]);
    bytes[base + 36..base + 40].copy_from_slice(&check.to_le_bytes());
}

fn assert_rejected(bytes: Vec<u8>, expected_code: &str) {
    let err = IndexFile::open(MemStorage::new(bytes), Limits::default())
        .err()
        .unwrap_or_else(|| panic!("corrupt file was ACCEPTED (expected {expected_code})"));
    assert_eq!(err.code(), expected_code, "unexpected error: {err}");
}

#[test]
fn accepts_unmodified_sample() {
    let bytes = build_from_value(sample_tree());
    IndexFile::open(MemStorage::new(bytes), Limits::default()).expect("valid file must open");
}

// --- truncation -------------------------------------------------------------

#[test]
fn rejects_empty_and_tiny_files() {
    assert_rejected(vec![], "TRUNCATED");
    assert_rejected(vec![0u8; 10], "TRUNCATED");
    assert_rejected(vec![0u8; 47], "TRUNCATED");
}

#[test]
fn rejects_truncation_at_every_region_boundary() {
    let good = build_from_value(sample_tree());
    let cut_points = [
        47,
        48,
        nodes_offset(&good) as usize - 1,
        nodes_offset(&good) as usize,
        toc_offset(&good) as usize - 1,
        toc_offset(&good) as usize,
        good.len() - 1,
        good.len() - 9,
    ];
    for cut in cut_points {
        let mut bytes = good.clone();
        bytes.truncate(cut);
        let result = IndexFile::open(MemStorage::new(bytes), Limits::default());
        assert!(result.is_err(), "truncation at {cut} should be rejected");
    }
}

// --- header mutations -------------------------------------------------------

#[test]
fn rejects_bad_magic_version_and_flags() {
    let good = build_from_value(sample_tree());

    let mut b = good.clone();
    b[0] = b'X';
    assert_rejected(b, "BAD_MAGIC");

    let mut b = good.clone();
    write_u16(&mut b, 8, 999);
    assert_rejected(b, "BAD_VERSION");

    let mut b = good.clone();
    b[6] = 0x80; // reserved flag bit
    assert_rejected(b, "BAD_FLAGS");

    let mut b = good.clone();
    write_u16(&mut b, 10, 1); // reserved0
    assert_rejected(b, "BAD_HEADER");

    let mut b = good.clone();
    b[7] = 4; // fanout exponent too small
    assert_rejected(b, "BAD_FANOUT");
}

#[test]
fn rejects_mutated_region_offsets() {
    let good = build_from_value(sample_tree());

    // data_bytes larger than reality -> tiling breaks.
    let mut b = good.clone();
    write_u64(&mut b, 16, 9_999_999);
    assert_rejected(b, "REGION_TILING");

    // nodes_offset no longer equals 48 + data_bytes.
    let mut b = good.clone();
    write_u64(&mut b, 24, nodes_offset(&good) + 8);
    assert_rejected(b, "REGION_TILING");

    // toc_offset shifted -> node block no longer tiles.
    let mut b = good.clone();
    write_u64(&mut b, 32, toc_offset(&good) + 48);
    assert_rejected(b, "REGION_TILING");

    // toc_offset past EOF.
    let mut b = good.clone();
    write_u64(&mut b, 32, good.len() as u64 + 100);
    assert_rejected(b, "REGION_TILING");
}

#[test]
fn rejects_declared_node_count_larger_than_toc() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // Inflating node_count breaks node-block tiling first.
    write_u32(&mut b, 12, 1_000_000);
    assert_rejected(b, "REGION_TILING");
}

// --- record field mutations (self_check repaired) --------------------------

#[test]
fn rejects_blob_length_escaping_data_region() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ONE, |r| write_u64(r, 8, u64::MAX / 2));
    assert_rejected(b, "DATA_RANGE");
}

#[test]
fn rejects_blob_offset_into_header() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ONE, |r| write_u64(r, 0, 8));
    assert_rejected(b, "DATA_RANGE");
}

#[test]
fn rejects_child_interval_out_of_range() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ALPHA, |r| write_u32(r, 24, 4_000_000_000));
    assert_rejected(b, "CHILD_RANGE");
}

#[test]
fn rejects_sentinel_first_child_with_nonzero_count() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ALPHA, |r| write_u32(r, 24, 0xFFFF_FFFF));
    assert_rejected(b, "CHILD_RANGE");
}

#[test]
fn rejects_self_referencing_child_cycle() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // alpha (id 1) claims itself as a child.
    mutate_record_fixed(&mut b, ALPHA, |r| {
        write_u32(r, 24, ALPHA);
        write_u16(r, 28, 1);
    });
    let result = IndexFile::open(MemStorage::new(b), Limits::default());
    let code = result.expect_err("cycle must be rejected").code();
    assert!(
        matches!(code, "NODE_CYCLE" | "CHILD_OVERLAP"),
        "expected a cycle/overlap error, got {code}"
    );
}

#[test]
fn rejects_overlapping_child_intervals() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // Root already claims ids [1,4) (alpha=1, empty=2, zeta=3). Make alpha
    // (id 1) ALSO claim id 2 (empty.txt) — two distinct parents, one child.
    mutate_record_fixed(&mut b, ALPHA, |r| {
        write_u32(r, 24, EMPTY);
        write_u16(r, 28, 1);
    });
    assert_rejected(b, "CHILD_OVERLAP");
}

#[test]
fn rejects_orphan_node() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // Shrink root's children from 3 to 2: zeta (id 3) and its subtree become
    // unreachable.
    mutate_record_fixed(&mut b, ROOT, |r| write_u16(r, 28, 2));
    assert_rejected(b, "ORPHAN_NODE");
}

#[test]
fn rejects_file_node_with_children() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // empty.txt (id 2) is a file; fabricate a child pointer.
    mutate_record_fixed(&mut b, EMPTY, |r| {
        write_u32(r, 24, ONE);
        write_u16(r, 28, 1);
    });
    // one.txt now double-claimed OR the file-with-child type rule fires.
    let result = IndexFile::open(MemStorage::new(b), Limits::default());
    let code = result.expect_err("must be rejected").code();
    assert!(matches!(code, "CHILD_OVERLAP" | "NODE_TYPE"), "got {code}");
}

#[test]
fn rejects_directory_carrying_blob_data() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ALPHA, |r| {
        write_u64(r, 0, 48);
        write_u64(r, 8, 3);
        write_u32(r, 16, Crc32::checksum(b"abc"));
    });
    assert_rejected(b, "NODE_TYPE");
}

#[test]
fn rejects_bad_record_type_byte() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ONE, |r| {
        r[30] = 9;
    });
    assert_rejected(b, "NODE_TYPE");
}

#[test]
fn rejects_root_node_that_is_a_file() {
    // A valid tree containing only the empty root directory.
    let solo_root = serde_json::json!({"name": "", "type": "dir"});
    let good = build_from_value(solo_root);
    // Record 0 is the root; flip its type byte to file (1) and repair CRC.
    let mut b = good.clone();
    mutate_record_fixed(&mut b, ROOT, |r| r[30] = 1);
    assert_rejected(b, "NODE_TYPE");
}

#[test]
fn rejects_bad_record_self_check_without_repair() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    let base = record_at(&b, ONE);
    b[base + 8] ^= 0x01; // flip data_len byte, leave CRC stale
    assert_rejected(b, "NODE_CRC");
}

// --- name interval mutations ------------------------------------------------

#[test]
fn rejects_name_range_outside_data_region() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // name_offset lives at record byte 40 and is NOT covered by self_check.
    let base = record_at(&b, ONE);
    write_u64(&mut b, base + 40, u64::MAX - 10);
    assert_rejected(b, "NAME_RANGE");
}

#[test]
fn rejects_non_utf8_name() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // Point alpha's name at the one.txt blob ("hello", valid UTF-8) then
    // corrupt that byte to an invalid UTF-8 lead. Easier: repoint name at a
    // crafted invalid sequence inside the data region by overwriting the
    // first data bytes (name bytes are at the start of the data region).
    // The first bytes are root's children names; corrupt "alpha".
    b[48] = 0xFF;
    b[49] = 0xFF;
    let result = IndexFile::open(MemStorage::new(b), Limits::default());
    let code = result.expect_err("invalid UTF-8 must be rejected").code();
    assert!(
        matches!(code, "NAME_INVALID" | "BLOB_CRC" | "BAD_CRC"),
        "got {code}"
    );
}

// --- data interval overlap --------------------------------------------------

#[test]
fn rejects_overlapping_blob_intervals() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // Find one.txt's data offset (record id 4, field at byte 0).
    let one_off = u64::from_le_bytes(
        b[record_at(&b, ONE)..record_at(&b, ONE) + 8]
            .try_into()
            .unwrap(),
    );
    // Point two.txt's blob at one.txt's blob start.
    mutate_record_fixed(&mut b, TWO, |r| write_u64(r, 0, one_off));
    assert_rejected(b, "DATA_OVERLAP");
}

#[test]
fn rejects_corrupted_blob_bytes() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    let one_base = record_at(&b, ONE);
    let data_off = u64::from_le_bytes(b[one_base..one_base + 8].try_into().unwrap()) as usize;
    b[data_off] ^= 0x01;
    // Blob CRC is checked before the whole-file CRC.
    assert_rejected(b, "BLOB_CRC");
}

// --- TOC mutations ----------------------------------------------------------

#[test]
fn rejects_mutated_toc_leaf_pointer() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // TOC leaf level begins at toc_offset; entry 0 payload is bytes 4..12.
    let toc = toc_offset(&b) as usize;
    write_u64(&mut b, toc + 4, nodes_offset(&good) + 1); // misaligned record
    assert_rejected(b, "TOC_POINTER");
}

#[test]
fn rejects_mutated_toc_level_count() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // level_count u32 is the first field of the 9-byte tail.
    let tail = b.len() - 9;
    write_u32(&mut b, tail, 7);
    assert_rejected(b, "TOC_STRUCTURE");
}

#[test]
fn rejects_missing_tend_marker() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    let last = b.len() - 1;
    b[last] = b'X';
    assert_rejected(b, "TOC_STRUCTURE");
}

#[test]
fn rejects_fanout_disagreement_between_header_and_toc() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    b[7] = 9; // header fanout changed; tail byte still 5/8
    assert_rejected(b, "TOC_STRUCTURE");
}

// --- file CRC ---------------------------------------------------------------

#[test]
fn rejects_whole_file_crc_mismatch() {
    let good = build_from_value(sample_tree());
    let mut b = good.clone();
    // Mutate the stored file CRC itself.
    write_u32(&mut b, 40, 0xDEAD_BEEF);
    assert_rejected(b, "BAD_CRC");
}

// --- multi-level TOC: larger generated tree --------------------------------

#[test]
fn rejects_toc_pointer_in_multilevel_tree() {
    let mut children = Vec::new();
    for i in 0..500u32 {
        children.push(serde_json::json!({"name": format!("n{i:04}"), "type": "file"}));
    }
    let root = serde_json::json!({"name": "", "type": "dir", "children": children});
    let good = build_from_value_fanout(root, 5);
    let mut b = good.clone();
    let toc = toc_offset(&b) as usize;
    // Corrupt a late leaf entry's payload pointer.
    write_u64(&mut b, toc + 400 * 12 + 4, nodes_offset(&good));
    assert_rejected(b, "TOC_POINTER");
}
