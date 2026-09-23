//! Strict format validation: every corruption class must be reported as
//! `Error::Corruption`, never silently accepted.
//!
//! The headline case is a **corrupted restart offset**: a payload mutation alone
//! would be caught by the block CRC first, so for that case the test recomputes
//! the (masked) checksum after tampering, forcing the reader past the CRC layer
//! into structural validation where the restart array is checked.

mod common;

use common::*;
use psst::block::Block;
use psst::coding::{self, crc32c, mask_crc};
use psst::error::Error;
use psst::format::{
    write_block as encode_block, BlockHandle, Footer, BLOCK_TRAILER_LEN, BLOCK_TYPE_DATA,
    FOOTER_LEN,
};
use psst::io::MemStore;
use psst::table::{Options, ScanOptions};

fn sample_bytes() -> Vec<u8> {
    let pairs: Vec<(Vec<u8>, Vec<u8>)> = (0..200u32)
        .map(|i| {
            (
                format!("k/{i:04}").into_bytes(),
                format!("v{i}").into_bytes(),
            )
        })
        .collect();
    let (store, _) = build_mem(
        &pairs,
        Options {
            block_size: 90,
            restart_interval: 4,
        },
    )
    .unwrap();
    store.snapshot()
}

fn first_data_handle(bytes: &[u8]) -> BlockHandle {
    let footer = Footer::decode(&bytes[bytes.len() - FOOTER_LEN..]).unwrap();
    let payload = &bytes
        [footer.index.offset as usize..footer.index.offset as usize + footer.index.size as usize];
    let idx = Block::parse(payload.to_vec()).unwrap();
    let cur = idx.first().unwrap();
    BlockHandle::decode(cur.value().unwrap()).unwrap().0
}

/// Recompute the trailer checksum for a block after payload tampering.
fn repair_checksum(bytes: &mut [u8], handle: BlockHandle) {
    let start = handle.offset as usize;
    let size = handle.size as usize;
    let mut crc_input = Vec::with_capacity(1 + size);
    crc_input.push(BLOCK_TYPE_DATA);
    crc_input.extend_from_slice(&bytes[start..start + size]);
    let crc = mask_crc(crc32c(&crc_input)).to_le_bytes();
    let trailer = start + size;
    bytes[trailer] = BLOCK_TYPE_DATA;
    bytes[trailer + 1..trailer + 5].copy_from_slice(&crc);
}

fn assert_corruption(store: &MemStore, label: &str, when: When) {
    let size = store.len() as u64;
    // Opening itself validates the footer and reads the index block.
    let open = psst::table::Table::open(psst::io::MemReader::new(store.clone()), size);
    let result = match (open, when) {
        (Ok(_t), When::Open) => panic!("{label}: expected corruption at open, opened fine"),
        (Ok(t), When::Get) => t
            .get(b"k/00050")
            .map(|_| ())
            .and_then(|_| t.validate().map(|_| ())),
        (Ok(t), When::Validate) => t.validate().map(|_| ()),
        (Ok(t), When::Scan) => t.scan(ScanOptions::default()).and_then(|mut it| {
            while it.next_entry()?.is_some() {}
            Ok(())
        }),
        (Err(e), When::Open) => Err(e),
        (Err(e), _) => Err(e),
    };
    match result {
        Err(Error::Corruption(msg)) => {
            eprintln!("{label}: correctly rejected -> {msg}");
        }
        Err(other) => panic!("{label}: expected corruption, got {other}"),
        Ok(()) => panic!("{label}: corruption went undetected"),
    }
}

#[derive(Clone, Copy)]
#[allow(dead_code)]
enum When {
    Open,
    Get,
    Validate,
    Scan,
}

#[test]
fn bad_footer_magic_rejected() {
    let mut bytes = sample_bytes();
    *bytes.last_mut().unwrap() ^= 0xff;
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "bad footer magic", When::Open);
}

#[test]
fn truncated_file_rejected() {
    let bytes = sample_bytes();
    // Cut off half the footer.
    let cut = bytes.len() - FOOTER_LEN / 2;
    let store = MemStore::new();
    store.overwrite(&bytes[..cut]);
    assert_corruption(&store, "truncated file", When::Open);
}

#[test]
fn payload_bitflip_caught_by_checksum() {
    let mut bytes = sample_bytes();
    let handle = first_data_handle(&bytes);
    bytes[handle.offset as usize + 3] ^= 0x01;
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "payload bitflip", When::Validate);
}

#[test]
fn block_type_byte_rejected() {
    let mut bytes = sample_bytes();
    let handle = first_data_handle(&bytes);
    bytes[handle.offset as usize + handle.size as usize] = 99;
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "bad block type", When::Validate);
}

#[test]
fn corrupted_first_restart_offset_rejected() {
    let mut bytes = sample_bytes();
    let handle = first_data_handle(&bytes);
    let base = handle.offset as usize;
    let size = handle.size as usize;
    // restart[0] is the first u32 of the restart array (4-byte count follows).
    bytes[base + size - 8] = 0x05; // must be zero
    repair_checksum(&mut bytes, handle);
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "restart[0] != 0", When::Validate);
}

#[test]
fn corrupted_restart_offset_into_array_rejected() {
    let mut bytes = sample_bytes();
    let handle = first_data_handle(&bytes);
    let base = handle.offset as usize;
    let size = handle.size as usize;
    // Point restart[0] directly at the restart array (>= restart_offset).
    let bad = (size - 8) as u32;
    bytes[base + size - 8..base + size - 4].copy_from_slice(&bad.to_le_bytes());
    repair_checksum(&mut bytes, handle);
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "restart points into array", When::Validate);
}

#[test]
fn corrupted_non_first_restart_offset_rejected() {
    let mut bytes = sample_bytes();
    let handle = first_data_handle(&bytes);
    let base = handle.offset as usize;
    let size = handle.size as usize;
    // restart[1]: make it equal to restart[0] (must be strictly increasing).
    bytes[base + size - 12..base + size - 8].copy_from_slice(&0u32.to_le_bytes());
    repair_checksum(&mut bytes, handle);
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "restart[1] == restart[0]", When::Validate);
}

#[test]
fn corrupted_entry_length_rejected() {
    let mut bytes = sample_bytes();
    let handle = first_data_handle(&bytes);
    let base = handle.offset as usize;
    // The first entry header begins with three varints; inflate value_len.
    // Layout for entry 0: 0(shared), n(non_shared), m(value), key..., value.
    // Skip shared(=1 byte, 0) and non_shared varint.
    let non_shared = bytes[base + 1];
    let pos = 2 + non_shared as usize;
    bytes[base + pos] = 0xff;
    bytes[base + pos + 1] = 0xff;
    bytes[base + pos + 2] = 0x07;
    repair_checksum(&mut bytes, handle);
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "inflated value length", When::Validate);
}

#[test]
fn footer_handle_tampering_rejected() {
    let bytes = sample_bytes();
    let footer = Footer::decode(&bytes[bytes.len() - FOOTER_LEN..]).unwrap();
    let mut bytes = bytes;
    let footer_start = bytes.len() - FOOTER_LEN;

    // Flip a byte inside the *index* handle: the open path reads the index
    // eagerly, so this must fail at open time.
    let meta_encoded = {
        let mut v = Vec::new();
        footer.metaindex.encode_to(&mut v);
        v.len()
    };
    bytes[footer_start + meta_encoded] ^= 0x01;
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "tampered index handle", When::Open);

    // A corrupted metaindex handle is caught by full validation instead.
    let bytes2 = sample_bytes();
    let mut bytes2 = bytes2;
    bytes2[footer_start] ^= 0x01;
    let store2 = MemStore::new();
    store2.overwrite(&bytes2);
    assert_corruption(&store2, "tampered metaindex handle", When::Validate);
}

#[test]
fn index_block_bitflip_rejected_at_open() {
    let bytes = sample_bytes();
    let footer = Footer::decode(&bytes[bytes.len() - FOOTER_LEN..]).unwrap();
    let mut bytes = bytes;
    // Flip a byte inside the index payload; its CRC must fail during open.
    let p = footer.index.offset as usize + 1;
    bytes[p] ^= 0x5a;
    let store = MemStore::new();
    store.overwrite(&bytes);
    assert_corruption(&store, "index block bitflip", When::Open);
}

#[test]
fn hand_built_cross_block_ordering_violation_rejected() {
    // Two data blocks whose key ranges overlap, with a correct-looking footer.
    let mut file = Vec::new();
    let mut b1 = psst::block::BlockBuilder::new(4).unwrap();
    b1.add(b"a", b"1");
    b1.add(b"m", b"1");
    let p1 = b1.finish().to_vec();
    let h1 = encode_block(&mut file, BLOCK_TYPE_DATA, &p1);

    let mut b2 = psst::block::BlockBuilder::new(4).unwrap();
    b2.add(b"b", b"2"); // < "m": violates cross-block ordering
    b2.add(b"z", b"2");
    let p2 = b2.finish().to_vec();
    let h2 = encode_block(&mut file, BLOCK_TYPE_DATA, &p2);

    let mut meta = psst::block::BlockBuilder::new(1).unwrap();
    let mp = meta.finish().to_vec();
    let hm = encode_block(&mut file, BLOCK_TYPE_DATA, &mp);

    // Deliberately wrong separators for the index (both blocks still listed).
    let mut idx = psst::block::BlockBuilder::new(1).unwrap();
    idx.add(b"m", &{
        let mut v = Vec::new();
        h1.encode_to(&mut v);
        v
    });
    idx.add(b"z0", &{
        let mut v = Vec::new();
        h2.encode_to(&mut v);
        v
    });
    let ip = idx.finish().to_vec();
    let hi = encode_block(&mut file, BLOCK_TYPE_DATA, &ip);

    let mut footer = Vec::new();
    Footer::new(hm, hi).encode_to(&mut footer).unwrap();
    file.extend_from_slice(&footer);

    // Sanity: handles are tightly packed like the real builder.
    assert_eq!(h2.end_exclusive().unwrap(), hm.offset);

    let store = MemStore::new();
    store.overwrite(&file);
    assert_corruption(&store, "cross-block order violation", When::Validate);
}

#[test]
fn trailer_length_constant_is_five() {
    assert_eq!(BLOCK_TRAILER_LEN, 5);
    let _ = coding::to_hex(b""); // keep import used even if helpers change
}
