//! 流式分段编码器。
//!
//! 内存模型：调用方逐行喂入数据，写入器**至多缓冲一个段**（段字典 + 行 ID）。
//! 当行数达到 `max_rows_per_segment` 或缓冲字节逼近 `max_segment_bytes` 时自动刷出
//! 为一个段并开启新段；调用方也可随时显式调用 [`FileWriter::finish_segment`] 制造
//! 段边界。已刷出的字节直接写入底层 `io::Write`，不在内存中保留。

use std::io::Write;

use crate::error::{Error, Result};
use crate::table::DictTable;
use crate::varint;

/// 段类型字节：字符串字典编码段。
pub const SEGMENT_KIND: u8 = 1;

/// 编码侧限制（传 `Default::default()` 得到保守默认值）。
#[derive(Debug, Clone)]
pub struct EncodeLimits {
    /// 单段内存缓冲的保守上限（字节）：达到且段非空时自动刷段。
    pub max_segment_bytes: usize,
    /// 单段最大行数；达到后自动刷段。
    pub max_rows_per_segment: u64,
    /// 单个值（UTF-8 字节数）上限。
    pub max_value_len: u64,
    /// 单段字典基数（不同值数量）上限。
    pub max_dict_cardinality: usize,
}

impl Default for EncodeLimits {
    fn default() -> Self {
        EncodeLimits {
            max_segment_bytes: 64 * 1024 * 1024,
            max_rows_per_segment: 1_000_000,
            max_value_len: 16 * 1024 * 1024,
            max_dict_cardinality: 10_000_000,
        }
    }
}

/// 编码统计。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WriteStats {
    /// 已写出的段数。
    pub segments: u64,
    /// 总行数（含 NULL）。
    pub rows: u64,
    /// NULL 行数。
    pub nulls: u64,
    /// 各段字典基数之和（跨段重复值按段重复计数）。
    pub distinct_sum: u64,
    /// 写入底层输出的总字节数。
    pub bytes_written: u64,
}

/// 一个正在累积的段。
struct OpenSegment {
    dict: DictTable,
    /// 非 NULL 行的字典 ID，按行序。
    ids: Vec<u64>,
    /// NULL 行的行号，严格升序。
    nulls: Vec<u64>,
    /// 字典字符串字节总数。
    dict_bytes: usize,
    /// 刷出时将使用的段编号。
    segment_no: u32,
}

impl OpenSegment {
    fn new(segment_no: u32) -> Self {
        OpenSegment {
            dict: DictTable::new(),
            ids: Vec::new(),
            nulls: Vec::new(),
            dict_bytes: 0,
            segment_no,
        }
    }

    fn row_count(&self) -> u64 {
        (self.ids.len() + self.nulls.len()) as u64
    }

    /// 缓冲占用的保守上界：字典字节 + 每个变长字段最多 10 字节的余量。
    fn estimated_bytes(&self) -> usize {
        self.dict_bytes + self.dict.len() * 10 + self.ids.len() * 10 + self.nulls.len() * 10
    }
}

/// 流式文件编码器。
pub struct FileWriter<W: Write> {
    inner: W,
    limits: EncodeLimits,
    current: OpenSegment,
    stats: WriteStats,
}

impl<W: Write> FileWriter<W> {
    /// 写出文件头并返回编码器。
    pub fn new(mut inner: W, limits: EncodeLimits) -> Result<Self> {
        inner.write_all(crate::MAGIC).map_err(io_err)?;
        inner
            .write_all(&[crate::FORMAT_VERSION, crate::FLAGS])
            .map_err(io_err)?;
        Ok(FileWriter {
            inner,
            limits,
            current: OpenSegment::new(0),
            stats: WriteStats {
                bytes_written: (crate::MAGIC.len() as u64) + 2,
                ..WriteStats::default()
            },
        })
    }

    /// 写入一行；`None` 表示 NULL，`Some("")` 是空字符串（二者严格区分）。
    pub fn write_row(&mut self, value: Option<&str>) -> Result<()> {
        // 段容量已满：先自动刷段。
        if self.current.row_count() >= self.limits.max_rows_per_segment {
            self.flush_segment()?;
        }
        // 缓冲逼近上限且段非空：先刷段，保证内存占用有界。
        if self.current.row_count() > 0
            && self.current.estimated_bytes() >= self.limits.max_segment_bytes
        {
            self.flush_segment()?;
        }

        let row = self.current.row_count();
        match value {
            None => {
                self.current.nulls.push(row);
                self.stats.nulls += 1;
            }
            Some(s) => {
                let bytes = s.as_bytes();
                if bytes.len() as u64 > self.limits.max_value_len {
                    return Err(Error::ValueBytesLimitExceeded {
                        limit: self.limits.max_value_len,
                    });
                }
                let before = self.current.dict.len();
                let id = self.current.dict.intern(bytes);
                if self.current.dict.len() > before {
                    // 真正新建的字典条目才累计字节。
                    self.current.dict_bytes += bytes.len();
                    if self.current.dict.len() > self.limits.max_dict_cardinality {
                        return Err(Error::CardinalityTooLarge {
                            cardinality: self.current.dict.len(),
                            max: self.limits.max_dict_cardinality,
                        });
                    }
                }
                self.current.ids.push(id);
            }
        }
        self.stats.rows += 1;
        Ok(())
    }

    /// 显式结束当前段；空段会被忽略（不产生段帧）。
    pub fn finish_segment(&mut self) -> Result<()> {
        if self.current.row_count() == 0 {
            return Ok(());
        }
        self.flush_segment()
    }

    fn flush_segment(&mut self) -> Result<()> {
        let next_no = self.stats.segments as u32;
        let seg = std::mem::replace(&mut self.current, OpenSegment::new(next_no));
        let frame = serialize_segment(seg.segment_no, &seg.dict, &seg.ids, &seg.nulls)?;
        if frame.len() > self.limits.max_segment_bytes {
            // 单行/单段无法靠切分挽救：如实报错，而不是悄悄超用内存。
            return Err(Error::MemoryLimitExceeded {
                limit: self.limits.max_segment_bytes,
            });
        }
        self.inner.write_all(&frame).map_err(io_err)?;
        self.stats.segments += 1;
        self.stats.bytes_written += frame.len() as u64;
        self.stats.distinct_sum += seg.dict.len() as u64;
        self.current.segment_no = self.stats.segments as u32;
        Ok(())
    }

    /// 结束写入：刷出最后一个非空段并返回统计。
    pub fn finish(mut self) -> Result<WriteStats> {
        if self.current.row_count() > 0 {
            self.flush_segment()?;
        }
        self.inner.flush().map_err(io_err)?;
        Ok(self.stats)
    }
}

fn io_err(e: std::io::Error) -> Error {
    Error::Io(e.to_string())
}

/// 序列化单个段为一整帧（合并器也复用此函数）。
///
/// 帧布局（详见 `FORMAT.md`）：
/// ```text
/// kind     : u8 = 1
/// body_len : varint（body 的字节数，不含 crc）
/// body     : segment_no, 行数, 字典, NULL 位, 行 ID
/// crc32    : u32 LE，覆盖 kind..body 的全部字节
/// ```
pub fn serialize_segment(
    segment_no: u32,
    dict: &DictTable,
    ids: &[u64],
    nulls: &[u64],
) -> Result<Vec<u8>> {
    let row_count = ids.len() as u64 + nulls.len() as u64;
    let cardinality = dict.len() as u64;

    // 一致性校验：NULL 位置必须严格升序且在行数范围内；ID 不得越界。
    let mut last: Option<u64> = None;
    for &p in nulls {
        if p >= row_count {
            return Err(Error::BadSegment("null position out of range"));
        }
        if last.is_some_and(|x| x >= p) {
            return Err(Error::BadSegment("null positions not strictly ascending"));
        }
        last = Some(p);
    }
    for &id in ids {
        if id >= cardinality {
            return Err(Error::DictIdOutOfRange {
                id,
                len: dict.len(),
            });
        }
    }

    let mut body = Vec::new();
    varint::write_u64(&mut body, u64::from(segment_no));
    varint::write_u64(&mut body, row_count);
    varint::write_u64(&mut body, cardinality);
    for key in dict.iter() {
        varint::write_u64(&mut body, key.len() as u64);
        body.extend_from_slice(key);
    }
    varint::write_u64(&mut body, nulls.len() as u64);
    for &p in nulls {
        varint::write_u64(&mut body, p);
    }
    for &id in ids {
        varint::write_u64(&mut body, id);
    }

    let mut frame = Vec::with_capacity(body.len() + 16);
    frame.push(SEGMENT_KIND);
    varint::write_u64(&mut frame, body.len() as u64);
    frame.extend_from_slice(&body);

    let crc = crate::crc32::checksum(&frame);
    frame.extend_from_slice(&crc.to_le_bytes());
    Ok(frame)
}
