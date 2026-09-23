//! 分段追加日志 (segmented append-only log)。
//!
//! # 磁盘布局
//!
//! 一个日志 = 一个目录, 内含若干只增段文件:
//!
//! ```text
//! <dir>/
//!   0000000000000000001.seg
//!   0000000000000000002.seg
//!   ...
//! ```
//!
//! 每个段是帧序列, 帧定长头 + 载荷:
//!
//! ```text
//! +-----------+-----------+-----------+-----------------+
//! | len : u32 | seq : u64 | crc : u32 | payload [len]B  |
//! +-----------+-----------+-----------+-----------------+
//!   大端序,  共 HEADER_LEN = 16 字节
//! ```
//!
//! * `len`  —— 载荷字节数
//! * `seq`  —— 全局单调递增序号, 从 1 开始; 恢复后继续递增, **永不复用**
//! * `crc`  —— CRC-32 (IEEE), 覆盖 `len || seq || payload`
//!
//! # 崩溃恢复规则
//!
//! 打开时按段号顺序校验全部段:
//!
//! * 帧头不完整(少于 16 字节)、或帧头完整但声明的载荷越过文件尾
//!   (半截写)——**只允许出现在最后一个段的末尾**, 此时把该不完整尾记录截去
//!   (截断并 `fsync` 文件与目录以持久化截断结果)。
//! * 已封口(非最后一个)段中出现上述任何异常, 或段号不连续/段缺失,
//!   一律返回 [`Error::Corrupt`] **拒绝打开**, 绝不静默跳过。
//! * 帧头与声明载荷都完整却 **CRC 不符**, 或 `len` 荒谬 —— 这是位损坏/篡改
//!   而非半截写, **无论出现在哪一段(含末段末尾)都拒绝打开**。
//! * 序号必须严格递增、绝不允许重复或回退 (复用)。序号允许**跳号**: 被认领
//!   但在数据落盘前崩溃的记录会留下空洞 (见 [`crate::marker`]), 这是
//!   "未确认记录允许丢失"与"序号不得复用"的必然结果。
//!
//! # 持久性
//!
//! [`SegmentLog::append`] 把整帧 `write_all` 后立即 `fsync`, fsync 成功才返回
//! 序号。因此:
//!
//! * 已确认 (返回 200) 的记录在崩溃/掉电后必然还在;
//! * 未确认 (写了一半或 fsync 未完成) 的记录可能完整存在、也可能只剩半截 —
//!   半截按上面的规则在末段截去, 完整且 CRC 正确的记录会被保留(序号自然连续)。

use std::fs::{self, File, OpenOptions};
use std::io::{self, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::crc32;
use crate::crash::CrashHook;
use crate::marker::Marker;

/// 帧头长度: len(u32) + seq(u64) + crc(u32)。
pub const HEADER_LEN: usize = 16;

/// 段文件名里序号的位数 (`%019u` 足够 u64 十进制位数)。
const SEG_DIGITS: usize = 19;

/// 单条记录载荷上限, 防止损坏帧头里的 len 造成荒谬的内存申请。
pub const MAX_PAYLOAD_LEN: u32 = 64 * 1024 * 1024;

/// 日志可能出现的错误。中段损坏一律是 [`Error::Corrupt`]。
#[derive(Debug)]
pub enum Error {
    /// I/O 错误。
    Io(io::Error),
    /// 中段损坏 / 结构不合法, 打开被拒绝。消息说明位置与原因。
    Corrupt(String),
    /// 载荷超过 [`MAX_PAYLOAD_LEN`]。
    PayloadTooLarge { len: usize, max: u32 },
    /// 序号已耗尽 (u64 溢出)。
    SequenceExhausted,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Corrupt(msg) => write!(f, "log corrupt, open refused: {msg}"),
            Error::PayloadTooLarge { len, max } => write!(
                f,
                "payload length {len} exceeds maximum {max}"
            ),
            Error::SequenceExhausted => write!(f, "sequence number exhausted"),
        }
    }
}

impl std::error::Error for Error {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Error::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}

/// 一条恢复出来的记录。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Record {
    pub seq: u64,
    pub payload: Vec<u8>,
}

fn seg_path(dir: &Path, index: u64) -> PathBuf {
    dir.join(format!("{index:0SEG_DIGITS$}.seg"))
}

/// 列出全部段文件并按段号排序。忽略以 `.` 开头的文件。
///
/// 非法文件名、段号重复、解析失败都视为目录结构损坏。
fn list_segments(dir: &Path) -> Result<Vec<(u64, PathBuf)>, Error> {
    let mut segs = Vec::new();
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with('.') {
            continue;
        }
        if name == "next.idx" {
            continue; // 序号 marker, 不是段文件
        }
        let Some(stem) = name.strip_suffix(".seg") else {
            return Err(Error::Corrupt(format!("unexpected file in log dir: {name}")));
        };
        if stem.len() != SEG_DIGITS || !stem.bytes().all(|b| b.is_ascii_digit()) {
            return Err(Error::Corrupt(format!("malformed segment file name: {name}")));
        }
        let index: u64 = stem
            .parse()
            .map_err(|_| Error::Corrupt(format!("segment index out of range: {name}")))?;
        if index == 0 {
            return Err(Error::Corrupt("segment index must start at 1".to_string()));
        }
        segs.push((index, entry.path()));
    }
    segs.sort_by_key(|(index, _)| *index);

    // 段号必须从 1 开始且严格连续 —— 段缺失绝不允许。
    for (position, (index, _)) in segs.iter().enumerate() {
        let expected = position as u64 + 1;
        if *index != expected {
            return Err(Error::Corrupt(format!(
                "segment gap/disorder: expected segment {expected:0SEG_DIGITS$}, found {index:0SEG_DIGITS$}"
            )));
        }
    }
    Ok(segs)
}

/// 一个已打开、可追加的分段日志。
///
/// 所有可变状态在一把 `Mutex` 后串行化; append 包含 fsync, 单实例并发安全。
pub struct SegmentLog {
    dir: PathBuf,
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for SegmentLog {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SegmentLog").field("dir", &self.dir).finish_non_exhaustive()
    }
}

struct Inner {
    /// 当前(最后一个)活动段文件, 句柄停在末尾。
    file: File,
    /// 当前活动段的段号。
    segment_index: u64,
    /// 当前段已写入字节数。
    segment_bytes: u64,
    /// 滚动阈值: 当前段超过该字节数后, 下一条记录写到新段。
    max_segment_bytes: u64,
    /// 下一条记录的序号 (= 已持久化认领的高水位)。
    next_seq: u64,
    /// 序号高水位 marker, 写数据前先认领序号。
    marker: Marker,
    /// 崩溃注入钩子 (仅测试用, 生产为无操作的空闭包)。
    hook: CrashHook,
    /// 已执行 append 次数, 供钩子按"第几次写"命中。
    append_ordinal: u64,
}

impl SegmentLog {
    /// 打开(不存在则创建)目录 `dir` 下的日志。
    ///
    /// `max_segment_bytes` 为段滚动阈值; 一条超大记录可使某段超过该值,
    /// 但不会跨段拆分(保证"记录不跨段", 恢复逻辑因此简单且严格)。
    pub fn open(dir: impl AsRef<Path>, max_segment_bytes: u64) -> Result<Self, Error> {
        let dir = dir.as_ref();
        fs::create_dir_all(dir)?;

        let segments = list_segments(dir)?;

        // 逐段校验。除最后一段外必须帧帧完整。
        let last = segments.len().checked_sub(1);
        let mut prev_seq: u64 = 0;
        for (pos, (index, path)) in segments.iter().enumerate() {
            let is_last = Some(pos) == last;
            prev_seq = scan_segment(path, *index, prev_seq, is_last)?;
        }
        let data_last_seq = prev_seq;
        let data_next_seq = prev_seq + 1;

        // 恢复序号高水位。marker 的 next_seq 必须 >= 数据里实际存在的最大序号 + 1:
        // 数据中每一条 seq 都必然已经被认领过; 反之 marker 领先数据是正常的
        // (认领后、数据落盘前崩溃, 该序号作废、不复用)。
        let (marker, marker_next_seq) = match Marker::open(dir) {
            Ok(Some(pair)) => pair,
            Ok(None) => {
                // marker 不存在: 数据必须为空, 否则说明 marker 丢失(损坏)。
                if !segments.is_empty() {
                    return Err(Error::Corrupt(
                        "next.idx missing while segment data exists".to_string(),
                    ));
                }
                let m = crate::marker::create(dir, 1)?;
                (m, 1)
            }
            // marker 文件存在但连一个合法槽都没有。只有数据为空时(首次创建
            // marker 的瞬间崩溃)才允许删掉重建; 有数据则宁可拒绝也不重置高水位。
            Err(e) if e.kind() == io::ErrorKind::InvalidData => {
                if !segments.is_empty() {
                    return Err(Error::Corrupt(format!(
                        "next.idx unreadable while segment data exists: {e}"
                    )));
                }
                let _ = fs::remove_file(dir.join("next.idx"));
                let m = crate::marker::create(dir, 1)?;
                (m, 1)
            }
            Err(e) => return Err(e.into()),
        };
        if marker_next_seq < data_next_seq {
            return Err(Error::Corrupt(format!(
                "next.idx high-water mark {marker_next_seq} behind data (need {data_next_seq})"
            )));
        }
        if marker_next_seq > data_next_seq {
            eprintln!(
                "[recovery] {gap} sequence number(s) claimed but never durably recorded; \
                 they will not be reused (last data seq {data_last_seq}, next seq {marker_next_seq})",
                gap = marker_next_seq - data_next_seq,
            );
        }

        // 打开/创建活动段。
        let (segment_index, file, segment_bytes) = match segments.last() {
            Some((index, path)) => {
                let mut file = OpenOptions::new().read(true).write(true).open(path)?;
                let len = file.seek(SeekFrom::End(0))?;
                (*index, file, len)
            }
            None => {
                let index = 1u64;
                let path = seg_path(dir, index);
                let file = OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create_new(true)
                    .open(&path)?;
                (index, file, 0u64)
            }
        };

        Ok(Self {
            dir: dir.to_path_buf(),
            inner: Mutex::new(Inner {
                file,
                segment_index,
                segment_bytes,
                max_segment_bytes: max_segment_bytes.max(1),
                next_seq: marker_next_seq,
                marker,
                hook: Arc::new(|_, _, _| {}),
                append_ordinal: 0,
            }),
        })
    }

    /// 安装崩溃注入钩子 (仅测试)。
    pub fn set_crash_hook(&self, hook: Option<CrashHook>) {
        let mut inner = self.inner.lock().unwrap();
        inner.hook = hook.unwrap_or_else(|| Arc::new(|_, _, _| {}));
    }

    /// 追加一条记录。流程 (每步都 fsync, 先序号后数据):
    ///
    /// 1. 取序号 `seq = next_seq`;
    /// 2. 把 `next_seq + 1` 写入 marker 并 fsync —— 序号此刻起被永久认领;
    /// 3. (必要时滚动段) 写入整帧并 fsync;
    /// 4. 返回 `seq`。
    ///
    /// 第 2 步后、第 3 步完成前崩溃: 该序号已认领但记录可能不存在, 恢复后
    /// 序号留出 (hole), 绝不复用; 客户端未收到确认, 允许记录存在或丢失。
    pub fn append(&self, payload: &[u8]) -> Result<u64, Error> {
        if payload.len() > MAX_PAYLOAD_LEN as usize {
            return Err(Error::PayloadTooLarge {
                len: payload.len(),
                max: MAX_PAYLOAD_LEN,
            });
        }
        let mut inner = self.inner.lock().expect("segment log mutex poisoned");

        inner.append_ordinal += 1;
        let ordinal = inner.append_ordinal;
        let seq = inner.next_seq;
        if seq == u64::MAX {
            return Err(Error::SequenceExhausted);
        }

        // 1) 先持久化认领序号。
        (inner.hook)(crate::crash::CrashPoint::BeforeMarker, ordinal, seq);
        inner.marker.commit(seq + 1, &self.dir, false)?;
        inner.next_seq = seq + 1;
        (inner.hook)(crate::crash::CrashPoint::AfterMarker, ordinal, seq);

        // 2) 段滚动: 非空段且已达阈值, 则下一条写入新段。
        if inner.segment_bytes > 0 && inner.segment_bytes >= inner.max_segment_bytes {
            (inner.hook)(crate::crash::CrashPoint::BeforeRoll, ordinal, seq);
            self.roll_segment(&mut inner)?;
            (inner.hook)(crate::crash::CrashPoint::AfterRoll, ordinal, seq);
        }

        // 3) 写数据帧并 fsync; 只有 fsync Ok 才算确认。
        let frame = encode_frame(seq, payload);
        (inner.hook)(crate::crash::CrashPoint::BeforeData, ordinal, seq);
        inner.file.write_all(&frame)?;
        inner.file.flush()?;
        inner.file.sync_data()?;
        (inner.hook)(crate::crash::CrashPoint::AfterData, ordinal, seq);

        inner.segment_bytes += frame.len() as u64;
        Ok(seq)
    }

    /// 滚动到下一个段: 先 fsync 旧段, 创建新段, 再 fsync 目录,
    /// 保证新文件名在崩溃后仍然存在。
    fn roll_segment(&self, inner: &mut Inner) -> Result<(), Error> {
        inner.file.sync_data()?;
        let new_index = inner.segment_index + 1;
        let path = seg_path(&self.dir, new_index);
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)?;
        inner.file = file;
        inner.segment_index = new_index;
        inner.segment_bytes = 0;
        fsync_dir(&self.dir)?;
        Ok(())
    }

    /// 顺序读出全部记录(用于调试 / 测试 / GET 接口)。
    pub fn read_all(&self) -> Result<Vec<Record>, Error> {
        let inner = self.inner.lock().expect("segment log mutex poisoned");
        let segments = list_segments(&self.dir)?;
        let mut out = Vec::new();
        for (pos, (index, path)) in segments.iter().enumerate() {
            let is_last = pos == segments.len() - 1;
            let prev_seq = out.last().map(|r: &Record| r.seq).unwrap_or(0);
            read_segment_into(path, *index, prev_seq, is_last, &mut out)?;
        }
        // marker 高水位必须不小于数据现状 (允许因未确认丢失而领先)。
        let data_last_seq = out.last().map(|r| r.seq).unwrap_or(0);
        assert!(
            inner.next_seq > data_last_seq,
            "next_seq {} not above data last seq {}",
            inner.next_seq,
            data_last_seq
        );
        Ok(out)
    }

    /// 已持久化认领的下一序号 (可能因未确认记录丢失而大于 数据最大序号 + 1)。
    pub fn next_seq(&self) -> u64 {
        self.inner.lock().unwrap().next_seq
    }

    /// 当前已确认落盘的最大序号 (没有记录时为 0)。
    #[allow(dead_code)] // 作为日志 API 保留; 单元测试与状态查看使用
    pub fn last_seq(&self) -> u64 {
        // 注意: 不能用 next_seq - 1, 中间可能有空洞。
        self.read_all()
            .map(|recs| recs.last().map(|r| r.seq).unwrap_or(0))
            .unwrap_or(0)
    }

    /// 当前段数(含活动段)。
    pub fn segment_count(&self) -> Result<u64, Error> {
        let _inner = self.inner.lock().unwrap();
        Ok(list_segments(&self.dir)?.len() as u64)
    }
}

/// 编码一帧。
fn encode_frame(seq: u64, payload: &[u8]) -> Vec<u8> {
    let len = payload.len() as u32;
    let mut frame = Vec::with_capacity(HEADER_LEN + payload.len());
    frame.extend_from_slice(&len.to_be_bytes());
    frame.extend_from_slice(&seq.to_be_bytes());
    // CRC 占位, 计算后回填。
    frame.extend_from_slice(&0u32.to_be_bytes());
    frame.extend_from_slice(payload);

    let mut crc = crc32::crc32(&frame[0..12]); // len || seq
    crc = crc32::feed(crc, payload);
    frame[12..16].copy_from_slice(&crc.to_be_bytes());
    frame
}

/// fsync 一个目录 (Linux 上用于持久化目录项的创建/截断)。
fn fsync_dir(dir: &Path) -> io::Result<()> {
    let f = File::open(dir)?;
    f.sync_all()
}

/// 扫描并校验一个段, 返回本段最后一条有效记录的序号 (无记录时原样返回
/// `prev_seq`, 初始为 0)。
///
/// `is_last` 为真时, 段尾允许存在一条不完整/损坏的记录, 该尾记录会被物理截断
/// (并 fsync 文件与目录); 其余任何损坏均报 [`Error::Corrupt`]。
///
/// 序号规则: 每条记录的 seq 必须 **严格大于** `prev_seq`; 重复或回退(序号复用)
/// 一律拒绝。seq 跳变(空洞)合法: 对应"认领序号后、数据落盘前崩溃"的丢失记录。
fn scan_segment(
    path: &Path,
    index: u64,
    mut prev_seq: u64,
    is_last: bool,
) -> Result<u64, Error> {
    let data = fs::read(path)?;
    let mut pos = 0usize;
    let mut valid_end = 0usize; // 最后一个完整有效帧之后的偏移

    while pos < data.len() {
        let remaining = data.len() - pos;

        // 帧头不完整。
        if remaining < HEADER_LEN {
            if is_last {
                truncate_tail(path, &data, valid_end, index, pos, "partial header")?;
                return Ok(prev_seq);
            }
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: truncated {remaining}-byte header at offset {pos}"
            )));
        }

        let len = u32::from_be_bytes(data[pos..pos + 4].try_into().unwrap());
        let seq = u64::from_be_bytes(data[pos + 4..pos + 12].try_into().unwrap());
        let stored_crc = u32::from_be_bytes(data[pos + 12..pos + 16].try_into().unwrap());

        if len > MAX_PAYLOAD_LEN {
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: implausible payload length {len} at offset {pos}"
            )));
        }

        let frame_end = match pos.checked_add(HEADER_LEN).and_then(|p| p.checked_add(len as usize)) {
            Some(end) if end <= data.len() => end,
            _ => {
                // 声明的载荷被截断。
                if is_last {
                    truncate_tail(
                        path,
                        &data,
                        valid_end,
                        index,
                        pos,
                        "payload shorter than declared length",
                    )?;
                    return Ok(prev_seq);
                }
                return Err(Error::Corrupt(format!(
                    "segment {index:0SEG_DIGITS$}: record at offset {pos} declares len {len} but segment ends"
                )));
            }
        };

        // 校验 CRC。
        let mut crc = crc32::crc32(&data[pos..pos + 12]);
        crc = crc32::feed(crc, &data[pos + HEADER_LEN..frame_end]);
        if crc != stored_crc {
            // 帧头与声明载荷都完整却 CRC 不符, 只可能是位损坏/篡改,
            // 不可能是自身半截写 (半截写必表现为结构性截断: 头不足或长度越界)。
            // 即使在末段末尾也拒绝打开; 截断会静默丢弃损坏记录。
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: crc mismatch at offset {pos} (seq field {seq})"
            )));
        }

        // 序号必须严格递增; 重复/回退 = 试图复用序号, 拒绝。
        if seq <= prev_seq {
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: seq {seq} at offset {pos} is not greater than previous {prev_seq}"
            )));
        }

        prev_seq = seq;
        pos = frame_end;
        valid_end = frame_end;
    }

    Ok(prev_seq)
}

/// 把最后一个段在 `valid_end` 处截断, 丢弃从 `bad_pos` 开始的不完整尾记录。
fn truncate_tail(
    path: &Path,
    data: &[u8],
    valid_end: usize,
    index: u64,
    bad_pos: usize,
    reason: &str,
) -> Result<(), Error> {
    // 不应截断任何完整帧: bad_pos 必须正好落在最后一个有效帧之后。
    assert_eq!(valid_end, bad_pos, "truncate_tail would drop valid records");
    if valid_end == data.len() {
        return Ok(()); // 无需截断
    }
    let mut f = OpenOptions::new().write(true).open(path)?;
    f.set_len(valid_end as u64)?;
    f.seek(SeekFrom::Start(valid_end as u64))?;
    f.sync_data()?;
    // 截断改变了文件内容(元数据/大小), fsync 目录以确保持久。
    if let Some(dir) = path.parent() {
        let _ = fsync_dir(dir);
    }
    eprintln!(
        "[recovery] segment {index:0SEG_DIGITS$}: discarded incomplete tail at offset {bad_pos} ({reason}), \
         kept {valid_end} bytes"
    );
    Ok(())
}

/// 读取一个段的全部记录到 `out`。复用扫描期的严格校验语义:
/// 序号必须严格大于 `prev_seq` (允许空洞, 不允许复用)。
fn read_segment_into(
    path: &Path,
    index: u64,
    mut prev_seq: u64,
    is_last: bool,
    out: &mut Vec<Record>,
) -> Result<(), Error> {
    let data = fs::read(path)?;
    let mut pos = 0usize;
    while pos < data.len() {
        let remaining = data.len() - pos;
        if remaining < HEADER_LEN {
            if is_last {
                break; // 打开时已截断, 理论上不会发生; 防御性处理
            }
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: truncated header at offset {pos}"
            )));
        }
        let len = u32::from_be_bytes(data[pos..pos + 4].try_into().unwrap());
        let seq = u64::from_be_bytes(data[pos + 4..pos + 12].try_into().unwrap());
        let stored_crc = u32::from_be_bytes(data[pos + 12..pos + 16].try_into().unwrap());
        if len > MAX_PAYLOAD_LEN {
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: implausible len {len} at offset {pos}"
            )));
        }
        let frame_end = pos + HEADER_LEN + len as usize;
        if frame_end > data.len() {
            if is_last {
                break;
            }
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: truncated payload at offset {pos}"
            )));
        }
        let mut crc = crc32::crc32(&data[pos..pos + 12]);
        crc = crc32::feed(crc, &data[pos + HEADER_LEN..frame_end]);
        if crc != stored_crc {
            // 完整帧 CRC 不符 = 数据损坏, 一律报错 (打开时 scan 已应拒绝)。
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: crc mismatch at offset {pos}"
            )));
        }
        if seq <= prev_seq {
            return Err(Error::Corrupt(format!(
                "segment {index:0SEG_DIGITS$}: seq {seq} not greater than previous {prev_seq}"
            )));
        }
        out.push(Record {
            seq,
            payload: data[pos + HEADER_LEN..frame_end].to_vec(),
        });
        prev_seq = seq;
        pos = frame_end;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// 单元测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn tempdir(tag: &str) -> PathBuf {
        let mut dir = std::env::temp_dir();
        let unique = format!(
            "seglog-{tag}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        dir.push(unique);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn append_and_reopen_keeps_records_and_advances_seq() {
        let dir = tempdir("basic");
        let log = SegmentLog::open(&dir, 1024).unwrap();
        for i in 1..=10u64 {
            let seq = log.append(format!("record-{i}").as_bytes()).unwrap();
            assert_eq!(seq, i);
        }
        drop(log);

        let log = SegmentLog::open(&dir, 1024).unwrap();
        let records = log.read_all().unwrap();
        assert_eq!(records.len(), 10);
        for (i, r) in records.iter().enumerate() {
            assert_eq!(r.seq, i as u64 + 1);
            assert_eq!(r.payload, format!("record-{}", i + 1).as_bytes());
        }
        // 恢复后序号不得复用: 新写入从 11 开始。
        assert_eq!(log.append(b"after-reopen").unwrap(), 11);
    }

    #[test]
    fn rolls_segments_and_recovers() {
        let dir = tempdir("roll");
        // 每段最多约 2~3 条记录。
        let log = SegmentLog::open(&dir, 50).unwrap();
        for i in 1..=12u64 {
            log.append(format!("payload-{i:02}").as_bytes()).unwrap();
        }
        let n_segments = log.segment_count().unwrap();
        assert!(n_segments >= 3, "expected multiple segments, got {n_segments}");
        drop(log);

        let log = SegmentLog::open(&dir, 50).unwrap();
        let records = log.read_all().unwrap();
        assert_eq!(records.len(), 12);
        let seqs: HashSet<u64> = records.iter().map(|r| r.seq).collect();
        assert_eq!(seqs, (1..=12).collect());
        assert_eq!(log.append(b"new").unwrap(), 13);
    }

    #[test]
    fn trailing_partial_header_is_truncated() {
        let dir = tempdir("partial-header");
        let log = SegmentLog::open(&dir, 1024).unwrap();
        log.append(b"aaa").unwrap();
        log.append(b"bbb").unwrap();
        drop(log);

        // 直接在段尾追加 7 个垃圾字节(不足 16 字节帧头)。
        let segs = list_segments(&dir).unwrap();
        let (_idx, path) = &segs[0];
        let mut f = OpenOptions::new().append(true).open(path).unwrap();
        f.write_all(b"garbage").unwrap();
        f.sync_data().unwrap();
        drop(f);

        let log = SegmentLog::open(&dir, 1024).unwrap();
        let records = log.read_all().unwrap();
        assert_eq!(records.len(), 2);
        assert_eq!(log.last_seq(), 2);
        // 半截尾记录被截去后, 新序号是 3 而非复用。
        assert_eq!(log.append(b"ccc").unwrap(), 3);
    }

    #[test]
    fn trailing_truncated_payload_is_truncated() {
        let dir = tempdir("partial-payload");
        {
            let log = SegmentLog::open(&dir, 1024).unwrap();
            log.append(b"keep-me").unwrap();
        }
        let segs = list_segments(&dir).unwrap();
        let (_idx, path) = &segs[0];
        // 手工拼一个声明 len=32 的帧头, 但只写 5 字节载荷。
        let mut bogus = Vec::new();
        bogus.extend_from_slice(&32u32.to_be_bytes());
        bogus.extend_from_slice(&2u64.to_be_bytes());
        bogus.extend_from_slice(&0xDEAD_BEEFu32.to_be_bytes());
        bogus.extend_from_slice(b"12345");
        let mut f = OpenOptions::new().append(true).open(path).unwrap();
        f.write_all(&bogus).unwrap();
        f.sync_data().unwrap();

        let log = SegmentLog::open(&dir, 1024).unwrap();
        let records = log.read_all().unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].payload, b"keep-me");
        assert_eq!(log.append(b"after").unwrap(), 2);
    }

    #[test]
    fn crc_corruption_in_middle_segment_is_rejected() {
        let dir = tempdir("mid-crc");
        {
            let log = SegmentLog::open(&dir, 40).unwrap();
            for i in 1..=9u64 {
                log.append(format!("p-{i}").as_bytes()).unwrap();
            }
        }
        let segs = list_segments(&dir).unwrap();
        assert!(segs.len() >= 2, "test needs >=2 segments, got {}", segs.len());

        // 翻转第一个(封口)段中第一条记录载荷的一个字节 (偏移 16+1)。
        let (_idx, first) = &segs[0];
        let mut data = fs::read(first).unwrap();
        data[16 + 1] ^= 0xFF;
        fs::write(first, &data).unwrap();

        let err = SegmentLog::open(&dir, 40).unwrap_err();
        match err {
            Error::Corrupt(msg) => {
                assert!(msg.contains("crc mismatch"), "unexpected message: {msg}");
            }
            other => panic!("expected Corrupt, got {other:?}"),
        }
    }

    #[test]
    fn truncated_middle_segment_is_rejected() {
        let dir = tempdir("mid-trunc");
        {
            let log = SegmentLog::open(&dir, 40).unwrap();
            for i in 1..=9u64 {
                log.append(format!("p-{i}").as_bytes()).unwrap();
            }
        }
        let segs = list_segments(&dir).unwrap();
        assert!(segs.len() >= 3, "test needs >=3 segments, got {}", segs.len());

        // 把中间段砍掉尾部若干字节 —— 封口段不完整必须拒绝打开。
        let (_idx, mid) = &segs[1];
        let data = fs::read(mid).unwrap();
        let cut = data.len() - 3; // 砍掉 3 字节, 造成尾部帧头/载荷残缺
        fs::write(mid, &data[..cut]).unwrap();

        let err = SegmentLog::open(&dir, 40).unwrap_err();
        assert!(matches!(err, Error::Corrupt(_)), "expected Corrupt");
    }

    #[test]
    fn missing_segment_is_rejected() {
        let dir = tempdir("gap");
        {
            let log = SegmentLog::open(&dir, 40).unwrap();
            for i in 1..=9u64 {
                log.append(format!("p-{i}").as_bytes()).unwrap();
            }
        }
        let segs = list_segments(&dir).unwrap();
        assert!(segs.len() >= 3);
        // 删除第一个段(段号将从 2 开始)。
        fs::remove_file(&segs[0].1).unwrap();
        let err = SegmentLog::open(&dir, 40).unwrap_err();
        assert!(matches!(err, Error::Corrupt(_)));
    }

    #[test]
    fn trailing_full_frame_with_crc_error_is_rejected() {
        // 帧头+声明载荷都完整, 但 CRC 不符, 且就在末段末尾: 也必须拒绝
        // (这是位损坏/篡改, 不是半截写; 截去会静默丢记录)。
        let dir = tempdir("tail-crc");
        {
            let log = SegmentLog::open(&dir, 1024).unwrap();
            log.append(b"keep-me").unwrap();
        }
        let frame = encode_frame(2, b"corrupt-but-full");
        let segs = list_segments(&dir).unwrap();
        let path = &segs[0].1;
        let mut f = OpenOptions::new().append(true).open(path).unwrap();
        f.write_all(&frame).unwrap();
        f.sync_data().unwrap();
        drop(f);
        // 翻转刚写入帧载荷的一个字节, 帧结构仍完整。
        let mut data = fs::read(path).unwrap();
        let flip = data.len() - 2;
        data[flip] ^= 0xFF;
        fs::write(path, &data).unwrap();

        let err = SegmentLog::open(&dir, 1024).unwrap_err();
        assert!(matches!(err, Error::Corrupt(_)), "expected Corrupt, got {err}");
    }

    #[test]
    fn seq_duplicate_is_rejected() {
        let dir = tempdir("seqdup");
        {
            let log = SegmentLog::open(&dir, 1024).unwrap();
            log.append(b"a").unwrap();
        }
        // 手工再追加一个 seq=1 的完整帧(序号复用/重复), 必须拒绝打开。
        let frame = encode_frame(1, b"b");
        let segs = list_segments(&dir).unwrap();
        let mut f = OpenOptions::new().append(true).open(&segs[0].1).unwrap();
        f.write_all(&frame).unwrap();
        f.sync_data().unwrap();
        drop(f);

        let err = SegmentLog::open(&dir, 1024).unwrap_err();
        match err {
            Error::Corrupt(msg) => assert!(msg.contains("not greater than previous"), "got: {msg}"),
            other => panic!("expected Corrupt, got {other:?}"),
        }
    }

    #[test]
    fn seq_hole_from_lost_claim_is_allowed_and_sequence_advances() {
        // 模拟: seq 1 落盘, seq 2 被认领(marker 高水位=3)但数据从未写入,
        // seq 3 落盘。重启必须接受空洞 [1,3], 且下一条拿到 4(不复用 2)。
        let dir = tempdir("seqhole");
        {
            // 先用正常路径写 seq1, 让 marker/段文件就位。
            let log = SegmentLog::open(&dir, 1024).unwrap();
            log.append(b"one").unwrap();
        }
        // 把 marker 高水位手动推到 4 (认领了 2,3), 再直接追加 seq=3 的帧。
        {
            let (mut m, _next) = Marker::open(&dir).unwrap().unwrap();
            m.commit(4, &dir, false).unwrap();
        }
        let frame = encode_frame(3, b"three");
        let segs = list_segments(&dir).unwrap();
        let mut f = OpenOptions::new().append(true).open(&segs[0].1).unwrap();
        f.write_all(&frame).unwrap();
        f.sync_data().unwrap();
        drop(f);

        let log = SegmentLog::open(&dir, 1024).unwrap();
        let records = log.read_all().unwrap();
        assert_eq!(records.iter().map(|r| r.seq).collect::<Vec<_>>(), vec![1, 3]);
        assert_eq!(log.next_seq(), 4);
        // seq 2 已作废, 新写入拿 4。
        assert_eq!(log.append(b"four").unwrap(), 4);
    }

    #[test]
    fn empty_log_starts_at_seq_one() {
        let dir = tempdir("empty");
        let log = SegmentLog::open(&dir, 1024).unwrap();
        assert_eq!(log.last_seq(), 0);
        assert_eq!(log.append(b"first").unwrap(), 1);
    }

    #[test]
    fn large_payload_roundtrip() {
        let dir = tempdir("large");
        let payload: Vec<u8> = (0..100_000u32).map(|i| (i % 251) as u8).collect();
        let log = SegmentLog::open(&dir, 4096).unwrap();
        let seq = log.append(&payload).unwrap();
        assert_eq!(seq, 1);
        drop(log);
        let log = SegmentLog::open(&dir, 4096).unwrap();
        let records = log.read_all().unwrap();
        assert_eq!(records[0].payload, payload);
    }
}
