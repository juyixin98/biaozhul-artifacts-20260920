//! File-backed block storage engine.
//!
//! # On-disk format
//!
//! One series = one file `<data-dir>/<name>.tsb`:
//!
//! ```text
//! offset  size  field
//! 0       4     magic  = b"TSBK"
//! 4       2     version (u16 LE) = 1
//! 6       2     flags (u16 LE); bit 0: unordered policy (0=reject, 1=buffer)
//! 8       4     block_size (u32 LE)
//! 12      8     reserved (8 zero bytes; future index pointer)
//! 20      ...   records
//! ```
//!
//! Each record:
//! ```text
//! payload_len : u32 LE
//! crc32       : u32 LE   (CRC-32/ISO-HDLC over payload only)
//! payload     : payload_len bytes, encoded by crate::coding
//! ```
//!
//! The block index is **not** stored separately: on open the file is scanned
//! record by record and an in-memory index (record offset, time range, count)
//! is rebuilt. A torn trailing record (short header, length overrunning the
//! file, or CRC mismatch — the signature of a crash or injected ENOSPC
//! mid-write) ends the scan and is truncated away. This is the durability
//! boundary:
//!
//! * a block is durable only after its record has been fully written **and**
//!   `fsync`ed;
//! * points still sitting in the in-memory active block are never durable;
//! * a failed `fsync` is reported to the client as an error even though bytes
//!   may be in the page cache — durability was not confirmed.
//!
//! # Ordering semantics
//!
//! A series is created with one of two policies for out-of-order points
//! (`ts < last accepted ts`; equal timestamps are legal duplicates):
//!
//! * `Reject` (default): the whole write batch fails, nothing is accepted.
//! * `Buffer`: late points go to a separate in-memory side buffer, are
//!   excluded from every range query, and are merged only by an explicit
//!   `drain`, which performs a minor compaction rewrite (see
//!   [`Store::drain`]): all flushed blocks, the active block and the side
//!   buffer are stable-sorted by timestamp and the series file is rewritten
//!   atomically. Buffered points are never durable until a drain succeeds.

use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::coding::{self, Point};
use crate::crc::Crc32;
use crate::io_layer::IoBackend;

pub const MAGIC: &[u8; 4] = b"TSBK";
pub const FORMAT_VERSION: u16 = 1;
pub const HEADER_LEN: u64 = 20;

const FLAG_BUFFER: u16 = 1;

/// What happens to points whose timestamp is older than the newest point
/// already accepted by the series.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnorderedPolicy {
    /// Reject the whole batch with [`StoreError::OutOfOrder`].
    Reject,
    /// Park late points in a side buffer; merge later with an explicit drain.
    Buffer,
}

/// Index entry for one flushed block.
#[derive(Debug, Clone, Copy)]
pub struct BlockMeta {
    /// Absolute offset of the record header (the length field).
    pub record_offset: u64,
    pub payload_len: u32,
    pub first_ts: i64,
    pub last_ts: i64,
    pub count: u32,
}

/// Result of opening a data directory.
#[derive(Debug, Default, Clone)]
pub struct RecoverReport {
    pub series_loaded: usize,
    pub blocks_loaded: usize,
    /// Number of trailing torn records truncated away.
    pub torn_records: usize,
}

#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    /// Directory is not a tsblock store (bad/absent magic).
    NotAStore,
    /// File header indicates a newer/unsupported format version.
    UnsupportedVersion(u16),
    /// Corruption detected away from the tail (CRC failure mid-file).
    Corruption {
        series: String,
        detail: String,
    },
    SeriesExists(String),
    UnknownSeries(String),
    BadSeriesName(String),
    /// At least one point was older than the series high-water mark.
    /// Carries the offending timestamp.
    OutOfOrder {
        ts: i64,
        last_ts: i64,
    },
    Codec(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "I/O error: {e}"),
            StoreError::NotAStore => write!(f, "not a tsblock data directory"),
            StoreError::UnsupportedVersion(v) => write!(f, "unsupported format version {v}"),
            StoreError::Corruption { series, detail } => {
                write!(f, "corruption in series {series}: {detail}")
            }
            StoreError::SeriesExists(s) => write!(f, "series {s:?} already exists"),
            StoreError::UnknownSeries(s) => write!(f, "unknown series {s:?}"),
            StoreError::BadSeriesName(s) => write!(f, "bad series name {s:?}"),
            StoreError::OutOfOrder { ts, last_ts } => write!(
                f,
                "out-of-order point ts={ts} is older than last accepted ts={last_ts}"
            ),
            StoreError::Codec(s) => write!(f, "codec error: {s}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        StoreError::Io(e)
    }
}

struct SeriesState {
    policy: UnorderedPolicy,
    block_size: u32,
    file: Box<dyn crate::io_layer::IoFile>,
    /// End of the valid region; next record is appended here.
    append_offset: u64,
    index: Vec<BlockMeta>,
    /// Ordered, not-yet-flushed points.
    active: Vec<Point>,
    /// High-water timestamp across flushed blocks and active.
    last_ts: Option<i64>,
    /// Side buffer for the Buffer policy (never queried).
    late_buffer: Vec<Point>,
    /// Compressed bytes (payload + 8-byte record headers) on disk.
    disk_bytes: u64,
    /// Points in flushed blocks.
    flushed_points: u64,
}

/// The storage engine. Cheaply cloneable (`Arc` inside); methods take `&self`.
#[derive(Clone)]
pub struct Store {
    inner: Arc<Mutex<StoreInner>>,
}

struct StoreInner {
    io: Arc<dyn IoBackend>,
    root: PathBuf,
    default_block_size: u32,
    series: BTreeMap<String, SeriesState>,
    recover: RecoverReport,
}

impl Store {
    /// Open (or create) a store rooted at `root`, using backend `io`.
    /// Existing `*.tsb` files are indexed and torn tails truncated.
    pub fn open(
        root: impl AsRef<Path>,
        io: Arc<dyn IoBackend>,
        default_block_size: u32,
    ) -> Result<Store, StoreError> {
        let root = root.as_ref().to_path_buf();
        std::fs::create_dir_all(&root).map_err(StoreError::Io)?;
        io.sync_dir(&root)?;

        let mut inner = StoreInner {
            io,
            root,
            default_block_size,
            series: BTreeMap::new(),
            recover: RecoverReport::default(),
        };
        inner.load_all()?;
        Ok(Store {
            inner: Arc::new(Mutex::new(inner)),
        })
    }

    pub fn recovery_report(&self) -> RecoverReport {
        self.inner.lock().unwrap().recover.clone()
    }

    pub fn create_series(
        &self,
        name: &str,
        policy: UnorderedPolicy,
        block_size: Option<u32>,
    ) -> Result<(), StoreError> {
        validate_name(name)?;
        let mut g = self.inner.lock().unwrap();
        if g.series.contains_key(name) {
            return Err(StoreError::SeriesExists(name.to_string()));
        }
        let bs = block_size.unwrap_or(g.default_block_size).max(1);
        let path = g.series_path(name);
        let mut file = g.io.create_new(&path)?;
        let flags = if matches!(policy, UnorderedPolicy::Buffer) {
            FLAG_BUFFER
        } else {
            0
        };
        let mut header = Vec::with_capacity(HEADER_LEN as usize);
        header.extend_from_slice(MAGIC);
        header.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        header.extend_from_slice(&flags.to_le_bytes());
        header.extend_from_slice(&bs.to_le_bytes());
        header.extend_from_slice(&0u64.to_le_bytes()); // reserved
        file.write_all_at(&header, 0)?;
        file.sync()?;
        g.io.sync_dir(&g.root)?;

        g.series.insert(
            name.to_string(),
            SeriesState {
                policy,
                block_size: bs,
                file,
                append_offset: HEADER_LEN,
                index: Vec::new(),
                active: Vec::with_capacity(bs as usize),
                last_ts: None,
                late_buffer: Vec::new(),
                disk_bytes: 0,
                flushed_points: 0,
            },
        );
        Ok(())
    }

    pub fn list_series(&self) -> Vec<String> {
        self.inner.lock().unwrap().series.keys().cloned().collect()
    }

    /// Append points in batch order. All points in the batch are validated
    /// against the ordering rule before any are accepted (all-or-nothing).
    /// Under `Buffer`, any late point is parked instead of failing the batch;
    /// in-order points are still accepted and may trigger a block flush.
    pub fn write(&self, name: &str, points: &[Point]) -> Result<WriteOutcome, StoreError> {
        let mut g = self.inner.lock().unwrap();
        let s = g
            .series
            .get_mut(name)
            .ok_or_else(|| StoreError::UnknownSeries(name.to_string()))?;

        // Validate: batch itself must be non-decreasing.
        let mut prev = s.last_ts;
        for p in points {
            if let Some(hw) = prev {
                if p.ts < hw {
                    match s.policy {
                        UnorderedPolicy::Reject => {
                            return Err(StoreError::OutOfOrder {
                                ts: p.ts,
                                last_ts: hw,
                            });
                        }
                        UnorderedPolicy::Buffer => {} // parked below
                    }
                }
            }
            // A buffered point doesn't advance the high-water mark.
            if prev.is_none_or(|hw| p.ts >= hw) {
                prev = Some(p.ts);
            }
        }

        let mut accepted = 0usize;
        let mut buffered = 0usize;
        let mut flushed = 0usize;
        let mut hw = s.last_ts;
        for p in points {
            let late = hw.is_some_and(|h| p.ts < h);
            if late {
                debug_assert!(matches!(s.policy, UnorderedPolicy::Buffer));
                s.late_buffer.push(*p);
                buffered += 1;
            } else {
                s.active.push(*p);
                hw = Some(p.ts);
                accepted += 1;
                if s.active.len() as u32 >= s.block_size {
                    s.flush_active()?;
                    flushed += 1;
                }
            }
        }
        s.last_ts = hw;
        Ok(WriteOutcome {
            accepted,
            buffered,
            blocks_flushed: flushed,
        })
    }

    /// Force the active points of one series (or all series) into a synced
    /// block. Returns the number of blocks written.
    pub fn flush(&self, name: Option<&str>) -> Result<usize, StoreError> {
        let mut g = self.inner.lock().unwrap();
        let mut count = 0;
        match name {
            Some(n) => {
                let s = g
                    .series
                    .get_mut(n)
                    .ok_or_else(|| StoreError::UnknownSeries(n.to_string()))?;
                if !s.active.is_empty() {
                    s.flush_active()?;
                    count += 1;
                }
            }
            None => {
                let names: Vec<String> = g.series.keys().cloned().collect();
                for n in names {
                    let s = g.series.get_mut(&n).unwrap();
                    if !s.active.is_empty() {
                        s.flush_active()?;
                        count += 1;
                    }
                }
            }
        }
        Ok(count)
    }

    /// Merge the side buffer into the ordered history.
    ///
    /// Because flushed blocks are immutable, a late point may be older than
    /// the start of the first block — it cannot be appended. `drain`
    /// therefore performs a **minor compaction rewrite**: every flushed
    /// block is read and CRC-checked, combined with the active block and the
    /// side buffer, stable-sorted by timestamp (points already on disk win
    /// ties against buffered arrivals), re-encoded into `block_size` chunks,
    /// written to a temporary file, fsync'd, atomically renamed over the
    /// series file, and followed by a directory fsync. On any I/O error the
    /// original file is left untouched. After a successful drain the side
    /// buffer is always empty and all its points are queryable.
    pub fn drain(&self, name: &str) -> Result<DrainOutcome, StoreError> {
        let mut g = self.inner.lock().unwrap();
        if !g.series.contains_key(name) {
            return Err(StoreError::UnknownSeries(name.to_string()));
        }
        g.compact_series(name)
    }

    pub fn buffered_count(&self, name: &str) -> Result<usize, StoreError> {
        let g = self.inner.lock().unwrap();
        let s = g
            .series
            .get(name)
            .ok_or_else(|| StoreError::UnknownSeries(name.to_string()))?;
        Ok(s.late_buffer.len())
    }

    /// Range query `[start, end]` (inclusive), in storage order. Only flushed
    /// blocks whose indexed time range overlaps the window are read from disk
    /// (binary search on the block index), then filtered; the in-memory
    /// active block is merged afterwards. Side-buffered points are excluded by
    /// definition.
    pub fn query(&self, name: &str, start: i64, end: i64) -> Result<QueryReport, StoreError> {
        let g = self.inner.lock().unwrap();
        let s = g
            .series
            .get(name)
            .ok_or_else(|| StoreError::UnknownSeries(name.to_string()))?;

        // First block whose last_ts >= start.
        let first = s
            .index
            .partition_point(|m| m.last_ts < start)
            .min(s.index.len());

        let mut points = Vec::new();
        let mut blocks_scanned = 0usize;
        let mut blocks_with_hits = 0usize;
        let mut bytes_read = 0u64;

        for meta in &s.index[first..] {
            if meta.first_ts > end {
                break; // later blocks start even later
            }
            blocks_scanned += 1;
            let mut header = [0u8; 8];
            read_exact_at(&*s.file, &mut header, meta.record_offset)?;
            let plen = u32::from_le_bytes(header[0..4].try_into().unwrap()) as usize;
            let crc_stored = u32::from_le_bytes(header[4..8].try_into().unwrap());
            if plen != meta.payload_len as usize {
                return Err(StoreError::Corruption {
                    series: name.to_string(),
                    detail: format!(
                        "index says payload {} bytes but record says {plen}",
                        meta.payload_len
                    ),
                });
            }
            let mut payload = vec![0u8; plen];
            read_exact_at(&*s.file, &mut payload, meta.record_offset + 8)?;
            bytes_read += 8 + plen as u64;
            if Crc32::checksum(&payload) != crc_stored {
                return Err(StoreError::Corruption {
                    series: name.to_string(),
                    detail: format!("CRC mismatch in block at offset {}", meta.record_offset),
                });
            }
            let decoded = coding::decode_block(&payload).map_err(|e| StoreError::Corruption {
                series: name.to_string(),
                detail: e.to_string(),
            })?;
            let before = points.len();
            points.extend(decoded.into_iter().filter(|p| p.ts >= start && p.ts <= end));
            if points.len() > before {
                blocks_with_hits += 1;
            }
        }

        // Merge the active block (already in storage order).
        points.extend(
            s.active
                .iter()
                .copied()
                .filter(|p| p.ts >= start && p.ts <= end),
        );

        Ok(QueryReport {
            points,
            blocks_scanned,
            blocks_with_hits,
            bytes_read,
        })
    }

    /// Real compression statistics versus an uncompressed 16-byte-per-point
    /// array (i64 ts + i64 value).
    pub fn stats(&self, name: &str) -> Result<SeriesStats, StoreError> {
        let g = self.inner.lock().unwrap();
        let s = g
            .series
            .get(name)
            .ok_or_else(|| StoreError::UnknownSeries(name.to_string()))?;
        let flushed_points = s.flushed_points;
        let active_points = s.active.len() as u64;
        let buffered = s.late_buffer.len() as u64;
        let uncompressed = (flushed_points + active_points) * 16;
        let ratio = if s.disk_bytes == 0 {
            None
        } else {
            Some(uncompressed as f64 / s.disk_bytes as f64)
        };
        Ok(SeriesStats {
            policy: s.policy,
            block_size: s.block_size,
            blocks: s.index.len() as u64,
            flushed_points,
            active_points,
            buffered_points: buffered,
            disk_bytes: s.disk_bytes,
            uncompressed_bytes: uncompressed,
            compression_ratio: ratio,
        })
    }
}

fn read_exact_at(
    file: &dyn crate::io_layer::IoFile,
    buf: &mut [u8],
    offset: u64,
) -> Result<(), StoreError> {
    let mut done = 0;
    while done < buf.len() {
        let n = file.read_at(&mut buf[done..], offset + done as u64)?;
        if n == 0 {
            return Err(StoreError::Io(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "short read from series file",
            )));
        }
        done += n;
    }
    Ok(())
}

#[derive(Debug, Clone, Copy)]
pub struct WriteOutcome {
    pub accepted: usize,
    pub buffered: usize,
    pub blocks_flushed: usize,
}

#[derive(Debug, Clone, Copy)]
pub struct DrainOutcome {
    /// Side-buffer points merged into history.
    pub merged: usize,
    /// Blocks present in the rewritten file.
    pub blocks_rewritten: usize,
    /// Always 0 after a successful drain.
    pub buffered_remaining: usize,
}

#[derive(Debug)]
pub struct QueryReport {
    pub points: Vec<Point>,
    pub blocks_scanned: usize,
    pub blocks_with_hits: usize,
    pub bytes_read: u64,
}

#[derive(Debug)]
pub struct SeriesStats {
    pub policy: UnorderedPolicy,
    pub block_size: u32,
    pub blocks: u64,
    pub flushed_points: u64,
    pub active_points: u64,
    pub buffered_points: u64,
    pub disk_bytes: u64,
    pub uncompressed_bytes: u64,
    pub compression_ratio: Option<f64>,
}

impl StoreInner {
    fn series_path(&self, name: &str) -> PathBuf {
        self.root.join(format!("{name}.tsb"))
    }

    fn load_all(&mut self) -> Result<(), StoreError> {
        let mut entries: Vec<_> = std::fs::read_dir(&self.root)
            .map_err(StoreError::Io)?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "tsb"))
            .collect();
        entries.sort();

        for path in entries {
            let name = path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or_default()
                .to_string();
            self.load_one(&path, &name)?;
            self.recover.series_loaded += 1;
        }
        Ok(())
    }

    fn load_one(&mut self, path: &Path, name: &str) -> Result<(), StoreError> {
        let mut file = self.io.open_write(path)?;
        let file_len = file.len()?;
        if file_len < HEADER_LEN {
            return Err(StoreError::Corruption {
                series: name.to_string(),
                detail: "file shorter than header".to_string(),
            });
        }
        let mut header = [0u8; HEADER_LEN as usize];
        read_exact_at(&*file, &mut header, 0)?;
        if &header[0..4] != MAGIC {
            return Err(StoreError::NotAStore);
        }
        let version = u16::from_le_bytes(header[4..6].try_into().unwrap());
        if version != FORMAT_VERSION {
            return Err(StoreError::UnsupportedVersion(version));
        }
        let flags = u16::from_le_bytes(header[6..8].try_into().unwrap());
        let block_size = u32::from_le_bytes(header[8..12].try_into().unwrap()).max(1);
        let policy = if flags & FLAG_BUFFER != 0 {
            UnorderedPolicy::Buffer
        } else {
            UnorderedPolicy::Reject
        };

        // Scan records.
        let mut index = Vec::new();
        let mut offset = HEADER_LEN;
        let mut disk_bytes = 0u64;
        let mut flushed_points = 0u64;
        let mut torn = 0usize;

        loop {
            if offset == file_len {
                break;
            }
            if offset + 8 > file_len {
                torn += 1;
                break;
            }
            let mut rec_hdr = [0u8; 8];
            read_exact_at(&*file, &mut rec_hdr, offset)?;
            let plen = u32::from_le_bytes(rec_hdr[0..4].try_into().unwrap()) as u64;
            let crc_stored = u32::from_le_bytes(rec_hdr[4..8].try_into().unwrap());
            if offset + 8 + plen > file_len {
                torn += 1;
                break;
            }
            let mut payload = vec![0u8; plen as usize];
            read_exact_at(&*file, &mut payload, offset + 8)?;
            if Crc32::checksum(&payload) != crc_stored {
                torn += 1;
                break;
            }
            let decoded = match coding::decode_block(&payload) {
                Ok(d) => d,
                Err(_) => {
                    torn += 1;
                    break;
                }
            };
            if decoded.is_empty() {
                torn += 1; // empty blocks are never written; treat as corrupt
                break;
            }
            index.push(BlockMeta {
                record_offset: offset,
                payload_len: plen as u32,
                first_ts: decoded.first().unwrap().ts,
                last_ts: decoded.last().unwrap().ts,
                count: decoded.len() as u32,
            });
            disk_bytes += 8 + plen;
            flushed_points += decoded.len() as u64;
            offset += 8 + plen;
        }

        let valid_end = offset;
        if valid_end != file_len {
            // Drop the torn tail so future appends cannot join onto garbage.
            file.set_len(valid_end)?;
            file.sync()?;
            self.recover.torn_records += torn;
        }

        let last_ts = index.last().map(|m| m.last_ts);
        self.recover.blocks_loaded += index.len();

        self.series.insert(
            name.to_string(),
            SeriesState {
                policy,
                block_size,
                file,
                append_offset: valid_end,
                index,
                active: Vec::new(),
                last_ts,
                late_buffer: Vec::new(),
                disk_bytes,
                flushed_points,
            },
        );
        Ok(())
    }

    /// Rewrite one series file after merging its side buffer. The series is
    /// removed from the map first so the borrow checker is satisfied; it is
    /// re-inserted on every path (the method never leaks a series out of the
    /// map).
    fn compact_series(&mut self, name: &str) -> Result<DrainOutcome, StoreError> {
        let mut s = self
            .series
            .remove(name)
            .expect("existence checked by caller");

        let merged = s.late_buffer.len();
        if merged == 0 {
            let outcome = DrainOutcome {
                merged: 0,
                blocks_rewritten: s.index.len(),
                buffered_remaining: 0,
            };
            self.series.insert(name.to_string(), s);
            return Ok(outcome);
        }

        // Gather every point: flushed history, active block, side buffer.
        let mut all: Vec<Point> =
            Vec::with_capacity(s.flushed_points as usize + s.active.len() + s.late_buffer.len());
        for meta in &s.index {
            let mut header = [0u8; 8];
            read_exact_at(&*s.file, &mut header, meta.record_offset)?;
            let mut payload = vec![0u8; meta.payload_len as usize];
            read_exact_at(&*s.file, &mut payload, meta.record_offset + 8)?;
            let crc_stored = u32::from_le_bytes(header[4..8].try_into().unwrap());
            if Crc32::checksum(&payload) != crc_stored {
                let bad_offset = meta.record_offset;
                self.series.insert(name.to_string(), s);
                return Err(StoreError::Corruption {
                    series: name.to_string(),
                    detail: format!("CRC mismatch at offset {bad_offset}"),
                });
            }
            all.extend(
                coding::decode_block(&payload).map_err(|e| StoreError::Codec(e.to_string()))?,
            );
        }
        all.extend(s.active.iter().copied());
        all.extend(s.late_buffer.iter().copied());
        all.sort_by_key(|p| p.ts); // stable: history wins ties

        let block_size = s.block_size;
        let flags = if matches!(s.policy, UnorderedPolicy::Buffer) {
            FLAG_BUFFER
        } else {
            0
        };

        // Write the new file from scratch.
        let tmp_name = format!(".{name}.compact.tmp");
        let tmp_path = self.root.join(&tmp_name);
        // Remove a stale temp file left by an earlier crashed compaction.
        let _ = std::fs::remove_file(&tmp_path);

        let result = (|| -> Result<DrainOutcome, StoreError> {
            let mut tmp = self.io.create_new(&tmp_path)?;
            let mut header = Vec::with_capacity(HEADER_LEN as usize);
            header.extend_from_slice(MAGIC);
            header.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
            header.extend_from_slice(&flags.to_le_bytes());
            header.extend_from_slice(&block_size.to_le_bytes());
            header.extend_from_slice(&0u64.to_le_bytes());
            write_all_at(&mut *tmp, &header, 0)?;

            let mut offset = HEADER_LEN;
            let mut new_index = Vec::new();
            let mut disk_bytes = 0u64;
            for chunk in all.chunks(block_size.max(1) as usize) {
                let payload = coding::encode_block(chunk);
                let crc = Crc32::checksum(&payload);
                let mut record = Vec::with_capacity(8 + payload.len());
                record.extend_from_slice(&(payload.len() as u32).to_le_bytes());
                record.extend_from_slice(&crc.to_le_bytes());
                record.extend_from_slice(&payload);
                write_all_at(&mut *tmp, &record, offset)?;
                new_index.push(BlockMeta {
                    record_offset: offset,
                    payload_len: payload.len() as u32,
                    first_ts: chunk.first().unwrap().ts,
                    last_ts: chunk.last().unwrap().ts,
                    count: chunk.len() as u32,
                });
                offset += record.len() as u64;
                disk_bytes += record.len() as u64;
            }
            tmp.sync()?;
            drop(tmp);

            // Atomic replace, then persist the directory entry change.
            let final_path = self.series_path(name);
            self.io.rename(&tmp_path, &final_path)?;
            self.io.sync_dir(&self.root)?;

            let blocks_rewritten = new_index.len();
            let total = all.len() as u64;
            s.file = self.io.open_write(&final_path)?;
            s.index = new_index;
            s.active = Vec::new();
            s.late_buffer = Vec::new();
            s.append_offset = offset;
            s.last_ts = all.last().map(|p| p.ts);
            s.disk_bytes = disk_bytes;
            s.flushed_points = total;

            Ok(DrainOutcome {
                merged,
                blocks_rewritten,
                buffered_remaining: 0,
            })
        })();

        match result {
            Ok(outcome) => {
                self.series.insert(name.to_string(), s);
                Ok(outcome)
            }
            Err(e) => {
                // Original file is untouched; restore state and clean up.
                let _ = std::fs::remove_file(&tmp_path);
                self.series.insert(name.to_string(), s);
                Err(e)
            }
        }
    }
}

/// Write a full buffer at a positional offset through an [`IoFile`].
fn write_all_at(
    file: &mut dyn crate::io_layer::IoFile,
    buf: &[u8],
    offset: u64,
) -> Result<(), StoreError> {
    file.write_all_at(buf, offset).map_err(StoreError::Io)
}

impl SeriesState {
    /// Encode the active points, append a CRC-framed record, fsync, update
    /// the in-memory index.
    fn flush_active(&mut self) -> Result<(), StoreError> {
        if self.active.is_empty() {
            return Ok(());
        }
        let points = std::mem::take(&mut self.active);
        let payload = coding::encode_block(&points);
        let crc = Crc32::checksum(&payload);

        let mut record = Vec::with_capacity(8 + payload.len());
        record.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        record.extend_from_slice(&crc.to_le_bytes());
        record.extend_from_slice(&payload);

        let offset = self.append_offset;
        // Write the record then fsync. If either fails, the record may be
        // partial on disk; recovery CRC-checks and truncates it on the next
        // open. The in-memory index is only updated after a confirmed sync,
        // and the points are restored to the active block so they can be
        // retried.
        if let Err(e) = self.file.write_all_at(&record, offset) {
            self.active = points;
            return Err(StoreError::Io(e));
        }
        if let Err(e) = self.file.sync() {
            self.active = points;
            return Err(StoreError::Io(e));
        }

        self.index.push(BlockMeta {
            record_offset: offset,
            payload_len: payload.len() as u32,
            first_ts: points.first().unwrap().ts,
            last_ts: points.last().unwrap().ts,
            count: points.len() as u32,
        });
        self.append_offset += record.len() as u64;
        self.disk_bytes += record.len() as u64;
        self.flushed_points += points.len() as u64;
        Ok(())
    }
}

/// Names: 1..=64 chars, ASCII letters/digits/`_`/`-`/`.`, must not start with
/// `.` or `-`, and must not be `.` / `..`. This maps 1:1 to file names and
/// rejects path traversal.
pub fn validate_name(name: &str) -> Result<(), StoreError> {
    let ok = !name.is_empty()
        && name.len() <= 64
        && name
            .as_bytes()
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'_' || *b == b'-' || *b == b'.')
        && !name.starts_with(['.', '-']);
    if ok {
        Ok(())
    } else {
        Err(StoreError::BadSeriesName(name.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::io_layer::RealIo;

    fn tempdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!(
            "tsblock-test-{}-{}-{}",
            tag,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn open_store(dir: &Path, bs: u32) -> Store {
        Store::open(dir, Arc::new(RealIo::new()), bs).unwrap()
    }

    #[test]
    fn name_validation_rules() {
        assert!(validate_name("cpu_0.load").is_ok());
        assert!(validate_name("a-b_c").is_ok());
        assert!(validate_name("").is_err());
        assert!(validate_name("../etc").is_err());
        assert!(validate_name("a/b").is_err());
        assert!(validate_name("-dash").is_err());
        assert!(validate_name(".hidden").is_err());
    }

    #[test]
    fn write_query_flush_reopen() {
        let dir = tempdir("persist");
        let store = open_store(&dir, 4);
        store
            .create_series("s", UnorderedPolicy::Reject, None)
            .unwrap();
        let pts: Vec<_> = (0..10).map(|i| Point::new(i, i * 2)).collect();
        store.write("s", &pts).unwrap();
        // 10 points, block size 4 -> 2 flushed blocks + 2 active.
        let r = store.query("s", 2, 7).unwrap();
        assert_eq!(r.points, pts[2..8].to_vec());

        store.flush(None).unwrap();
        drop(store);

        // Reopen: index rebuilt from disk, torn-tail scan runs.
        let store2 = open_store(&dir, 4);
        let rep = store2.recovery_report();
        assert_eq!(rep.series_loaded, 1);
        assert_eq!(rep.blocks_loaded, 3);
        let r = store2.query("s", 2, 7).unwrap();
        assert_eq!(r.points, pts[2..8].to_vec());
    }

    #[test]
    fn reject_policy_blocks_whole_batch() {
        let dir = tempdir("reject");
        let store = open_store(&dir, 100);
        store
            .create_series("s", UnorderedPolicy::Reject, None)
            .unwrap();
        store
            .write("s", &[Point::new(1, 1), Point::new(2, 2)])
            .unwrap();
        let err = store
            .write("s", &[Point::new(3, 3), Point::new(0, 0)])
            .unwrap_err();
        assert!(matches!(err, StoreError::OutOfOrder { ts: 0, last_ts: 3 }));
        // Neither point of the failed batch landed.
        assert_eq!(
            store.query("s", i64::MIN, i64::MAX).unwrap().points.len(),
            2
        );
    }

    #[test]
    fn buffer_policy_parks_late_points_until_drain() {
        let dir = tempdir("buffer");
        let store = open_store(&dir, 100);
        store
            .create_series("s", UnorderedPolicy::Buffer, None)
            .unwrap();
        store
            .write("s", &[Point::new(10, 1), Point::new(20, 2)])
            .unwrap();
        let out = store
            .write(
                "s",
                &[Point::new(15, 9), Point::new(30, 3), Point::new(5, 0)],
            )
            .unwrap();
        assert_eq!(out.accepted, 1);
        assert_eq!(out.buffered, 2);

        // Buffered points are invisible to queries.
        let q = store.query("s", i64::MIN, i64::MAX).unwrap();
        assert_eq!(
            q.points,
            vec![Point::new(10, 1), Point::new(20, 2), Point::new(30, 3)]
        );
        assert_eq!(store.buffered_count("s").unwrap(), 2);

        // Drain merges in timestamp order; nothing is still late here.
        let d = store.drain("s").unwrap();
        assert_eq!(d.merged, 2);
        let q = store.query("s", i64::MIN, i64::MAX).unwrap();
        assert_eq!(
            q.points,
            vec![
                Point::new(5, 0),
                Point::new(10, 1),
                Point::new(15, 9),
                Point::new(20, 2),
                Point::new(30, 3),
            ]
        );
    }

    #[test]
    fn drain_rewrites_history_including_flushed_blocks() {
        // Even points older than the start of the first flushed block must
        // land in history: drain compacts and rewrites the whole file.
        let dir = tempdir("drain");
        let store = open_store(&dir, 2); // tiny blocks force flushing
        store
            .create_series("s", UnorderedPolicy::Buffer, None)
            .unwrap();
        store
            .write(
                "s",
                &[
                    Point::new(100, 1),
                    Point::new(200, 2),
                    Point::new(300, 3),
                    Point::new(400, 4),
                ],
            )
            .unwrap();
        store.flush(Some("s")).unwrap();
        assert_eq!(store.stats("s").unwrap().blocks, 2);

        // 50 and 60 predate the first block (ts=100); 250 lands in between.
        let out = store
            .write(
                "s",
                &[Point::new(50, 0), Point::new(250, 9), Point::new(60, 7)],
            )
            .unwrap();
        assert_eq!(out.accepted, 0);
        assert_eq!(out.buffered, 3);

        let d = store.drain("s").unwrap();
        assert_eq!(d.merged, 3);
        assert_eq!(d.buffered_remaining, 0);

        let q = store.query("s", i64::MIN, i64::MAX).unwrap();
        assert_eq!(
            q.points,
            vec![
                Point::new(50, 0),
                Point::new(60, 7),
                Point::new(100, 1),
                Point::new(200, 2),
                Point::new(250, 9),
                Point::new(300, 3),
                Point::new(400, 4),
            ]
        );
        // 7 points at block size 2 -> ceil = 4 rewritten blocks.
        assert_eq!(store.stats("s").unwrap().blocks, 4);
        drop(store);

        // The rewrite must survive reopen via the normal recovery scan.
        let store2 = open_store(&dir, 2);
        let rep = store2.recovery_report();
        assert_eq!(rep.torn_records, 0);
        assert_eq!(rep.blocks_loaded, 4);
        let q = store2.query("s", 40, 260).unwrap();
        assert_eq!(
            q.points,
            vec![
                Point::new(50, 0),
                Point::new(60, 7),
                Point::new(100, 1),
                Point::new(200, 2),
                Point::new(250, 9),
            ]
        );
    }

    #[test]
    fn drain_with_empty_buffer_is_noop() {
        let dir = tempdir("drainnoop");
        let store = open_store(&dir, 100);
        store
            .create_series("s", UnorderedPolicy::Buffer, None)
            .unwrap();
        store.write("s", &[Point::new(1, 1)]).unwrap();
        let d = store.drain("s").unwrap();
        assert_eq!(d.merged, 0);
        assert_eq!(
            store.query("s", 0, 10).unwrap().points,
            vec![Point::new(1, 1)]
        );
    }

    #[test]
    fn index_prunes_blocks_for_range_query() {
        let dir = tempdir("prune");
        let store = open_store(&dir, 2);
        store
            .create_series("s", UnorderedPolicy::Reject, None)
            .unwrap();
        let pts: Vec<_> = (0..20).map(|i| Point::new(i * 10, i)).collect();
        store.write("s", &pts).unwrap();
        store.flush(None).unwrap();
        // 10 blocks of 2. A tight window should touch very few of them.
        let r = store.query("s", 50, 60).unwrap();
        assert_eq!(r.points, vec![Point::new(50, 5), Point::new(60, 6)]);
        assert!(r.blocks_scanned <= 2, "scanned {}", r.blocks_scanned);
    }
}
