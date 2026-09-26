//! 流式分段解码器。
//!
//! 内存模型：任意时刻只缓冲**一个段**的字典与行数据；段帧大小、字典字节数、
//! 总行数与解码输出字节数均受 [`DecodeLimits`] 约束。段通过 [`FileReader::next_segment`]
//! 逐段读出（返回借用引用），也可用 [`FileReader::next_row`] 逐行读出。

use std::io::{BufRead, Read};

use crate::error::{Error, Result};
use crate::varint;
use crate::writer::SEGMENT_KIND;

/// 解码侧限制。任何一项被突破都会返回错误而非继续分配内存。
#[derive(Debug, Clone)]
pub struct DecodeLimits {
    /// 单个段帧载荷（body）最大字节数。
    pub max_segment_bytes: u64,
    /// 全文件允许的累计行数。
    pub max_rows: u64,
    /// 全文件允许的累计解码输出字节数（每个非 NULL 行的字符串字节之和）。
    pub max_value_bytes: u64,
    /// 单段字典字符串字节上限。
    pub max_dict_bytes: usize,
    /// 单段字典基数上限。
    pub max_cardinality: usize,
    /// 全文件段数上限。
    pub max_segments: u64,
}

impl Default for DecodeLimits {
    fn default() -> Self {
        DecodeLimits {
            max_segment_bytes: 256 * 1024 * 1024,
            max_rows: 100_000_000,
            max_value_bytes: 1024 * 1024 * 1024,
            max_dict_bytes: 128 * 1024 * 1024,
            max_cardinality: 20_000_000,
            max_segments: 100_000,
        }
    }
}

/// 解码统计。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReadStats {
    /// 已读出的段数。
    pub segments: u64,
    /// 已读出的行数（含 NULL）。
    pub rows: u64,
    /// 已读出的 NULL 行数。
    pub nulls: u64,
    /// 已输出的非 NULL 字符串字节总数。
    pub value_bytes: u64,
}

/// 一个已解码的段。段内数据可按行随机访问。
#[derive(Debug, Clone)]
pub struct SegmentData {
    /// 段编号（取自文件，合并前每段各自独立）。
    segment_no: u32,
    /// 字典条目，按 ID 顺序。
    dict: Vec<Vec<u8>>,
    /// 非 NULL 行的字典 ID，按行序（剔除 NULL 行后的顺序）。
    ids: Vec<u64>,
    /// NULL 行号，严格升序。
    nulls: Vec<u64>,
}

impl SegmentData {
    /// 段编号。
    pub fn segment_no(&self) -> u32 {
        self.segment_no
    }

    /// 段内行数（含 NULL）。
    pub fn row_count(&self) -> u64 {
        self.ids.len() as u64 + self.nulls.len() as u64
    }

    /// 段内字典基数。
    pub fn cardinality(&self) -> usize {
        self.dict.len()
    }

    /// 字典条目（按 ID 顺序）。
    pub fn dict_entries(&self) -> &[Vec<u8>] {
        &self.dict
    }

    /// 非 NULL 行的字典 ID 序列。
    pub fn ids(&self) -> &[u64] {
        &self.ids
    }

    /// NULL 行号序列。
    pub fn null_positions(&self) -> &[u64] {
        &self.nulls
    }

    /// 取第 `row` 行（段内从 0 起）：返回 `None` 表示行号越界；
    /// 返回的内层 `None` 表示 NULL，`Some(bytes)` 为字符串字节（空串即空切片）。
    pub fn row_bytes(&self, row: u64) -> Option<Option<&[u8]>> {
        if row >= self.row_count() {
            return None;
        }
        if self.nulls.binary_search(&row).is_ok() {
            return Some(None);
        }
        // nulls 严格升序：partition_point 即 row 之前的 NULL 个数。
        let nulls_before = self.nulls.partition_point(|&p| p < row);
        let id_index = row as usize - nulls_before;
        let id = self.ids[id_index] as usize;
        Some(Some(self.dict[id].as_slice()))
    }
}

/// 流式文件解码器。
pub struct FileReader<R: BufRead> {
    inner: R,
    limits: DecodeLimits,
    stats: ReadStats,
    /// 上一个段编号，用于单调递增校验。
    last_segment_no: Option<u32>,
    /// 当前段（逐行 API 的游标状态）。
    current: Option<SegmentData>,
    current_row: u64,
}

impl<R: BufRead> FileReader<R> {
    /// 读取并校验文件头，返回解码器。
    pub fn new(mut inner: R, limits: DecodeLimits) -> Result<Self> {
        let mut magic = [0u8; 4];
        read_exact(&mut inner, &mut magic)?;
        if &magic != crate::MAGIC {
            return Err(Error::BadMagic);
        }
        let mut vf = [0u8; 2];
        read_exact(&mut inner, &mut vf)?;
        if vf[0] != crate::FORMAT_VERSION {
            return Err(Error::UnsupportedVersion(vf[0]));
        }
        if vf[1] != crate::FLAGS {
            return Err(Error::BadFlags(vf[1]));
        }
        Ok(FileReader {
            inner,
            limits,
            stats: ReadStats::default(),
            last_segment_no: None,
            current: None,
            current_row: 0,
        })
    }

    /// 解码统计（按已实际读出的内容累计）。
    pub fn stats(&self) -> &ReadStats {
        &self.stats
    }

    /// 当前生效的解码限制（控制面用于值字节计费）。
    pub fn limits_ref(&self) -> &DecodeLimits {
        &self.limits
    }

    fn read_varint(&mut self) -> Result<u64> {
        let mut result: u64 = 0;
        let mut shift: u32 = 0;
        for i in 0..10u32 {
            let mut byte = [0u8; 1];
            read_exact(&mut self.inner, &mut byte)?;
            let b = byte[0];
            let payload = u64::from(b & 0x7f);
            if i == 9 && (b & 0x80 != 0 || payload > 1) {
                return Err(Error::BadVarint);
            }
            result |= payload
                .checked_mul(1u64.checked_shl(shift).ok_or(Error::BadVarint)?)
                .ok_or(Error::BadVarint)?;
            if b & 0x80 == 0 {
                return Ok(result);
            }
            shift += 7;
        }
        Err(Error::BadVarint)
    }

    /// 读出下一个段；文件结束返回 `Ok(None)`。
    ///
    /// 注意：返回值借用 `self`，下一次调用会使前一次的引用失效（标准流式模式）。
    /// 若此前用过 [`FileReader::next_row`]，段会被替换。
    pub fn next_segment(&mut self) -> Result<Option<&SegmentData>> {
        let seg = match self.pull_segment()? {
            Some(s) => s,
            None => return Ok(None),
        };
        self.current = Some(seg);
        self.current_row = 0;
        Ok(self.current.as_ref())
    }

    /// 实际读取并解析一个段帧，更新累计统计，但不改动逐行游标容器。
    fn pull_segment(&mut self) -> Result<Option<SegmentData>> {
        let mut kind = [0u8; 1];
        match self.inner.read_exact(&mut kind) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
            Err(e) => return Err(Error::Io(e.to_string())),
        }
        if kind[0] != SEGMENT_KIND {
            return Err(Error::BadSegmentKind(kind[0]));
        }
        let body_len = self.read_varint()?;
        if body_len > self.limits.max_segment_bytes {
            return Err(Error::SegmentTooLarge {
                size: body_len,
                max: self.limits.max_segment_bytes,
            });
        }

        // CRC 覆盖 kind 与 body_len 的原始字节，因此重建前缀。
        let mut crc_input = Vec::with_capacity(body_len as usize + 10);
        crc_input.push(SEGMENT_KIND);
        varint::write_u64(&mut crc_input, body_len);

        let body_start = crc_input.len();
        crc_input.resize(body_start + body_len as usize, 0u8);
        read_exact(&mut self.inner, &mut crc_input[body_start..])?;

        let mut crc_bytes = [0u8; 4];
        read_exact(&mut self.inner, &mut crc_bytes)?;
        let stored = u32::from_le_bytes(crc_bytes);

        let actual = crate::crc32::checksum(&crc_input);
        if actual != stored {
            return Err(Error::CrcMismatch {
                expected: stored,
                actual,
            });
        }

        let body = &crc_input[body_start..];
        let seg = parse_segment_body(body, &self.limits)?;

        if let Some(last) = self.last_segment_no {
            if seg.segment_no <= last {
                return Err(Error::BadSegmentOrder(seg.segment_no));
            }
        }
        self.last_segment_no = Some(seg.segment_no);

        self.stats.segments += 1;
        if self.stats.segments > self.limits.max_segments {
            return Err(Error::TooManySegments {
                limit: self.limits.max_segments,
                got: self.stats.segments,
            });
        }
        self.stats.rows = self.stats.rows.saturating_add(seg.row_count());
        if self.stats.rows > self.limits.max_rows {
            return Err(Error::RowsLimitExceeded {
                limit: self.limits.max_rows,
            });
        }
        self.stats.nulls = self.stats.nulls.saturating_add(seg.nulls.len() as u64);

        Ok(Some(seg))
    }

    /// 逐行读取：文件结束返回 `Ok(None)`。
    /// 输出长度在求值每个值时按 [`DecodeLimits::max_value_bytes`] 校验。
    pub fn next_row(&mut self) -> Result<Option<Row>> {
        loop {
            if let Some(seg) = &self.current {
                if self.current_row < seg.row_count() {
                    let row = self.current_row;
                    self.current_row += 1;
                    let value = match seg.row_bytes(row) {
                        Some(None) => None,
                        Some(Some(bytes)) => {
                            self.stats.value_bytes =
                                self.stats.value_bytes.saturating_add(bytes.len() as u64);
                            if self.stats.value_bytes > self.limits.max_value_bytes {
                                return Err(Error::ValueBytesLimitExceeded {
                                    limit: self.limits.max_value_bytes,
                                });
                            }
                            let s = std::str::from_utf8(bytes).map_err(|_| Error::BadUtf8)?;
                            Some(s.to_owned())
                        }
                        None => unreachable!("row bound checked above"),
                    };
                    return Ok(Some(Row {
                        segment_no: seg.segment_no,
                        index: row,
                        value,
                    }));
                }
            }
            self.current = match self.pull_segment()? {
                Some(s) => Some(s),
                None => return Ok(None),
            };
            self.current_row = 0;
        }
    }
}

/// 逐行读取返回的一行。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    /// 该行所属段的编号。
    pub segment_no: u32,
    /// 段内行号（从 0 起）。
    pub index: u64,
    /// `None` 为 NULL；`Some("")` 为空字符串。
    pub value: Option<String>,
}

fn read_exact<R: Read>(r: &mut R, buf: &mut [u8]) -> Result<()> {
    match r.read_exact(buf) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => Err(Error::UnexpectedEof {
            wanted: buf.len(),
            got: 0,
        }),
        Err(e) => Err(Error::Io(e.to_string())),
    }
}

/// 解析段 body（不含帧头与 CRC），全程做边界与一致性校验。
fn parse_segment_body(body: &[u8], limits: &DecodeLimits) -> Result<SegmentData> {
    let mut pos = 0usize;
    let segment_no = varint::read_u64(body, &mut pos)? as u32;
    let row_count = varint::read_u64(body, &mut pos)?;
    let cardinality = varint::read_u64(body, &mut pos)?;

    if cardinality > limits.max_cardinality as u64 {
        return Err(Error::CardinalityTooLarge {
            cardinality: cardinality as usize,
            max: limits.max_cardinality,
        });
    }

    let mut dict: Vec<Vec<u8>> = Vec::with_capacity(cardinality.min(1 << 20) as usize);
    let mut dict_bytes: usize = 0;
    for _ in 0..cardinality {
        let len = varint::read_u64(body, &mut pos)?;
        let end = pos
            .checked_add(len as usize)
            .ok_or(Error::BadSegment("length overflow"))?;
        if end > body.len() {
            return Err(Error::UnexpectedEof {
                wanted: len as usize,
                got: body.len() - pos,
            });
        }
        let entry = &body[pos..end];
        std::str::from_utf8(entry).map_err(|_| Error::BadUtf8)?;
        dict_bytes = dict_bytes
            .checked_add(entry.len())
            .ok_or(Error::BadSegment("dict byte count overflow"))?;
        if dict_bytes > limits.max_dict_bytes {
            return Err(Error::DictBytesLimitExceeded {
                limit: limits.max_dict_bytes,
            });
        }
        dict.push(entry.to_vec());
        pos = end;
    }

    let null_count = varint::read_u64(body, &mut pos)?;
    if null_count > row_count {
        return Err(Error::BadSegment("null count exceeds row count"));
    }
    let mut nulls = Vec::with_capacity(null_count.min(1 << 20) as usize);
    let mut last: Option<u64> = None;
    for _ in 0..null_count {
        let p = varint::read_u64(body, &mut pos)?;
        if p >= row_count {
            return Err(Error::BadSegment("null position out of range"));
        }
        if last.is_some_and(|x| x >= p) {
            return Err(Error::BadSegment("null positions not strictly ascending"));
        }
        last = Some(p);
        nulls.push(p);
    }

    let non_null = row_count - null_count;
    let mut ids = Vec::with_capacity(non_null.min(1 << 20) as usize);
    for _ in 0..non_null {
        let id = varint::read_u64(body, &mut pos)?;
        if id >= cardinality {
            return Err(Error::DictIdOutOfRange {
                id,
                len: dict.len(),
            });
        }
        ids.push(id);
    }

    if pos != body.len() {
        return Err(Error::BadSegment("trailing bytes after segment body"));
    }

    Ok(SegmentData {
        segment_no,
        dict,
        ids,
        nulls,
    })
}
