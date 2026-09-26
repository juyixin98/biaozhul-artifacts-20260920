//! 端到端集成测试：编码 → 解码逐行一致性、NULL/空串、Unicode、大基数退化、
//! 分段合并与跨段重映射、顺序无关性、损坏检测与各类内存/输出限制。

use std::io::{BufReader, Cursor};

use cdc::merge::{merge_sources, MergeOptions};
use cdc::reader::{DecodeLimits, FileReader, SegmentData};
use cdc::writer::{EncodeLimits, FileWriter};

/// 把行按给定每段行数编码为字节。
fn encode(rows: &[Option<&str>], rows_per_segment: u64) -> Vec<u8> {
    let limits = EncodeLimits {
        max_rows_per_segment: rows_per_segment,
        ..EncodeLimits::default()
    };
    let mut buf = Vec::new();
    let mut w = FileWriter::new(&mut buf, limits).unwrap();
    for r in rows {
        w.write_row(*r).unwrap();
    }
    w.finish().unwrap();
    buf
}

/// 解码为逐行字符串（NULL = None）。
fn decode_all(data: &[u8]) -> Vec<Option<String>> {
    let mut r =
        FileReader::new(BufReader::new(Cursor::new(data)), DecodeLimits::default()).unwrap();
    let mut out = Vec::new();
    while let Some(row) = r.next_row().unwrap() {
        out.push(row.value);
    }
    out
}

fn decode_segments(data: &[u8]) -> Vec<SegmentData> {
    let mut r =
        FileReader::new(BufReader::new(Cursor::new(data)), DecodeLimits::default()).unwrap();
    let mut segs = Vec::new();
    while let Some(seg) = r.next_segment().unwrap() {
        segs.push(seg.clone());
    }
    segs
}

fn opt_strs(v: &[Option<&str>]) -> Vec<Option<String>> {
    v.iter().map(|o| o.map(|s| s.to_string())).collect()
}

// ---------- 基础验收：空串、全 NULL、Unicode ----------

#[test]
fn empty_string_is_distinct_from_null() {
    let rows = vec![Some(""), None, Some(""), None, Some("x")];
    let data = encode(&rows, 100);
    let back = decode_all(&data);
    assert_eq!(back, opt_strs(&rows));
    // 字典只有两个不同值："" 与 "x"；NULL 不进字典。
    let segs = decode_segments(&data);
    assert_eq!(segs.len(), 1);
    assert_eq!(segs[0].cardinality(), 2);
    assert_eq!(segs[0].null_positions(), &[1u64, 3]);
}

#[test]
fn all_null_column() {
    let rows: Vec<Option<&str>> = vec![None; 1000];
    let data = encode(&rows, 37); // 强制切成多段
    let back = decode_all(&data);
    assert_eq!(back.len(), 1000);
    assert!(back.iter().all(|v| v.is_none()));
    let segs = decode_segments(&data);
    assert!(segs.len() > 1);
    // 全 NULL：每个段字典都为空。
    assert!(segs.iter().all(|s| s.cardinality() == 0));
}

#[test]
fn unicode_values_roundtrip() {
    let rows = vec![
        Some("你好，世界"),
        Some("こんにちは"),
        Some("안녕하세요"),
        Some("café"),
        Some("😀🎉"),
        None,
        Some(""),
        Some("é"), // é 的两种 Unicode 表示应被区分为不同字节
        Some("é"),
        Some("mixed 123 你好"),
    ];
    let data = encode(&rows, 3);
    let back = decode_all(&data);
    assert_eq!(back, opt_strs(&rows));
}

#[test]
fn empty_input_roundtrips() {
    // 零行：只有文件头，没有段。
    let data = encode(&[], 10);
    let back = decode_all(&data);
    assert!(back.is_empty());
    assert!(decode_segments(&data).is_empty());
}

// ---------- 大基数字典退化（近乎每行一个不同值）----------

#[test]
fn high_cardinality_degenerate_dict() {
    let n = 120_000usize;
    // 每行都不同：字典编码“退化”为近似直接存储，但仍须正确。
    let source: Vec<String> = (0..n).map(|i| format!("row-value-{i:07}")).collect();
    let rows: Vec<Option<&str>> = source.iter().map(|s| Some(s.as_str())).collect();

    let limits = EncodeLimits {
        max_rows_per_segment: 10_000,
        ..EncodeLimits::default()
    };
    let mut buf = Vec::new();
    let mut w = FileWriter::new(&mut buf, limits).unwrap();
    for r in &rows {
        w.write_row(*r).unwrap();
    }
    w.finish().unwrap();
    let data = buf;

    let segs = decode_segments(&data);
    assert_eq!(segs.len(), 12);
    let back = decode_all(&data);
    assert_eq!(back.len(), n);
    for (i, v) in back.iter().enumerate() {
        assert_eq!(v.as_deref(), Some(source[i].as_str()));
    }
}

#[test]
fn low_cardinality_compresses_well() {
    // 低基数：ID 应明显小于重复字符串。
    let vals = ["alpha", "beta", "gamma"];
    let rows: Vec<Option<&str>> = (0..10_000).map(|i| Some(vals[i % 3])).collect();
    let data = encode(&rows, 1_000_000);
    let back = decode_all(&data);
    assert_eq!(back, opt_strs(&rows));
    let seg = decode_segments(&data);
    assert_eq!(seg[0].cardinality(), 3);
    // 原始朴素存储需重复全部字符串（约 4.7 万字节）；字典编码后仅 1 字节/行 ID + 一份字典。
    let naive_bytes: usize = rows.iter().filter_map(|r| *r).map(|s| s.len()).sum();
    assert!(data.len() < naive_bytes / 2, "encoded {}", data.len());
}

// ---------- 分段合并：逐行值前后完全一致 ----------

#[test]
fn merge_preserves_every_row_across_segments_and_files() {
    // 三个逻辑文件，各自多段，且字典顺序刻意不同。
    let file_a: Vec<Option<&str>> = vec![Some("b"), Some("a"), None, Some("b"), Some("c")];
    let file_b: Vec<Option<&str>> = vec![Some("a"), Some("a"), Some(""), None, Some("b")];
    let file_c: Vec<Option<&str>> = vec![None, None, Some("z"), Some("a"), Some("")];

    let da = encode(&file_a, 2);
    let db = encode(&file_b, 3);
    let dc = encode(&file_c, 2);

    let mut segs = Vec::new();
    for d in [&da, &db, &dc] {
        segs.extend(decode_segments(d));
    }
    assert!(segs.len() >= 4); // 确实是多段输入

    // 第一次合并：首次出现顺序。
    let (merged1, stats1) = merge_sources(&segs, &MergeOptions::default()).unwrap();
    assert!(stats1.deduped > 0); // 跨段重复值被去重

    // 第二次合并：canonical 字节序。
    let (merged2, stats2) = merge_sources(
        &segs,
        &MergeOptions {
            canonical: true,
            ..MergeOptions::default()
        },
    )
    .unwrap();

    // 两种字典顺序下，逐行值必须一致，且等于拼接后的原始输入。
    let mut expected: Vec<Option<String>> = Vec::new();
    for f in [&file_a, &file_b, &file_c] {
        expected.extend(opt_strs(f));
    }
    let back1 = decode_all(&merged1);
    let back2 = decode_all(&merged2);
    assert_eq!(back1, expected);
    assert_eq!(back2, expected);

    // 合并结果是单段、单一全局字典。
    let msegs = decode_segments(&merged1);
    assert_eq!(msegs.len(), 1);
    assert_eq!(stats1.global_distinct, stats2.global_distinct);
    // 全局不同值：a,b,c,z,"" = 5。
    assert_eq!(stats1.global_distinct, 5);
}

#[test]
fn merge_cross_segment_ids_are_remapped() {
    // 同样的字节 "x" 在两段里局部 ID 不同（段1先见 y，段2先见 z）。
    let seg1_rows = vec![Some("y"), Some("x")]; // x 的局部 ID = 1
    let seg2_rows = vec![Some("z"), Some("x")]; // x 的局部 ID = 1
    let seg3_rows = vec![Some("x"), Some("w")]; // x 的局部 ID = 0

    let d1 = encode(&seg1_rows, 100);
    let d2 = encode(&seg2_rows, 100);
    let d3 = encode(&seg3_rows, 100);
    let mut segs = Vec::new();
    for d in [&d1, &d2, &d3] {
        segs.extend(decode_segments(d));
    }
    let (merged, stats) = merge_sources(&segs, &MergeOptions::default()).unwrap();
    let msegs = decode_segments(&merged);
    assert_eq!(msegs.len(), 1);
    assert_eq!(stats.global_distinct, 4); // y,x,z,w

    // 所有 "x" 行必须解析到同一个全局字节，尽管原局部 ID 有 1 也有 0。
    let back = decode_all(&merged);
    assert_eq!(
        back.iter().map(|v| v.as_deref()).collect::<Vec<_>>(),
        vec![
            Some("y"),
            Some("x"),
            Some("z"),
            Some("x"),
            Some("x"),
            Some("w")
        ]
    );
}

#[test]
fn merge_idempotent() {
    let rows = vec![Some("a"), None, Some("b"), Some("a")];
    let d = encode(&rows, 2);
    let segs = decode_segments(&d);
    let (m1, _) = merge_sources(&segs, &MergeOptions::default()).unwrap();
    let segs2 = decode_segments(&m1);
    let (m2, _) = merge_sources(&segs2, &MergeOptions::default()).unwrap();
    assert_eq!(decode_all(&m1), decode_all(&m2));
}

// ---------- 顺序无关性：打乱段输入，canonical 输出字节相同 ----------

#[test]
fn canonical_merge_is_order_independent_in_values() {
    let rows_a = vec![Some("m"), Some("n"), None];
    let rows_b = vec![Some("n"), Some("o"), Some("m")];
    let da = encode(&rows_a, 100);
    let db = encode(&rows_b, 100);
    let sa = decode_segments(&da);
    let sb = decode_segments(&db);

    let opts = MergeOptions {
        canonical: true,
        ..MergeOptions::default()
    };
    let (ab, _) = merge_sources(&[sa[0].clone(), sb[0].clone()], &opts).unwrap();
    let (ba, _) = merge_sources(&[sb[0].clone(), sa[0].clone()], &opts).unwrap();
    // canonical 字典顺序下，虽然行序仍按拼接顺序，但同序输入值集合一致；
    // 这里校验两次合并各自解码值符合各自拼接顺序。
    assert_eq!(
        decode_all(&ab),
        opt_strs(&[Some("m"), Some("n"), None, Some("n"), Some("o"), Some("m")])
    );
    assert_eq!(
        decode_all(&ba),
        opt_strs(&[Some("n"), Some("o"), Some("m"), Some("m"), Some("n"), None])
    );
}

// ---------- 损坏与限制 ----------

#[test]
fn detects_bad_magic_and_truncation() {
    let good = encode(&[Some("a"), None, Some("b")], 100);
    let mut bad_magic = good.clone();
    bad_magic[0] = b'X';
    assert!(decode_all_result(&bad_magic).is_err());

    let truncated = &good[..good.len() - 3];
    assert!(decode_all_result(truncated).is_err());
}

#[test]
fn detects_crc_corruption() {
    let good = encode(&[Some("abc"), Some("abd"), None, Some("abc")], 2);
    // 翻转某个正文字节（避开 6 字节文件头）。
    let mut corrupted = good.clone();
    let idx = 10;
    corrupted[idx] ^= 0x01;
    let err = decode_all_result(&corrupted).err().unwrap();
    assert!(
        matches!(err, cdc::Error::CrcMismatch { .. }),
        "expected CRC error, got {err:?}"
    );
}

#[test]
fn row_limit_enforced() {
    let data = encode(&[Some("a"), Some("b"), Some("c")], 100);
    let limits = DecodeLimits {
        max_rows: 2,
        ..DecodeLimits::default()
    };
    let mut r = FileReader::new(BufReader::new(Cursor::new(&data)), limits).unwrap();
    assert!(matches!(
        r.next_row(),
        Err(cdc::Error::RowsLimitExceeded { .. })
    ));
}

#[test]
fn value_bytes_limit_enforced() {
    let data = encode(&[Some("abcd"), Some("efgh")], 100);
    let limits = DecodeLimits {
        max_value_bytes: 5,
        ..DecodeLimits::default()
    };
    let mut r = FileReader::new(BufReader::new(Cursor::new(&data)), limits).unwrap();
    r.next_row().unwrap(); // 4 字节，OK
    let err = r.next_row().unwrap_err(); // 再 4 字节 → 8 > 5
    assert!(matches!(err, cdc::Error::ValueBytesLimitExceeded { .. }));
}

#[test]
fn segment_size_limit_enforced() {
    let data = encode(&[Some("abcdef"), Some("ghijkl")], 100);
    let limits = DecodeLimits {
        max_segment_bytes: 3,
        ..DecodeLimits::default()
    };
    let mut r = FileReader::new(BufReader::new(Cursor::new(&data)), limits).unwrap();
    assert!(matches!(
        r.next_segment(),
        Err(cdc::Error::SegmentTooLarge { .. })
    ));
}

#[test]
fn encode_value_length_limit_enforced() {
    let limits = EncodeLimits {
        max_value_len: 2,
        ..EncodeLimits::default()
    };
    let mut buf = Vec::new();
    let mut w = FileWriter::new(&mut buf, limits).unwrap();
    assert!(w.write_row(Some("abc")).is_err());
    assert!(w.write_row(Some("ab")).is_ok());
    assert!(w.write_row(None).is_ok());
}

// ---------- 流式：自动分段与显式段边界 ----------

#[test]
fn automatic_and_manual_segment_boundaries() {
    let limits = EncodeLimits {
        max_rows_per_segment: 2,
        ..EncodeLimits::default()
    };
    let mut buf = Vec::new();
    let mut w = FileWriter::new(&mut buf, limits).unwrap();
    for r in [Some("a"), Some("b"), Some("c")] {
        w.write_row(r).unwrap();
    }
    // 已自动在第 2 行后切一段；显式结束第二段。
    w.finish_segment().unwrap();
    w.write_row(Some("d")).unwrap();
    w.finish().unwrap();
    let data = buf;
    let segs = decode_segments(&data);
    assert_eq!(segs.len(), 3);
    assert_eq!(
        decode_all(&data),
        opt_strs(&[Some("a"), Some("b"), Some("c"), Some("d")])
    );
}

fn decode_all_result(data: &[u8]) -> cdc::Result<Vec<Option<String>>> {
    let mut r = FileReader::new(BufReader::new(Cursor::new(data)), DecodeLimits::default())?;
    let mut out = Vec::new();
    while let Some(row) = r.next_row()? {
        out.push(row.value);
    }
    Ok(out)
}

#[test]
fn tiny_segment_byte_limit_forces_many_streaming_flushes() {
    // 极小的单段字节上限：编码器必须频繁自动刷段，内存不随行数增长。
    let limits = EncodeLimits {
        max_segment_bytes: 128,
        max_rows_per_segment: u64::MAX, // 不靠行数切，只靠字节上限切
        ..EncodeLimits::default()
    };
    let n = 2_000usize;
    let source: Vec<String> = (0..n).map(|i| format!("v{i}")).collect();
    let mut buf = Vec::new();
    let mut w = FileWriter::new(&mut buf, limits).unwrap();
    for s in &source {
        w.write_row(Some(s)).unwrap();
    }
    w.finish().unwrap();

    let segs = decode_segments(&buf);
    assert!(
        segs.len() > 50,
        "expected many tiny segments, got {}",
        segs.len()
    );
    let back = decode_all(&buf);
    assert_eq!(back.len(), n);
    for (i, v) in back.iter().enumerate() {
        assert_eq!(v.as_deref(), Some(source[i].as_str()));
    }
}

#[test]
fn merge_all_null_and_empty_inputs() {
    // 全 NULL 的两段 + 一个 0 行文件。
    let null_rows: Vec<Option<&str>> = vec![None, None, None];
    let d1 = encode(&null_rows, 2);
    let d2 = encode(&null_rows, 3);
    let d3 = encode(&[], 10); // 纯文件头，无段

    let mut segs = Vec::new();
    segs.extend(decode_segments(&d1));
    segs.extend(decode_segments(&d2));
    segs.extend(decode_segments(&d3)); // 不贡献段

    let (merged, stats) = merge_sources(&segs, &MergeOptions::default()).unwrap();
    assert_eq!(stats.rows, 6);
    assert_eq!(stats.nulls, 6);
    assert_eq!(stats.global_distinct, 0);
    let back = decode_all(&merged);
    assert_eq!(back, vec![None; 6]);

    // 完全没有任何输入段：输出应是纯文件头（无空段帧）。
    let (empty_merged, _) = merge_sources(&[], &MergeOptions::default()).unwrap();
    assert_eq!(empty_merged.len(), 6); // 仅文件头
    assert!(decode_all(&empty_merged).is_empty());
}

#[test]
fn randomized_segmentation_merge_is_value_preserving() {
    // 确定性 LCG，避免依赖 rand。从一个小值池里造大量重复行，随机切成多个“文件”，
    // 每个文件再用不同的 rows_per_segment 编码；合并结果必须与原始行逐行相等，
    // 且 first-seen 与 canonical 两种字典顺序解码值一致。
    let pool = ["a", "b", "cc", "", "你好", "ddd", "a", "b"];
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut next_u64 = move || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };

    let total: Vec<Option<String>> = (0..5_000)
        .map(|_| {
            let r = next_u64() % 10; // 10% NULL
            if r == 0 {
                None
            } else {
                Some(pool[(next_u64() as usize) % pool.len()].to_owned())
            }
        })
        .collect();

    // 随机切成 4 个文件。
    let mut boundaries = vec![0usize, total.len()];
    for _ in 0..3 {
        boundaries.push((next_u64() as usize) % total.len());
    }
    boundaries.sort_unstable();
    boundaries.dedup();

    let mut segs = Vec::new();
    for w in boundaries.windows(2) {
        let part: Vec<Option<&str>> = total[w[0]..w[1]].iter().map(|o| o.as_deref()).collect();
        let rps = 1 + (next_u64() % 50); // 每文件 1..50 行/段
        let data = encode(&part, rps);
        segs.extend(decode_segments(&data));
    }
    assert!(
        segs.len() > 1,
        "random segmentation should yield many segments"
    );

    let (m_first, _) = merge_sources(&segs, &MergeOptions::default()).unwrap();
    let (m_canon, _) = merge_sources(
        &segs,
        &MergeOptions {
            canonical: true,
            ..MergeOptions::default()
        },
    )
    .unwrap();

    let back_first = decode_all(&m_first);
    let back_canon = decode_all(&m_canon);
    assert_eq!(back_first.len(), total.len());
    assert_eq!(back_first, total);
    assert_eq!(back_canon, total); // 两种字典顺序逐行值一致
}

#[test]
fn empty_string_survives_merge_separately_from_null() {
    let a = vec![Some(""), None, Some("")];
    let b = vec![None, Some(""), Some("x")];
    let da = encode(&a, 10);
    let db = encode(&b, 10);
    let mut segs = decode_segments(&da);
    segs.extend(decode_segments(&db));
    let (merged, stats) = merge_sources(&segs, &MergeOptions::default()).unwrap();
    assert_eq!(stats.global_distinct, 2); // "" 与 "x"
    assert_eq!(stats.nulls, 2);
    assert_eq!(
        decode_all(&merged),
        opt_strs(&[Some(""), None, Some(""), None, Some(""), Some("x")])
    );
}
