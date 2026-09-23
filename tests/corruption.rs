//! 损坏场景测试：损坏的重启偏移、CRC、页脚、截断等。
//! 要求：检测为 Err，绝不 panic、绝不返回错误数据。

use sst::crc32::crc32;
use sst::varint::get_uvarint;
use sst::{MemStorage, TableOptions, TableReader, TableWriter, FOOTER_SIZE};

fn build_table() -> MemStorage {
    let mem = MemStorage::new();
    let mut w = TableWriter::new(
        mem.clone(),
        TableOptions {
            block_size: 128,
            restart_interval: 2, // 多块、每块多个重启点
        },
    );
    for i in 0..100 {
        w.add(format!("key{:04}", i).as_bytes(), format!("value-{i}").as_bytes())
            .unwrap();
    }
    w.finish().unwrap();
    mem
}

/// 解析页脚，返回 (index_offset, index_size)。
fn parse_footer(bytes: &[u8]) -> (usize, usize) {
    let n = bytes.len();
    let index_offset = u64::from_le_bytes(bytes[n - 32..n - 24].try_into().unwrap()) as usize;
    let index_size = u64::from_le_bytes(bytes[n - 24..n - 16].try_into().unwrap()) as usize;
    (index_offset, index_size)
}

/// 从索引块中读出第 idx 个数据块的 (offset, size)。
fn index_entry(bytes: &[u8], idx: usize) -> (usize, usize) {
    let (index_offset, _) = parse_footer(bytes);
    // 索引块 restart_interval=1，条目顺序解码；简单起见手工走条目区
    let mut off = index_offset;
    let mut prev_key = Vec::new();
    let mut count = 0;
    loop {
        let (shared, n1) = get_uvarint(&bytes[off..]).unwrap();
        let (unshared, n2) = get_uvarint(&bytes[off + n1..]).unwrap();
        let (vlen, n3) = get_uvarint(&bytes[off + n1 + n2..]).unwrap();
        let hdr = n1 + n2 + n3;
        let key_start = off + hdr;
        let val_start = key_start + unshared as usize;
        let val_end = val_start + vlen as usize;
        let mut key = prev_key[..shared as usize].to_vec();
        key.extend_from_slice(&bytes[key_start..val_start]);
        if count == idx {
            let (boff, m1) = get_uvarint(&bytes[val_start..val_end]).unwrap();
            let (bsize, _) = get_uvarint(&bytes[val_start + m1..val_end]).unwrap();
            return (boff as usize, bsize as usize);
        }
        prev_key = key;
        count += 1;
        off = val_end;
    }
}

#[test]
fn corrupted_restart_offset_detected() {
    let mem = build_table();
    let bytes = mem.bytes();
    let (boff, bsize) = index_entry(&bytes, 0);
    let block = &bytes[boff..boff + bsize];
    let num_restarts =
        u32::from_le_bytes(block[bsize - 8..bsize - 4].try_into().unwrap()) as usize;
    assert!(num_restarts >= 2, "need multiple restart points for this test");

    // 篡改 restart[1] 为越界值，并重算 CRC（使 CRC 通过、专测重启点校验）
    mem.mutate(|data| {
        let restarts_begin = boff + bsize - 8 - num_restarts * 4;
        data[restarts_begin + 4..restarts_begin + 8].copy_from_slice(&0xFFFFu32.to_le_bytes());
        let crc = crc32(&data[boff..boff + bsize - 4]);
        data[boff + bsize - 4..boff + bsize].copy_from_slice(&crc.to_le_bytes());
    });

    let t = TableReader::open(mem).expect("open still succeeds (index intact)");
    let err = t.verify().expect_err("verify must reject corrupt restart offset");
    assert!(err.to_string().contains("restart"), "unexpected: {err}");

    // 点查/扫描经过该块时也必须报错而不是 panic 或返回错数据
    let r = t.get(b"key0000");
    assert!(r.is_err(), "get on corrupt block must fail, got {r:?}");
    let r: Vec<_> = t.scan(b"", None).collect();
    assert!(r.iter().any(|e| e.is_err()), "scan must surface the error");
}

#[test]
fn restart_offset_pointing_mid_entry_detected() {
    let mem = build_table();
    let bytes = mem.bytes();
    let (boff, bsize) = index_entry(&bytes, 0);
    let block = &bytes[boff..boff + bsize];
    let num_restarts =
        u32::from_le_bytes(block[bsize - 8..bsize - 4].try_into().unwrap()) as usize;
    let restarts_begin = bsize - 8 - num_restarts * 4;
    let r1 = u32::from_le_bytes(
        block[restarts_begin + 4..restarts_begin + 8].try_into().unwrap(),
    );
    assert!(r1 > 1, "restart[1] should be inside the entries region");

    // restart[1] 向后挪 1 字节：落在某条目中间，且不再指向条目边界
    mem.mutate(|data| {
        let abs = boff + restarts_begin + 4;
        data[abs..abs + 4].copy_from_slice(&(r1 + 1).to_le_bytes());
        let crc = crc32(&data[boff..boff + bsize - 4]);
        data[boff + bsize - 4..boff + bsize].copy_from_slice(&crc.to_le_bytes());
    });

    let t = TableReader::open(mem).unwrap();
    let err = t.verify().expect_err("mid-entry restart offset must be rejected");
    assert!(err.to_string().contains("restart"), "unexpected: {err}");
}

#[test]
fn crc_mismatch_detected_on_read() {
    let mem = build_table();
    let bytes = mem.bytes();
    let (boff, _) = index_entry(&bytes, 0);
    // 翻转块内数据字节，不修正 CRC
    mem.mutate(|data| data[boff + 3] ^= 0xFF);

    let t = TableReader::open(mem).unwrap();
    let err = t.verify().expect_err("verify must catch crc mismatch");
    assert!(err.to_string().contains("crc"), "unexpected: {err}");
    assert!(t.get(b"key0000").is_err(), "get must fail on crc mismatch");
}

#[test]
fn shared_len_beyond_prev_key_detected() {
    let mem = build_table();
    let bytes = mem.bytes();
    let (boff, bsize) = index_entry(&bytes, 0);
    // 块首条目 shared_len 必为 0（单字节 varint 0x00），改成 5 并修 CRC
    assert_eq!(bytes[boff], 0x00, "first entry shared_len should be 0");
    mem.mutate(|data| {
        data[boff] = 0x05;
        let crc = crc32(&data[boff..boff + bsize - 4]);
        data[boff + bsize - 4..boff + bsize].copy_from_slice(&crc.to_le_bytes());
    });

    let t = TableReader::open(mem).unwrap();
    let err = t.verify().expect_err("verify must catch impossible shared_len");
    assert!(err.to_string().contains("shared prefix"), "unexpected: {err}");
}

#[test]
fn truncated_file_rejected() {
    let mem = build_table();
    mem.mutate(|data| data.truncate(data.len() - 10)); // 截掉页脚一部分
    let err = TableReader::open(mem).expect_err("truncated file must not open");
    assert!(err.to_string().contains("corruption"), "unexpected: {err}");
}

#[test]
fn bad_magic_rejected() {
    let mem = build_table();
    mem.mutate(|data| {
        let n = data.len();
        for b in &mut data[n - 8..] {
            *b = 0;
        }
    });
    let err = TableReader::open(mem).expect_err("bad magic must not open");
    assert!(err.to_string().contains("magic"), "unexpected: {err}");
}

#[test]
fn footer_pointing_past_eof_rejected() {
    let mem = build_table();
    mem.mutate(|data| {
        let n = data.len();
        // index_offset 改成接近文件末尾（非法）
        data[n - 32..n - 24].copy_from_slice(&(u64::MAX - 1000).to_le_bytes());
    });
    let err = TableReader::open(mem).expect_err("out-of-bounds index must not open");
    assert!(err.to_string().contains("corruption"), "unexpected: {err}");
}

#[test]
fn entry_count_mismatch_detected() {
    let mem = build_table();
    mem.mutate(|data| {
        let n = data.len();
        // 页脚 num_entries 加 1（页脚无 CRC，属于格式一致性检查）
        let cur = u64::from_le_bytes(data[n - 16..n - 8].try_into().unwrap());
        data[n - 16..n - 8].copy_from_slice(&(cur + 1).to_le_bytes());
    });
    let t = TableReader::open(mem).unwrap();
    let err = t.verify().expect_err("entry count mismatch must be caught");
    assert!(err.to_string().contains("entry count"), "unexpected: {err}");
}

#[test]
fn index_separator_mismatch_detected() {
    let mem = build_table();
    let bytes = mem.bytes();
    let (index_offset, _) = parse_footer(&bytes);
    // 索引块第一个条目的键是 "key0015" 之类（restart_interval=1，首条目 shared=0）。
    // 把索引首条目键的最后一个字节 +1，使分隔键与数据块最后键不一致。
    // 首条目布局：shared(0x00) unshared(0x07) vlen key... → 键首字节在 index_offset+3
    let key_start = index_offset + 3;
    let last_key_byte = key_start + 6; // "keyXXXX" 的最后一个字符
    // 从页脚拿 index_size 以重算索引块 CRC
    let index_size = {
        let n = bytes.len();
        u64::from_le_bytes(bytes[n - 24..n - 16].try_into().unwrap()) as usize
    };
    mem.mutate(|data| {
        data[last_key_byte] = data[last_key_byte].wrapping_add(1);
        let crc = crc32(&data[index_offset..index_offset + index_size - 4]);
        data[index_offset + index_size - 4..index_offset + index_size]
            .copy_from_slice(&crc.to_le_bytes());
    });
    let t = TableReader::open(mem).unwrap();
    let err = t.verify().expect_err("separator mismatch must be caught");
    assert!(err.to_string().contains("separator"), "unexpected: {err}");
}

#[test]
fn footer_size_constant() {
    // 防止意外改动格式常量
    assert_eq!(FOOTER_SIZE, 32);
}
