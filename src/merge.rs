//! 多段字典合并与跨段 ID 重映射。
//!
//! 输入是若干已解码段（[`SegmentData`]，可来自一个或多个 `.cdc` 文件）。
//! 每个输入段都有自己的局部字典与局部 ID；合并输出**单个段**，包含一份
//! **全局去重字典**，并把每行的局部 ID 重映射为全局 ID：
//!
//! - 相同字节无论出现在哪一段、原本是什么局部 ID，合并后都是同一个全局 ID
//!   （映射路径：局部 ID → 字节 → 全局 ID，以字节相等为唯一锚点）；
//! - NULL 不进字典，其位置按段顺序换算到全局行坐标后原样保留；
//! - 空字符串是字典中的普通条目，与 NULL 严格区分。
//!
//! **字典顺序不影响解码结果**：全局字典默认按首次出现顺序编号；置
//! [`MergeOptions::canonical`] 后改为按 UTF-8 字节序编号。两种顺序下逐行解码出的
//! 值完全一致（只是同一个字节对应的整数 ID 不同）。
//!
//! 合并是**有界批量**操作（需要见到全部段才能确定全局字典），受 [`MergeLimits`]
//! 约束；流式性质由编码/解码侧保证。

use crate::error::{Error, Result};
use crate::reader::SegmentData;
use crate::table::DictTable;
use crate::writer::serialize_segment;

/// 合并侧限制。
#[derive(Debug, Clone)]
pub struct MergeLimits {
    /// 全局字典不同值数量上限。
    pub max_distinct: usize,
    /// 全局字典字符串字节总数上限。
    pub max_dict_bytes: usize,
    /// 输出行数上限。
    pub max_rows: u64,
}

impl Default for MergeLimits {
    fn default() -> Self {
        MergeLimits {
            max_distinct: 20_000_000,
            max_dict_bytes: 256 * 1024 * 1024,
            max_rows: 100_000_000,
        }
    }
}

/// 合并选项。
#[derive(Debug, Clone, Default)]
pub struct MergeOptions {
    /// 限制。
    pub limits: MergeLimits,
    /// 为 true 时全局字典按 UTF-8 字节序编号，输出字节不再依赖输入段的先后；
    /// 为 false 时按“跨段首次出现顺序”编号。
    pub canonical: bool,
}

/// 合并统计。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MergeStats {
    /// 输入段数。
    pub input_segments: u64,
    /// 总行数（含 NULL）。
    pub rows: u64,
    /// NULL 行数。
    pub nulls: u64,
    /// 合并后全局字典基数。
    pub global_distinct: usize,
    /// 合并前各段基数之和（含跨段重复）。
    pub local_distinct_sum: usize,
    /// 因跨段重复而省掉的字典条目数 = 局部基数和 − 全局基数。
    pub deduped: usize,
}

/// 将若干段合并为一个新的 CDC 文件字节流（含文件头，正文为单个全局字典段）。
pub fn merge_sources(
    segments: &[SegmentData],
    options: &MergeOptions,
) -> Result<(Vec<u8>, MergeStats)> {
    let limits = &options.limits;

    // ---- 第一遍：统计行数/NULL，构建全局去重字典（首次出现顺序）----
    let mut first_seen = DictTable::new();
    let mut dict_bytes: usize = 0;
    let mut rows: u64 = 0;
    let mut nulls: u64 = 0;
    let mut local_sum = 0usize;

    for seg in segments {
        rows = rows
            .checked_add(seg.row_count())
            .ok_or(Error::BadSegment("row count overflow during merge"))?;
        if rows > limits.max_rows {
            return Err(Error::RowsLimitExceeded {
                limit: limits.max_rows,
            });
        }
        nulls = nulls.saturating_add(seg.null_positions().len() as u64);
        local_sum += seg.cardinality();
        for entry in seg.dict_entries() {
            let before = first_seen.len();
            first_seen.intern(entry);
            if first_seen.len() > before {
                dict_bytes = dict_bytes
                    .checked_add(entry.len())
                    .ok_or(Error::BadSegment("dict byte count overflow"))?;
                if first_seen.len() > limits.max_distinct {
                    return Err(Error::CardinalityTooLarge {
                        cardinality: first_seen.len(),
                        max: limits.max_distinct,
                    });
                }
                if dict_bytes > limits.max_dict_bytes {
                    return Err(Error::DictBytesLimitExceeded {
                        limit: limits.max_dict_bytes,
                    });
                }
            }
        }
    }

    // ---- 确定最终全局字典的编号表 ----
    // global_dict 按最终 ID 顺序持有字节；lookup 负责 字节 -> 最终全局 ID。
    let mut global_dict = DictTable::new();
    if options.canonical {
        let mut ordered: Vec<&Vec<u8>> = first_seen.keys().iter().collect();
        ordered.sort();
        for k in &ordered {
            global_dict.intern(k);
        }
    } else {
        for k in first_seen.keys() {
            global_dict.intern(k);
        }
    }

    // ---- 第二遍：逐段把局部 ID 重映射为全局 ID，NULL 换算到全局行坐标 ----
    let mut global_ids: Vec<u64> = Vec::with_capacity(rows.min(1 << 20) as usize);
    let mut global_nulls: Vec<u64> = Vec::with_capacity(nulls.min(1 << 20) as usize);
    let mut row_offset: u64 = 0;

    for seg in segments {
        // 段内非 NULL 行按行序重映射。
        for &local_id in seg.ids() {
            let bytes =
                seg.dict_entries()
                    .get(local_id as usize)
                    .ok_or(Error::DictIdOutOfRange {
                        id: local_id,
                        len: seg.cardinality(),
                    })?;
            let gid = global_dict
                .find(bytes)
                .expect("byte string was interned into global dict");
            global_ids.push(gid);
        }
        // NULL 位置 = 段内位置 + 该段之前的累计行数。
        for &p in seg.null_positions() {
            global_nulls.push(row_offset + p);
        }
        row_offset += seg.row_count();
    }
    global_nulls.sort_unstable(); // 多段拼接后天然升序，排序仅为稳妥保证

    debug_assert_eq!(global_ids.len() as u64 + global_nulls.len() as u64, rows);

    // ---- 序列化：0 行时输出纯文件头；否则输出单个全局字典段 ----
    let mut out = Vec::new();
    out.extend_from_slice(crate::MAGIC);
    out.push(crate::FORMAT_VERSION);
    out.push(crate::FLAGS);
    if rows > 0 {
        let frame = serialize_segment(0, &global_dict, &global_ids, &global_nulls)?;
        out.extend_from_slice(&frame);
    }

    let stats = MergeStats {
        input_segments: segments.len() as u64,
        rows,
        nulls,
        global_distinct: global_dict.len(),
        local_distinct_sum: local_sum,
        deduped: local_sum.saturating_sub(global_dict.len()),
    };
    Ok((out, stats))
}
