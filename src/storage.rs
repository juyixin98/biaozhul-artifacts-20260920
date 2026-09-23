//! File-backed time-series repository.
//!
//! # On-disk layout
//!
//! Per series, two files under the storage root:
//!
//! ```text
//! data/<series>.tsb   -- block file
//!   [0..8)   file header: magic "TSB1", version u16 LE (=1), reserved u16
//!   then a sequence of blocks:
//!     block header (48 bytes):
//!       count    u32 LE   number of points
//!       ts_start i64 LE   first timestamp
//!       ts_end   i64 LE   last timestamp
//!       val_min  i64 LE
//!       val_max  i64 LE
//!       ts_len   u32 LE   timestamp payload bytes
//!       val_len  u32 LE   value payload bytes
//!       crc32    u32 LE   CRC-32/IEEE over (ts_payload ++ val_payload)
//!     ts_payload  (ts_len bytes)   delta-of-delta, see codec
//!     val_payload (val_len bytes)  plain delta, see codec
//!
//! late/<series>.tsl   -- late/out-of-order log (only in Buffer mode)
//!   raw records, 16 bytes each: ts i64 LE, val i64 LE, append order
//! ```
//!
//! The block index is NOT persisted; it is rebuilt on open by scanning block
//! headers and verifying CRCs (see "Sync boundaries").
//!
//! # Sync boundaries
//!
//! * Points accepted by `append` live only in memory until `flush`.
//! * `flush` encodes pending points into one block, appends it to the data
//!   file, then calls `sync` (fsync) on the data file (and the late log if
//!   non-empty). After `flush` returns Ok, those points are durable.
//! * A crash may leave a torn tail (partial block / half-written header).
//!   On `open`, the file is scanned block by block; the first header that is
//!   incomplete, has impossible lengths, or fails CRC terminates the scan
//!   and the file is truncated to that offset. All blocks before it are
//!   valid and indexed. A late log whose length is not a multiple of 16 is
//!   truncated to a 16-byte boundary.
//! * After a `flush` error (injected or real I/O failure), in-memory state
//!   may diverge from disk; callers must drop the repo and `open` again
//!   (recovery path) before retrying.
//!
//! # Ordering semantics
//!
//! Timestamps within a series must be strictly increasing.
//! * `LateMode::Reject` (default): a point with ts <= last accepted ts is
//!   rejected (`duplicate` when ts == last, `out_of_order` when ts < last).
//! * `LateMode::Buffer`: such points are appended to the series' late log.
//!   At query time the late log is merged with the main blocks; on
//!   timestamp collision the point from the main (in-order) stream wins,
//!   and within the late log the earliest appended record wins
//!   (first-write-wins). Late points never modify flushed blocks.

use crate::codec::{self, BlockMeta};
use crate::io::FileIO;
use std::collections::HashMap;
use std::io;

pub const DEFAULT_BLOCK_SIZE: usize = 256;
pub const LATE_RECORD_LEN: usize = 16;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LateMode {
    Reject,
    Buffer,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rejected {
    pub ts: i64,
    pub val: i64,
    pub reason: &'static str, // "duplicate" | "out_of_order"
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct AppendOutcome {
    pub accepted: usize,
    pub buffered: usize,
    pub rejected: Vec<Rejected>,
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Stats {
    pub points: usize,      // flushed points
    pub pending: usize,     // in-memory, not yet durable
    pub blocks: usize,
    pub late_points: usize,
    pub raw_bytes: u64,     // 16 * (points + pending)
    pub file_bytes: u64,    // data file length on disk
    pub late_bytes: u64,
}

#[derive(Default)]
struct SeriesState {
    index: Vec<BlockMeta>,
    pending: Vec<(i64, i64)>,
    last_ts: Option<i64>,
    late_points: usize,
}

pub struct Repo<IO: FileIO> {
    io: IO,
    mode: LateMode,
    block_size: usize,
    series: HashMap<String, SeriesState>,
}

fn data_path(series: &str) -> String {
    format!("data/{}.tsb", series)
}

fn late_path(series: &str) -> String {
    format!("late/{}.tsl", series)
}

/// Series names are used as file names: restrict to a safe alphabet.
pub fn valid_series_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

impl<IO: FileIO> Repo<IO> {
    /// Opens (or creates) a repository over `io`, rebuilding all in-memory
    /// indexes from disk and truncating torn tails (see module docs).
    pub fn open(io: IO, mode: LateMode, block_size: usize) -> io::Result<Self> {
        let mut repo = Repo {
            io,
            mode,
            block_size: block_size.max(1),
            series: HashMap::new(),
        };
        let mut names: Vec<String> = Vec::new();
        for path in repo.io.list("data")? {
            if let Some(name) = path
                .strip_prefix("data/")
                .and_then(|n| n.strip_suffix(".tsb"))
            {
                names.push(name.to_string());
            }
        }
        for name in names {
            repo.recover_series(&name)?;
        }
        Ok(repo)
    }

    /// Scans one series' data file, verifies every block, truncates the
    /// first torn/corrupt tail, and rebuilds the in-memory index.
    fn recover_series(&mut self, name: &str) -> io::Result<()> {
        let data = self.io.read_all(&data_path(name))?;
        let mut state = SeriesState::default();
        if data.is_empty() {
            self.series.insert(name.to_string(), state);
            return Ok(());
        }
        if data.len() < codec::FILE_HEADER_LEN || &data[0..4] != codec::FILE_MAGIC {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("series {}: bad file header", name),
            ));
        }
        let mut pos = codec::FILE_HEADER_LEN;
        while pos < data.len() {
            let header_end = pos + codec::BLOCK_HEADER_LEN;
            let header = match data.get(pos..header_end).and_then(codec::parse_block_header) {
                Some(h) => h,
                None => break, // torn header
            };
            let block_len = codec::BLOCK_HEADER_LEN
                .saturating_add(header.ts_len as usize)
                .saturating_add(header.val_len as usize);
            let end = match pos.checked_add(block_len) {
                Some(e) if e <= data.len() => e,
                _ => break, // torn payload or absurd lengths
            };
            if header.count == 0 || codec::decode_block(&data[pos..end]).is_none() {
                break; // CRC failure / corrupt payload
            }
            state.index.push(BlockMeta {
                count: header.count,
                ts_start: header.ts_start,
                ts_end: header.ts_end,
                val_min: header.val_min,
                val_max: header.val_max,
                offset: pos as u64,
                len: block_len as u64,
            });
            pos = end;
        }
        if pos < data.len() {
            self.io.truncate(&data_path(name), pos as u64)?;
        }
        state.last_ts = state.index.last().map(|m| m.ts_end);

        // late log: length must be a multiple of the record size
        let late_len = self.io.len(&late_path(name))?;
        let good = late_len - (late_len % LATE_RECORD_LEN as u64);
        if good != late_len {
            self.io.truncate(&late_path(name), good)?;
        }
        state.late_points = (good / LATE_RECORD_LEN as u64) as usize;

        self.series.insert(name.to_string(), state);
        Ok(())
    }

    fn ensure_series(&mut self, name: &str) -> io::Result<()> {
        if !valid_series_name(name) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("invalid series name: {:?}", name),
            ));
        }
        if !self.series.contains_key(name) {
            if self.io.len(&data_path(name))? == 0 {
                self.io.append(&data_path(name), &codec::file_header())?;
            }
            self.series.insert(name.to_string(), SeriesState::default());
        }
        Ok(())
    }

    /// Appends points to a series. See module docs for ordering semantics.
    /// Points become durable only after `flush`.
    pub fn append(&mut self, name: &str, points: &[(i64, i64)]) -> io::Result<AppendOutcome> {
        self.ensure_series(name)?;
        let mut outcome = AppendOutcome::default();
        let mut late_records: Vec<u8> = Vec::new();
        {
            let state = self.series.get_mut(name).unwrap();
            let mut last_ts = state.last_ts;
            for &(ts, val) in points {
                match last_ts {
                    None => {
                        state.pending.push((ts, val));
                        last_ts = Some(ts);
                        outcome.accepted += 1;
                    }
                    Some(last) if ts > last => {
                        state.pending.push((ts, val));
                        last_ts = Some(ts);
                        outcome.accepted += 1;
                    }
                    Some(last) => {
                        let reason = if ts == last { "duplicate" } else { "out_of_order" };
                        match self.mode {
                            LateMode::Reject => {
                                outcome.rejected.push(Rejected { ts, val, reason });
                            }
                            LateMode::Buffer => {
                                late_records.extend_from_slice(&ts.to_le_bytes());
                                late_records.extend_from_slice(&val.to_le_bytes());
                                state.late_points += 1;
                                outcome.buffered += 1;
                            }
                        }
                    }
                }
            }
            state.last_ts = last_ts;
        }
        if !late_records.is_empty() {
            self.io.append(&late_path(name), &late_records)?;
        }
        Ok(outcome)
    }

    /// Encodes pending points into a block, appends it and fsyncs.
    /// After Ok, all accepted points (and buffered late points) are durable.
    pub fn flush(&mut self, name: &str) -> io::Result<()> {
        self.ensure_series(name)?;
        let pending = std::mem::take(&mut self.series.get_mut(name).unwrap().pending);
        if !pending.is_empty() {
            for chunk in pending.chunks(self.block_size) {
                let block = codec::encode_block(chunk);
                let h = codec::parse_block_header(&block).expect("own block header");
                let offset = self.io.append(&data_path(name), &block)?;
                self.series
                    .get_mut(name)
                    .unwrap()
                    .index
                    .push(BlockMeta {
                        count: h.count,
                        ts_start: h.ts_start,
                        ts_end: h.ts_end,
                        val_min: h.val_min,
                        val_max: h.val_max,
                        offset,
                        len: block.len() as u64,
                    });
            }
        }
        self.io.sync(&data_path(name))?;
        if self.series[name].late_points > 0 {
            self.io.sync(&late_path(name))?;
        }
        Ok(())
    }

    /// Range read, inclusive bounds: all points with `from <= ts <= to`.
    /// In Buffer mode, late-log points are merged in (main stream wins on
    /// timestamp collision).
    pub fn query(&self, name: &str, from: i64, to: i64) -> io::Result<Vec<(i64, i64)>> {
        if from > to {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "query range: from > to",
            ));
        }
        let state = match self.series.get(name) {
            Some(s) => s,
            None => return Ok(Vec::new()),
        };
        let mut out: Vec<(i64, i64)> = Vec::new();
        for meta in &state.index {
            if meta.ts_end < from || meta.ts_start > to {
                continue;
            }
            let raw = self
                .io
                .read_at(&data_path(name), meta.offset, meta.len as usize)?;
            let points = codec::decode_block(&raw).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "block failed verification")
            })?;
            out.extend(points.into_iter().filter(|p| p.0 >= from && p.0 <= to));
        }
        // pending (unflushed) points are visible to reads in this process
        out.extend(state.pending.iter().copied().filter(|p| p.0 >= from && p.0 <= to));

        if self.mode == LateMode::Buffer && state.late_points > 0 {
            let late = self.read_late(name)?;
            let mut late_in_range: Vec<(i64, i64)> =
                late.into_iter().filter(|p| p.0 >= from && p.0 <= to).collect();
            late_in_range.sort_by_key(|p| p.0);
            // first-write-wins within the late log
            late_in_range.dedup_by_key(|p| p.0);
            // main stream wins on collision
            let main_ts: std::collections::HashSet<i64> = out.iter().map(|p| p.0).collect();
            late_in_range.retain(|p| !main_ts.contains(&p.0));
            out.extend(late_in_range);
            out.sort_by_key(|p| p.0);
        }
        Ok(out)
    }

    fn read_late(&self, name: &str) -> io::Result<Vec<(i64, i64)>> {
        let raw = self.io.read_all(&late_path(name))?;
        let mut out = Vec::with_capacity(raw.len() / LATE_RECORD_LEN);
        for rec in raw.chunks_exact(LATE_RECORD_LEN) {
            let ts = i64::from_le_bytes(rec[0..8].try_into().unwrap());
            let val = i64::from_le_bytes(rec[8..16].try_into().unwrap());
            out.push((ts, val));
        }
        Ok(out)
    }

    pub fn stats(&self, name: &str) -> io::Result<Stats> {
        let mut s = Stats::default();
        if let Some(state) = self.series.get(name) {
            s.blocks = state.index.len();
            s.points = state.index.iter().map(|m| m.count as usize).sum();
            s.pending = state.pending.len();
            s.late_points = state.late_points;
            s.raw_bytes = 16 * (s.points + s.pending) as u64;
        }
        s.file_bytes = self.io.len(&data_path(name))?;
        s.late_bytes = self.io.len(&late_path(name))?;
        Ok(s)
    }

    /// Unwraps the repository and returns the underlying I/O (for tests).
    pub fn into_io(self) -> IO {
        self.io
    }

    /// Mutable access to the underlying I/O (for fault-injection tests).
    pub fn io_mut(&mut self) -> &mut IO {
        &mut self.io
    }
}
