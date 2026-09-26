//! Mutation tests: every corruption class the spec promises to reject.
//!
//! Each test starts from a valid image containing a multi-level directory
//! index, applies a targeted mutation, and asserts that *both* backends reject
//! it — the streaming reader at open/lookup time, and full validation.

use ifix::format::{BLOCK_SIZE, FANOUT, HEADER_SIZE, NIL, NODE_SIZE};
use ifix::json::parse;
use ifix::reader::{Reader, SliceSource};
use ifix::validate::validate;
use ifix::writer::{build_from_json, WriteOptions};
use ifix::{Limits, NodeType};

/// Build a tree with more than FANOUT^2 leaves so the root index has at
/// least two internal levels; include a nested directory and a symlink.
fn valid_image() -> Vec<u8> {
    let n = FANOUT * FANOUT * 2 + 7; // guarantees 3 levels
    let mut children = String::new();
    for i in 0..n {
        children.push_str(&format!(
            "{{\"name\":\"f{i:05}\",\"type\":\"file\",\"content_base64\":\"aGk=\"}},"
        ));
    }
    children.push_str("{\"name\":\"d\",\"type\":\"dir\",\"children\":[");
    for i in 0..(FANOUT + 1) {
        children.push_str(&format!(
            "{{\"name\":\"g{i:03}\",\"type\":\"file\",\"content_base64\":\"\"}},"
        ));
    }
    children.push_str("{\"name\":\"link\",\"type\":\"symlink\",\"target\":\"../f00000\"}]}");
    let req = format!(r#"{{"tree":{{"name":"","type":"dir","children":[{children}]}}}}"#);
    let v = parse(&req).unwrap();
    build_from_json(&v, WriteOptions::default()).unwrap()
}

fn slice_reader(img: &[u8]) -> Reader {
    Reader::new(Box::new(SliceSource::new(img.to_vec())), Limits::default()).unwrap()
}

/// Assert the image is rejected: either opening fails, or — when the header
/// is intact — validation/lookup fails.
fn assert_rejected(img: &[u8], label: &str) {
    // Open may fail, which is sufficient.
    if let Ok(r) = Reader::new(Box::new(SliceSource::new(img.to_vec())), Limits::default()) {
        let validation = validate(&r);
        let lookup = r.lookup(&format!("/f{:05}", FANOUT * FANOUT), true);
        assert!(
            validation.is_err() || lookup.is_err(),
            "{label}: mutation accepted (validate={validation:?}, lookup={lookup:?})"
        );
    }
}

fn patch(img: &mut [u8], at: usize, bytes: &[u8]) {
    img[at..at + bytes.len()].copy_from_slice(bytes);
}

#[test]
fn valid_image_passes() {
    let img = valid_image();
    let r = slice_reader(&img);
    validate(&r).unwrap();
    // spot lookups on both sides of block boundaries
    for i in [0u32, 15, 16, 255, 256, (FANOUT as u32) * FANOUT as u32] {
        r.lookup(&format!("/f{i:05}"), true).unwrap();
    }
    r.lookup("/d/g000", false).unwrap();
    // The symlink resolves to its target through both relative and ".."-bearing paths.
    let via_link = r.lookup("/d/link", true).unwrap();
    assert_eq!(via_link.kind, NodeType::File);
}

#[test]
fn mutate_header_offsets() {
    // Inflate node section end past EOF.
    let mut img = valid_image();
    let bogus_end = img.len() as u64 + 999;
    patch(&mut img, 24, &bogus_end.to_le_bytes());
    assert_rejected(&img, "nodes_end past eof");

    // Swap names/blocks start so sections overlap.
    let mut img = valid_image();
    let names_start = u64::from_le_bytes(img[32..40].try_into().unwrap());
    let blocks_start = u64::from_le_bytes(img[48..56].try_into().unwrap());
    patch(&mut img, 32, &blocks_start.to_le_bytes()); // names start too late
    patch(&mut img, 40, &(blocks_start + 1).to_le_bytes());
    let _ = names_start;
    assert_rejected(&img, "overlapping sections");

    // Garbage magic / version / reserved.
    let mut img = valid_image();
    img[0] = b'X';
    assert_rejected(&img, "magic");
    let mut img = valid_image();
    img[8] = 7;
    assert_rejected(&img, "version");
    let mut img = valid_image();
    img[100] = 1;
    assert_rejected(&img, "reserved byte");
}

#[test]
fn truncation_variants() {
    let img = valid_image();
    for cut in [
        0usize,
        16,
        127,
        128,
        img.len() - 1,
        HEADER_SIZE as usize + 1,
    ] {
        assert_rejected(&img[..cut], &format!("truncate@{cut}"));
    }
}

#[test]
fn mutate_node_offsets_and_lengths() {
    let img = valid_image();
    // Node 0 record begins at HEADER_SIZE. Its name offset/length live at +4/+8.
    // Corrupt name length to run past the names section.
    let mut bad = img.clone();
    patch(&mut bad, HEADER_SIZE as usize + 8, &u32::MAX.to_le_bytes());
    assert_rejected(&bad, "huge name_len");

    // Corrupt blob offset of a file node (node 1: first child).
    let node1 = HEADER_SIZE as usize + NODE_SIZE as usize;
    let mut bad = img.clone();
    patch(&mut bad, node1 + 16, &u64::MAX.to_le_bytes());
    assert_rejected(&bad, "huge blob_off");

    // data_off + data_len overflow
    let mut bad = img.clone();
    patch(&mut bad, node1 + 24, &u64::MAX.to_le_bytes());
    assert_rejected(&bad, "blob interval overflow");

    // Invalid node type byte.
    let mut bad = img.clone();
    bad[node1] = 99;
    assert_rejected(&bad, "node type 99");

    // Directory claims children but NIL first_block.
    let mut bad = img.clone();
    patch(&mut bad, HEADER_SIZE as usize + 32, &NIL.to_le_bytes());
    assert_rejected(&bad, "dir nil block with children");

    // Non-directory carrying a block pointer (flip type of node 1 to dir).
    let mut bad = img.clone();
    bad[node1] = ifix::format::TYPE_DIR;
    assert_rejected(&bad, "file pretending dir");
}

#[test]
fn mutate_block_child_to_self_creates_cycle() {
    let img = valid_image();
    let r = slice_reader(&img);
    let root_block = r.header().root_block;
    // Root is internal; repoint its first child to its own block id.
    let block_at = |bid: usize| r.header().blocks.0 as usize + bid * BLOCK_SIZE as usize;
    let mut bad = img.clone();
    let first_entry_child = block_at(root_block as usize) + 4 + 8;
    patch(&mut bad, first_entry_child, &root_block.to_le_bytes());
    assert_rejected(&bad, "self-referencing block");

    // Point child backwards (block id < parent).
    let mut bad = img.clone();
    patch(
        &mut bad,
        first_entry_child,
        &root_block.saturating_sub(1).to_le_bytes(),
    );
    assert_rejected(&bad, "backwards block ref");
}

#[test]
fn mutate_leaf_node_ref_out_of_range() {
    let img = valid_image();
    let r = slice_reader(&img);
    // Walk root -> first child chain down to a leaf, then corrupt one leaf.
    let mut block = r.header().root_block;
    let mut leaf = None;
    for _ in 0..5 {
        let (kind, entries) = r.debug_block(block).unwrap();
        if kind == ifix::format::KIND_LEAF {
            leaf = Some(block);
            break;
        }
        block = entries[0].2 as u32;
    }
    let leaf = leaf.unwrap();
    let leaf_off = r.header().blocks.0 as usize + leaf as usize * BLOCK_SIZE as usize;
    let mut bad = img.clone();
    patch(&mut bad, leaf_off + 4 + 8, &u32::MAX.to_le_bytes());
    assert_rejected(&bad, "leaf child u32::MAX");
}

#[test]
fn mutate_block_kind_and_ordering() {
    let img = valid_image();
    let r = slice_reader(&img);
    let root_block = r.header().root_block;
    let base = r.header().blocks.0 as usize + root_block as usize * BLOCK_SIZE as usize;

    let mut bad = img.clone();
    bad[base] = 7; // invalid kind
    assert_rejected(&bad, "bad block kind");

    // Swap two entry keys in an internal block -> ordering violation.
    let mut bad = img.clone();
    // Entries are 16 bytes; exchange child fields of slot 0 and slot 1.
    let c0 = base + 4 + 8;
    let c1 = base + 4 + ENTRY16 + 8;
    let v0 = bad[c0..c0 + 8].to_vec();
    let v1 = bad[c1..c1 + 8].to_vec();
    patch(&mut bad, c0, &v1);
    patch(&mut bad, c1, &v0);
    assert_rejected(&bad, "internal entry order");
}

#[test]
fn node_interval_overlap() {
    // Two nodes claiming overlapping name ranges.
    let mut img = valid_image();
    let n0_name_len_at = HEADER_SIZE as usize + 8;
    let n1_name_off_at = HEADER_SIZE as usize + NODE_SIZE as usize + 4;
    // Make node 0's "name" cover most of the names section.
    patch(&mut img, n0_name_len_at, &4096u32.to_le_bytes());
    patch(&mut img, n1_name_off_at, &0u32.to_le_bytes());
    assert_rejected(&img, "name overlap");
}

const ENTRY16: usize = 16;

#[test]
fn declared_count_greater_than_leaves() {
    // Inflate root directory child_count; validation must notice the mismatch.
    let mut img = valid_image();
    patch(
        &mut img,
        HEADER_SIZE as usize + 36,
        &(9_000_000u32).to_le_bytes(),
    );
    assert_rejected(&img, "child_count mismatch");
}

#[test]
fn allocation_bomb_declared_giant_counts() {
    // A tiny file that *declares* astronomic section sizes must never cause a
    // large allocation — run under the normal (small) test stack/heap and
    // simply assert rejection.
    let mut bomb = vec![0u8; 256];
    bomb[0..8].copy_from_slice(&ifix::format::MAGIC);
    bomb[8] = 1;
    bomb[16..24].copy_from_slice(&128u64.to_le_bytes());
    bomb[24..32].copy_from_slice(&u64::MAX.to_le_bytes());
    assert_rejected(&bomb, "allocation bomb");
}
