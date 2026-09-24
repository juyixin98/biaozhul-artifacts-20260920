//! 分段追加日志:写入、fsync、段滚动与崩溃恢复。
//!
//! 恢复规则(打开日志时执行):
//! 1. 段号必须连续,缺段即数据丢失,拒绝打开;
//! 2. 中段(已封存段)必须完整:任何损坏(魔数错、长度越界、CRC 错、
//!    尾部不完整、空段)都拒绝打开,绝不静默跳过;
//! 3. 末段(活动段)允许且仅允许截去末尾的不完整记录(撕裂写);
//!    截断后 fsync 落盘。完整但 CRC 错误的尾记录同样拒绝打开;
//! 4. 全日志序号必须从 1 连续递增,出现空洞即拒绝打开,
//!    恢复后 `next_seq = 最大序号 + 1`,序号绝不复用。

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use crate::crash::{self, CrashPoint};
use crate::record;

pub const SEGMENT_EXT: &str = "seg";

#[derive(Debug)]
pub enum LogError {
    Io(io::Error),
    /// 中段损坏:拒绝打开。
    CorruptMiddleSegment { segment: u64, reason: String },
    /// 活动段头部/完整记录损坏(非可截断的不完整尾部):拒绝打开。
    CorruptActiveSegment { segment: u64, reason: String },
    /// 序号不连续。
    SeqGap { expected: u64, found: u64 },
    /// 段号不连续。
    SegmentGap { expected: u64, found: u64 },
    PayloadTooLarge { len: usize, max: usize },
    InvalidName(String),
    LogNotFound(String),
    RecordNotFound(u64),
    NonUtf8(u64),
}

impl fmt::Display for LogError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LogError::Io(e) => write!(f, "I/O 错误: {e}"),
            LogError::CorruptMiddleSegment { segment, reason } => {
                write!(f, "中段 {segment:016}.seg 损坏,拒绝打开: {reason}")
            }
            LogError::CorruptActiveSegment { segment, reason } => {
                write!(f, "活动段 {segment:016}.seg 损坏,拒绝打开: {reason}")
            }
            LogError::SeqGap { expected, found } => {
                write!(f, "序号不连续: 期望 {expected},实际 {found},拒绝打开")
            }
            LogError::SegmentGap { expected, found } => {
                write!(f, "段号不连续: 期望 {expected},实际 {found},拒绝打开")
            }
            LogError::PayloadTooLarge { len, max } => {
                write!(f, "payload 过大: {len} 字节,上限 {max} 字节")
            }
            LogError::InvalidName(n) => write!(f, "非法日志名: {n:?}"),
            LogError::LogNotFound(n) => write!(f, "日志不存在: {n}"),
            LogError::RecordNotFound(seq) => write!(f, "记录不存在: seq={seq}"),
            LogError::NonUtf8(seq) => write!(f, "记录 seq={seq} 不是合法 UTF-8"),
        }
    }
}

impl std::error::Error for LogError {}

impl From<io::Error> for LogError {
    fn from(e: io::Error) -> Self {
        LogError::Io(e)
    }
}

/// 一条已解析记录的元数据。
#[derive(Debug, Clone, Copy)]
pub struct RecordMeta {
    pub seq: u64,
    pub offset: u64,
    pub len: u32,
}

/// 段解析结果。
struct ParsedSegment {
    records: Vec<RecordMeta>,
    /// 有效数据长度(截断点)。
    valid_len: u64,
    /// 末尾是否存在不完整记录(撕裂写残留)。
    tail_incomplete: bool,
}

/// 解析段数据。完整但损坏(魔数/长度/CRC 错)返回 Err;
/// 仅末尾不完整时返回 Ok 且 tail_incomplete = true。
fn parse_segment(data: &[u8]) -> Result<ParsedSegment, String> {
    let mut records = Vec::new();
    let mut pos = 0usize;
    loop {
        let remaining = data.len() - pos;
        if remaining == 0 {
            return Ok(ParsedSegment { records, valid_len: pos as u64, tail_incomplete: false });
        }
        if remaining < record::HEADER_LEN {
            // 尾部连一个完整头部都凑不齐:撕裂写,可截断。
            return Ok(ParsedSegment { records, valid_len: pos as u64, tail_incomplete: true });
        }
        let h = &data[pos..pos + record::HEADER_LEN];
        let magic = u32::from_le_bytes(h[0..4].try_into().unwrap());
        if magic != record::MAGIC {
            return Err(format!("偏移 {pos}: 魔数错误 {magic:#010x}"));
        }
        let len = u32::from_le_bytes(h[4..8].try_into().unwrap()) as usize;
        if len > record::MAX_PAYLOAD {
            return Err(format!("偏移 {pos}: 长度字段 {len} 超过上限"));
        }
        let total = record::HEADER_LEN + len;
        if remaining < total {
            // 头部完整但记录体不完整:撕裂写,可截断。
            return Ok(ParsedSegment { records, valid_len: pos as u64, tail_incomplete: true });
        }
        let seq = u64::from_le_bytes(h[8..16].try_into().unwrap());
        let crc = u32::from_le_bytes(h[16..20].try_into().unwrap());
        let payload = &data[pos + record::HEADER_LEN..pos + total];
        if record::record_crc(seq, payload) != crc {
            return Err(format!("偏移 {pos}: seq={seq} CRC 校验失败"));
        }
        records.push(RecordMeta { seq, offset: pos as u64, len: len as u32 });
        pos += total;
    }
}

fn parse_segment_file(path: &Path) -> Result<ParsedSegment, LogError> {
    let data = fs::read(path)?;
    parse_segment(&data).map_err(|reason| LogError::CorruptActiveSegment {
        segment: 0, // 由调用方替换为真实段号
        reason,
    })
}

/// 校验一段记录的序号连续性并推进 next_seq。
fn check_seq(records: &[RecordMeta], next_seq: &mut u64) -> Result<(), LogError> {
    for r in records {
        if r.seq != *next_seq {
            return Err(LogError::SeqGap { expected: *next_seq, found: r.seq });
        }
        *next_seq += 1;
    }
    Ok(())
}

fn segment_path(dir: &Path, idx: u64) -> PathBuf {
    dir.join(format!("{idx:016}.{SEGMENT_EXT}"))
}

fn create_segment(dir: &Path, idx: u64) -> io::Result<File> {
    OpenOptions::new().create_new(true).append(true).open(segment_path(dir, idx))
}

/// fsync 目录,确保证录项(新建段文件)持久化。
fn sync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir)?.sync_all()
}

fn list_segment_indices(dir: &Path) -> Result<Vec<u64>, LogError> {
    let mut indices = Vec::new();
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if let Some(stem) = name.strip_suffix(&format!(".{SEGMENT_EXT}")) {
            if stem.len() == 16 {
                if let Ok(idx) = stem.parse::<u64>() {
                    indices.push(idx);
                }
            }
        }
    }
    indices.sort_unstable();
    Ok(indices)
}

/// 日志运行状态(供 HTTP 查询)。
#[derive(Debug, Clone)]
pub struct LogStatus {
    pub next_seq: u64,
    pub record_count: u64,
    pub segments: Vec<u64>,
}

struct LogInner {
    segments: Vec<u64>,
    active: File,
    active_size: u64,
    next_seq: u64,
}

/// 一个分段追加日志(对应数据目录下的一个子目录)。
pub struct Log {
    dir: PathBuf,
    max_segment_size: u64,
    inner: Mutex<LogInner>,
}

impl Log {
    /// 打开(必要时创建)一个日志,执行崩溃恢复。
    pub fn open(dir: &Path, max_segment_size: u64) -> Result<Log, LogError> {
        fs::create_dir_all(dir)?;
        let mut indices = list_segment_indices(dir)?;
        // 段号必须从 0 连续,缺段即数据丢失。
        for (i, &idx) in indices.iter().enumerate() {
            if idx != i as u64 {
                return Err(LogError::SegmentGap { expected: i as u64, found: idx });
            }
        }

        let mut next_seq = 1u64;
        if indices.is_empty() {
            let f = create_segment(dir, 0)?;
            f.sync_all()?;
            sync_dir(dir)?;
            indices.push(0);
        } else {
            let last = *indices.last().unwrap();
            // 中段:必须完全完整,任何异常都拒绝打开。
            for &idx in &indices[..indices.len() - 1] {
                let parsed = parse_segment_file(&segment_path(dir, idx)).map_err(|e| match e {
                    LogError::CorruptActiveSegment { reason, .. } => {
                        LogError::CorruptMiddleSegment { segment: idx, reason }
                    }
                    other => other,
                })?;
                if parsed.tail_incomplete {
                    return Err(LogError::CorruptMiddleSegment {
                        segment: idx,
                        reason: "封存段末尾存在不完整记录".into(),
                    });
                }
                if parsed.records.is_empty() {
                    return Err(LogError::CorruptMiddleSegment {
                        segment: idx,
                        reason: "空的封存段".into(),
                    });
                }
                check_seq(&parsed.records, &mut next_seq)?;
            }
            // 末段(活动段):仅允许截去不完整尾记录。
            let parsed = parse_segment_file(&segment_path(dir, last)).map_err(|e| match e {
                LogError::CorruptActiveSegment { reason, .. } => {
                    LogError::CorruptActiveSegment { segment: last, reason }
                }
                other => other,
            })?;
            check_seq(&parsed.records, &mut next_seq)?;
            if parsed.tail_incomplete {
                let path = segment_path(dir, last);
                let orig_len = fs::metadata(&path)?.len();
                let f = OpenOptions::new().write(true).open(&path)?;
                f.set_len(parsed.valid_len)?;
                f.sync_all()?;
                eprintln!(
                    "[seglog] 恢复: 段 {last:016} 截去末尾 {} 字节的不完整记录",
                    orig_len - parsed.valid_len
                );
            }
        }

        let active_idx = *indices.last().unwrap();
        let active = OpenOptions::new().append(true).open(segment_path(dir, active_idx))?;
        let active_size = active.metadata()?.len();
        Ok(Log {
            dir: dir.to_path_buf(),
            max_segment_size,
            inner: Mutex::new(LogInner { segments: indices, active, active_size, next_seq }),
        })
    }

    /// 追加一条记录。write → fsync → 内存提交,应答在提交之后。
    /// 返回 (seq, 所在段号)。
    pub fn append(&self, payload: &[u8]) -> Result<(u64, u64), LogError> {
        if payload.len() > record::MAX_PAYLOAD {
            return Err(LogError::PayloadTooLarge { len: payload.len(), max: record::MAX_PAYLOAD });
        }
        let mut inner = self.inner.lock().unwrap();
        let encoded = record::encode(inner.next_seq, payload);
        if inner.active_size > 0 && inner.active_size + encoded.len() as u64 > self.max_segment_size
        {
            self.roll(&mut inner)?;
        }
        inner.active.write_all(&encoded)?;
        crash::maybe_crash(CrashPoint::AfterWrite);
        inner.active.sync_data()?;
        crash::maybe_crash(CrashPoint::AfterSync);
        let seq = inner.next_seq;
        inner.next_seq += 1;
        inner.active_size += encoded.len() as u64;
        crash::maybe_crash(CrashPoint::AfterCommit);
        Ok((seq, *inner.segments.last().unwrap()))
    }

    /// 滚动到新的段:先 fsync 旧段,再创建新段并 fsync 目录。
    fn roll(&self, inner: &mut LogInner) -> Result<(), LogError> {
        inner.active.sync_data()?;
        let new_idx = *inner.segments.last().unwrap() + 1;
        let f = create_segment(&self.dir, new_idx)?;
        f.sync_all()?;
        sync_dir(&self.dir)?;
        inner.segments.push(new_idx);
        inner.active = f;
        inner.active_size = 0;
        Ok(())
    }

    /// 手动滚动(空活动段不滚动,避免产生空的封存段)。
    pub fn roll_manual(&self) -> Result<u64, LogError> {
        let mut inner = self.inner.lock().unwrap();
        if inner.active_size == 0 {
            return Ok(*inner.segments.last().unwrap());
        }
        self.roll(&mut inner)?;
        Ok(*inner.segments.last().unwrap())
    }

    /// 按序号读取一条记录(解析时重新校验 CRC)。
    pub fn get(&self, seq: u64) -> Result<Option<Vec<u8>>, LogError> {
        let inner = self.inner.lock().unwrap();
        if seq == 0 || seq >= inner.next_seq {
            return Ok(None);
        }
        for &idx in &inner.segments {
            let data = fs::read(segment_path(&self.dir, idx))?;
            let parsed = parse_segment(&data).map_err(|reason| LogError::CorruptMiddleSegment {
                segment: idx,
                reason,
            })?;
            if let Some(r) = parsed.records.iter().find(|r| r.seq == seq) {
                let start = r.offset as usize + record::HEADER_LEN;
                return Ok(Some(data[start..start + r.len as usize].to_vec()));
            }
        }
        Ok(None)
    }

    /// 列出全部记录的元数据。
    pub fn list(&self) -> Result<Vec<RecordMeta>, LogError> {
        let inner = self.inner.lock().unwrap();
        let mut out = Vec::new();
        for &idx in &inner.segments {
            let data = fs::read(segment_path(&self.dir, idx))?;
            let parsed = parse_segment(&data).map_err(|reason| LogError::CorruptMiddleSegment {
                segment: idx,
                reason,
            })?;
            out.extend(parsed.records);
        }
        Ok(out)
    }

    pub fn status(&self) -> LogStatus {
        let inner = self.inner.lock().unwrap();
        LogStatus {
            next_seq: inner.next_seq,
            record_count: inner.next_seq - 1,
            segments: inner.segments.clone(),
        }
    }
}
