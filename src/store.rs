//! File-backed append-only log, frame codec and crash recovery.
//!
//! ## On-disk format (version 1, magic `MVCCRCL1`)
//!
//! All integers are big-endian. A file is a sequence of independent frames:
//!
//! ```text
//! u16 type | u32 payload_len | payload[payload_len] | u32 crc32
//! ```
//!
//! `crc32` covers `type || payload_len || payload` (the 6 header bytes plus
//! the payload). Frame types:
//!
//! - `PUTV  = 1`: `u64 version | u32 klen | key | u32 vlen | value`
//! - `DELV  = 2`: `u64 version | u32 klen | key`           (a key deletion)
//! - `CMMT  = 3`: `u64 version | u64 first_record_offset`  (commit marker)
//! - `VSET  = 4`: `u32 count | count × u64 version`        (compaction manifest)
//!
//! ### Synchronization boundary
//!
//! A transaction writes its `PUTV`/`DELV` frames (unsynced), and publication is
//! a single `CMMT` frame immediately followed by `fsync`. A version is
//! committed iff its `CMMT` marker is present and valid. Bytes after the last
//! valid frame are a torn tail (from a crash mid-write) and are truncated on
//! open; data frames without a matching `CMMT` stay in the file as dead bytes
//! but are never visible.
//!
//! ### Compaction
//!
//! Garbage collection rewrites the log into a new file:
//! 1. one retained record per key for versions below the watermark (the
//!    snapshot floor — newest version strictly below the watermark);
//! 2. every committed record at version `>= watermark`, in `(version, offset)`
//!    order;
//! 3. a single trailing `VSET` frame listing the versions of all retained
//!    records (all of them committed).
//!
//! The temp file is fully written and fsynced, then `log -> old` and
//! `tmp -> log` are renamed with directory fsyncs around them. Recovery from
//! the four crash points is handled in [`Store::open`].

use std::collections::{BTreeSet, HashMap};
use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::crc::crc32;

pub const MAGIC: &[u8; 8] = b"MVCCRCL1";
pub const FORMAT_VERSION: u8 = 1;
/// `MVCCRCL1` (8 bytes) followed by one big-endian-ish format-version byte.
pub const HEADER_LEN: u64 = 9;

const TYPE_PUTV: u16 = 1;
const TYPE_DELV: u16 = 2;
const TYPE_CMMT: u16 = 3;
const TYPE_VSET: u16 = 4;

/// A mutation as stored in a data frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mutation {
    pub key: Vec<u8>,
    /// `None` means delete.
    pub value: Option<Vec<u8>>,
}

/// One parsed data record (PUTV/DELV).
#[derive(Debug, Clone)]
pub struct Record {
    pub offset: u64,
    pub frame_len: u64,
    pub version: u64,
    pub key: Vec<u8>,
    pub value: Option<Vec<u8>>,
}

/// Result of parsing a log file.
#[derive(Debug, Clone, Default)]
pub struct ParsedFile {
    /// Data records in file order.
    pub records: Vec<Record>,
    /// Committed versions, sorted and deduplicated (from CMMT/VSET).
    pub versions: Vec<u64>,
    /// `(version, first_record_offset)` pairs from CMMT frames.
    pub commit_points: Vec<(u64, u64)>,
    /// True when the file ends with a VSET manifest (it is a compacted log).
    pub has_vset: bool,
    /// Length of the valid prefix; bytes beyond it form a torn tail.
    pub valid_len: u64,
    pub torn: bool,
    pub size: u64,
}

/// Result of a garbage-collection pass.
#[derive(Debug, Clone)]
pub struct GcReport {
    pub watermark: u64,
    pub bytes_before: u64,
    pub bytes_after: u64,
    pub reclaimed: u64,
}

// ---------------------------------------------------------------------------
// Framing
// ---------------------------------------------------------------------------

fn u16_at(b: &[u8]) -> u16 {
    u16::from_be_bytes(b[..2].try_into().unwrap())
}
fn u32_at(b: &[u8]) -> u32 {
    u32::from_be_bytes(b[..4].try_into().unwrap())
}
fn u64_at(b: &[u8]) -> u64 {
    u64::from_be_bytes(b[..8].try_into().unwrap())
}

/// Total on-disk length of a data frame (used by space-accounting tests).
pub fn data_frame_len(key_len: usize, value_len: Option<usize>) -> u64 {
    // type(2) + len(4) + version(8) + klen(4) + key + [vlen(4)+value] + crc(4)
    let payload = 8 + 4 + key_len + value_len.map_or(0, |v| 4 + v);
    (2 + 4 + payload + 4) as u64
}

/// Total on-disk length of the VSET manifest for `version_count` versions.
pub fn vset_manifest_len(version_count: usize) -> u64 {
    // header(6) + count(4) + 8*version_count + crc(4)
    (14 + 8 * version_count) as u64
}

/// Encode a PUTV frame (exposed for disk-format/fault tests).
pub fn put_frame_bytes(version: u64, key: &[u8], value: &[u8]) -> Vec<u8> {
    encode_data_frame(
        TYPE_PUTV,
        version,
        &Mutation {
            key: key.to_vec(),
            value: Some(value.to_vec()),
        },
    )
}

/// Encode a DELV frame (exposed for disk-format/fault tests).
pub fn del_frame_bytes(version: u64, key: &[u8]) -> Vec<u8> {
    encode_data_frame(
        TYPE_DELV,
        version,
        &Mutation {
            key: key.to_vec(),
            value: None,
        },
    )
}

/// Encode a CMMT frame (exposed for disk-format/fault tests).
pub fn commit_frame_bytes(version: u64, first_offset: u64) -> Vec<u8> {
    let mut payload = Vec::with_capacity(16);
    payload.extend_from_slice(&version.to_be_bytes());
    payload.extend_from_slice(&first_offset.to_be_bytes());
    encode_frame(TYPE_CMMT, &payload)
}

fn encode_data_frame(ty: u16, version: u64, m: &Mutation) -> Vec<u8> {
    let mut payload =
        Vec::with_capacity(16 + m.key.len() + m.value.as_ref().map_or(0, |v| 4 + v.len()));
    payload.extend_from_slice(&version.to_be_bytes());
    payload.extend_from_slice(&(m.key.len() as u32).to_be_bytes());
    payload.extend_from_slice(&m.key);
    if let Some(v) = &m.value {
        payload.extend_from_slice(&(v.len() as u32).to_be_bytes());
        payload.extend_from_slice(v);
    }
    encode_frame(ty, &payload)
}

fn encode_frame(ty: u16, payload: &[u8]) -> Vec<u8> {
    assert!(payload.len() <= u32::MAX as usize);
    let mut hdr = [0u8; 6];
    hdr[0..2].copy_from_slice(&ty.to_be_bytes());
    hdr[2..6].copy_from_slice(&(payload.len() as u32).to_be_bytes());

    let mut crc_input = Vec::with_capacity(6 + payload.len());
    crc_input.extend_from_slice(&hdr);
    crc_input.extend_from_slice(payload);

    let mut out = crc_input;
    out.extend_from_slice(&crc32(&out).to_be_bytes());
    out
}

/// Parse exactly one frame at `pos`. Returns `Ok(Some((type,payload,end)))`,
/// `Ok(None)` on a clean EOF at a frame boundary, or an error on a torn tail.
fn parse_one(buf: &[u8], pos: u64) -> io::Result<Option<(u16, Vec<u8>, u64)>> {
    let p = pos as usize;
    if p == buf.len() {
        return Ok(None);
    }
    if buf.len() - p < 10 {
        return Err(torn("short frame header"));
    }
    let ty = u16_at(&buf[p..p + 2]);
    let plen = u32_at(&buf[p + 2..p + 6]) as usize;
    let end = p + 6 + plen + 4;
    if end > buf.len() {
        return Err(torn("frame payload truncated"));
    }
    let want = u32::from_be_bytes(buf[end - 4..end].try_into().unwrap());
    let got = crc32(&buf[p..end - 4]);
    if want != got {
        return Err(torn("frame crc mismatch"));
    }
    Ok(Some((ty, buf[p + 6..end - 4].to_vec(), end as u64)))
}

fn torn(msg: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, format!("torn tail: {msg}"))
}

/// Parse a complete log image.
pub fn parse_bytes(buf: &[u8]) -> io::Result<ParsedFile> {
    let mut pf = ParsedFile {
        size: buf.len() as u64,
        ..Default::default()
    };
    if buf.is_empty() {
        return Ok(pf);
    }
    if buf.len() < HEADER_LEN as usize || &buf[..8] != MAGIC {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "bad magic: not an mvcc-reclaim log",
        ));
    }
    if buf[8] != FORMAT_VERSION {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("unsupported format version {}", buf[8]),
        ));
    }
    let mut pos = HEADER_LEN;
    let mut data_offsets: BTreeSet<u64> = BTreeSet::new();
    let mut cmmt_versions = BTreeSet::new();
    let mut vset_versions: Option<BTreeSet<u64>> = None;
    // Offset of the VSET frame; records at or after this offset were appended
    // by later commits and are covered by later CMMT frames, not by VSET.
    let mut vset_pos: Option<u64> = None;

    loop {
        let frame = match parse_one(buf, pos) {
            Ok(f) => f,
            Err(_) => {
                // Torn tail: keep everything parsed so far.
                pf.torn = true;
                break;
            }
        };
        let (ty, payload, end) = match frame {
            Some(f) => f,
            None => break,
        };
        let frame_len = end - pos;
        match ty {
            TYPE_PUTV | TYPE_DELV => {
                if payload.len() < 12 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "data frame payload too short",
                    ));
                }
                let version = u64_at(&payload[0..8]);
                let klen = u32_at(&payload[8..12]) as usize;
                if payload.len() < 12 + klen {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "data frame key truncated",
                    ));
                }
                let key = payload[12..12 + klen].to_vec();
                let value = if ty == TYPE_PUTV {
                    let rest = &payload[12 + klen..];
                    if rest.len() < 4 {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "put frame value length missing",
                        ));
                    }
                    let vlen = u32_at(&rest[0..4]) as usize;
                    if rest.len() < 4 + vlen {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "put frame value truncated",
                        ));
                    }
                    Some(rest[4..4 + vlen].to_vec())
                } else {
                    None
                };
                pf.records.push(Record {
                    offset: pos,
                    frame_len,
                    version,
                    key,
                    value,
                });
                data_offsets.insert(pos);
            }
            TYPE_CMMT => {
                if payload.len() != 16 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "bad CMMT payload length",
                    ));
                }
                let version = u64_at(&payload[0..8]);
                let offset = u64_at(&payload[8..16]);
                // A commit marker must point at a real data record.
                if !data_offsets.contains(&offset) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("CMMT v{version} points at unknown offset {offset}"),
                    ));
                }
                if cmmt_versions.insert(version) {
                    pf.commit_points.push((version, offset));
                }
            }
            TYPE_VSET => {
                if payload.len() < 4 || (payload.len() - 4) % 8 != 0 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "bad VSET payload length",
                    ));
                }
                let count = u32_at(&payload[0..4]) as usize;
                let mut set = BTreeSet::new();
                for i in 0..count {
                    let at = 4 + i * 8;
                    set.insert(u64_at(&payload[at..at + 8]));
                }
                if count != set.len() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "duplicate version in VSET",
                    ));
                }
                if vset_pos.is_some() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "multiple VSET frames",
                    ));
                }
                vset_pos = Some(pos);
                vset_versions = Some(set);
                pf.has_vset = true;
            }
            other => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("unknown frame type {other}"),
                ));
            }
        }
        pos = end;
    }

    pf.valid_len = pos;
    let mut committed: BTreeSet<u64> = cmmt_versions;
    if let Some(vs) = vset_versions {
        let cut = vset_pos.unwrap_or(u64::MAX);
        // Compacted prefix: every record before VSET must be in the manifest.
        for r in pf.records.iter().filter(|r| r.offset < cut) {
            if !vs.contains(&r.version) {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("record version {} absent from VSET", r.version),
                ));
            }
        }
        committed.extend(vs);
    }
    pf.versions = committed.into_iter().collect();
    Ok(pf)
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

pub struct Store {
    io: Arc<dyn crate::io::Io>,
    dir: PathBuf,
    log: PathBuf,
    file: Mutex<Box<dyn crate::io::IoFile>>,
    /// Current append offset (also the logical file length).
    offset: Mutex<u64>,
}

impl Store {
    /// Open (or create) the log under `dir` and run crash recovery.
    pub fn open(io: Arc<dyn crate::io::Io>, dir: &Path) -> io::Result<(Store, ParsedFile)> {
        fs::create_dir_all(dir).ok();
        let log = dir.join("mvcc.log");
        let old = dir.join("mvcc.old");

        // Recovery across the compaction rename window.
        if !io.exists(&log)? {
            if io.exists(&old)? {
                // Crash after `log -> old`, before `tmp -> log`.
                io.rename(&old, &log)?;
            }
        } else if io.exists(&old)? {
            // Crash after `tmp -> log` but before removing old: log is new.
            io.remove(&old)?;
        }
        // Any stale temp files are leftovers from a crash before any rename.
        if let Ok(entries) = fs::read_dir(dir) {
            for e in entries.flatten() {
                let n = e.file_name();
                let n = n.to_string_lossy();
                if n.starts_with("mvcc.log.") && n.contains(".tmp.") {
                    let _ = io.remove(&e.path());
                }
            }
        }

        let mut file = io.open_or_create(&log)?;
        let mut buf = Vec::new();
        file.seek(SeekFrom::Start(0))?;
        file.read_to_end(&mut buf)?;

        if buf.is_empty() {
            // Brand-new log: write the file header.
            file.seek(SeekFrom::Start(0))?;
            file.write_all(MAGIC)?;
            file.write_all(&[FORMAT_VERSION])?;
            file.sync_all()?;
            buf.extend_from_slice(MAGIC);
            buf.push(FORMAT_VERSION);
        }

        let parsed = parse_bytes(&buf)?;
        if parsed.torn {
            // Drop the torn tail for good: it can never become committed.
            drop(file);
            io.truncate_file(&log, parsed.valid_len)?;
            file = io.open_or_create(&log)?;
        }

        let end = parsed.valid_len;
        file.seek(SeekFrom::Start(end))?;
        Ok((
            Store {
                io,
                dir: dir.to_path_buf(),
                log,
                file: Mutex::new(file),
                offset: Mutex::new(end),
            },
            parsed,
        ))
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn file_len(&self) -> u64 {
        *self.offset.lock().unwrap()
    }

    /// Append data frames for one transaction (no fsync: publication is the
    /// CMMT in [`Store::publish`]). Returns the offset of the first frame.
    pub fn append_mutations(&self, version: u64, mutations: &[Mutation]) -> io::Result<u64> {
        let mut file = self.file.lock().unwrap();
        let mut off = self.offset.lock().unwrap();
        let first = *off;
        file.seek(SeekFrom::Start(first))?;
        for m in mutations {
            let ty = if m.value.is_some() { TYPE_PUTV } else { TYPE_DELV };
            let frame = encode_data_frame(ty, version, m);
            file.write_all(&frame)?;
            *off += frame.len() as u64;
        }
        Ok(first)
    }

    /// Append the CMMT marker and fsync: the single publication boundary.
    pub fn publish(&self, version: u64, first_offset: u64) -> io::Result<()> {
        let mut payload = Vec::with_capacity(16);
        payload.extend_from_slice(&version.to_be_bytes());
        payload.extend_from_slice(&first_offset.to_be_bytes());
        let frame = encode_frame(TYPE_CMMT, &payload);

        let mut file = self.file.lock().unwrap();
        let mut off = self.offset.lock().unwrap();
        file.seek(SeekFrom::Start(*off))?;
        file.write_all(&frame)?;
        *off += frame.len() as u64;
        file.sync_all()?;
        Ok(())
    }

    /// Read and parse the current log.
    pub fn parse_current(&self) -> io::Result<ParsedFile> {
        let buf = self.io.read_file(&self.log)?;
        parse_bytes(&buf)
    }

    /// Rewrite the log, keeping exactly the records at `keep_offsets`.
    ///
    /// `keep_offsets` must contain only committed records. The caller decides
    /// retention using the active-snapshot watermark; this function handles
    /// frame copying, the VSET manifest, fsyncs and the atomic rename dance.
    /// Returns post-compaction stats and a full parse of the new log.
    pub fn compact(
        &self,
        keep_offsets: &BTreeSet<u64>,
        watermark: u64,
    ) -> io::Result<(GcReport, ParsedFile)> {
        let old_bytes = self.io.read_file(&self.log)?;
        let old = parse_bytes(&old_bytes)?;

        let by_offset: HashMap<u64, &Record> =
            old.records.iter().map(|r| (r.offset, r)).collect();

        // Retained records ordered by (version, original offset): every
        // retained version is committed, so this is a legal commit order for
        // the rewritten file.
        let mut kept: Vec<&Record> = keep_offsets
            .iter()
            .filter_map(|o| by_offset.get(o).copied())
            .collect();
        kept.sort_by(|a, b| a.version.cmp(&b.version).then(a.offset.cmp(&b.offset)));

        let versions: BTreeSet<u64> = kept.iter().map(|r| r.version).collect();

        let tmp = self.io.temp_path(&self.log);
        let new_bytes_len;
        {
            let mut w = self.io.open_or_create(&tmp)?;
            let mut pos = 0u64;
            w.seek(SeekFrom::Start(0))?;
            w.write_all(MAGIC)?;
            w.write_all(&[FORMAT_VERSION])?;
            pos += HEADER_LEN;
            for r in &kept {
                let start = r.offset as usize;
                let end = start + r.frame_len as usize;
                let frame = &old_bytes[start..end];
                w.seek(SeekFrom::Start(pos))?;
                w.write_all(frame)?;
                pos += frame.len() as u64;
            }
            let mut vset = Vec::with_capacity(4 + versions.len() * 8);
            vset.extend_from_slice(&(versions.len() as u32).to_be_bytes());
            for v in &versions {
                vset.extend_from_slice(&v.to_be_bytes());
            }
            let manifest = encode_frame(TYPE_VSET, &vset);
            w.seek(SeekFrom::Start(pos))?;
            w.write_all(&manifest)?;
            pos += manifest.len() as u64;
            w.sync_all()?;
            new_bytes_len = pos;
        }

        let old = self.dir.join("mvcc.old");
        // 1) log -> old (log currently exists, old does not).
        self.io.rename(&self.log, &old)?;
        self.io.sync_dir(&self.dir)?;
        // 2) tmp -> log.
        self.io.rename(&tmp, &self.log)?;
        self.io.sync_dir(&self.dir)?;
        // 3) old is dead.
        let _ = self.io.remove(&old);

        // Parse the new image, then swap the active handle to its end.
        let new_parsed = self.parse_current()?;
        let mut file = self.io.open_or_create(&self.log)?;
        file.seek(SeekFrom::End(0))?;
        *self.file.lock().unwrap() = file;
        *self.offset.lock().unwrap() = new_bytes_len;

        let bytes_before = old_bytes.len() as u64;
        Ok((
            GcReport {
                watermark,
                bytes_before,
                bytes_after: new_bytes_len,
                reclaimed: bytes_before.saturating_sub(new_bytes_len),
            },
            new_parsed,
        ))
    }
}
