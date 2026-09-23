//! On-disk format.
//!
//! The data directory contains three kinds of append-only binary files:
//!
//! ```text
//! <root>/
//!   seg-0000000000000001  committed transaction at version 1 (a "segment")
//!   seg-0000000000000002  committed transaction at version 2
//!   base-0000000000000005 compacted snapshot of every key at version 5
//! ```
//!
//! * A **segment** `seg-V` is created by transaction commit at version `V`.
//!   It contains one [`Record::Begin`], zero or more `Put`/`Delete` cells,
//!   in key order, and one [`Record::Commit`].
//! * A **base** `base-V` is created by GC/compaction. It contains one
//!   [`Record::Base`] header followed by one [`Record::Put`] or
//!   [`Record::Delete`] per key that existed at version `V` (the compaction
//!   horizon), in key order. It makes every old segment with version
//!   `<= V` redundant.
//!
//! Both files are written through `Vfs::write_atomic`: temp file → fsync →
//! atomic rename → fsync directory. Recovery therefore only ever observes
//! whole files; a CRC is still checked on every frame to catch torn or
//! corrupt bytes.
//!
//! Frame layout (all integers little-endian):
//!
//! ```text
//! u32 magic   = 0x4D56_4343 ("MVCC")
//! u8  record  = 1 Begin | 2 Commit | 3 Base | 4 Put | 5 Delete
//! u32 length  = payload byte length
//! [payload]
//! u32 crc32   = CRC32 over magic..payload (IEEE 802.3 polynomial)
//! ```
//!
//! Payloads:
//! * Begin  / Commit: `u64 version`
//! * Base:            `u64 horizon_version`
//! * Put / Delete:    `u32 key_len | key bytes | u64 cell_version`
//!   (Put additionally: `u32 val_len | value bytes`)

use std::io::{Cursor, Read, Write};
use std::path::Path;

use crate::error::{Error, Result};
use crate::vfs::SharedVfs;

pub const MAGIC: u32 = 0x4D56_4343;

pub const REC_BEGIN: u8 = 1;
pub const REC_COMMIT: u8 = 2;
pub const REC_BASE: u8 = 3;
pub const REC_PUT: u8 = 4;
pub const REC_DELETE: u8 = 5;

/// One decoded cell version.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cell {
    pub key: Vec<u8>,
    pub val: Option<Vec<u8>>,
    /// Version under which this value/tombstone was committed.
    pub version: u64,
}

/// A file replayed from disk.
#[derive(Debug, Clone)]
pub struct Segment {
    /// Version of this file: transaction version for segments, horizon
    /// version for bases.
    pub version: u64,
    pub is_base: bool,
    pub cells: Vec<Cell>,
}

// ---------------------------------------------------------------------------
// CRC32 (IEEE, same polynomial as zlib/gzip)
// ---------------------------------------------------------------------------

const CRC_POLY: u32 = 0xEDB8_8320;

const fn crc_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                CRC_POLY ^ (c >> 1)
            } else {
                c >> 1
            };
            k += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
}

static CRC_TABLE: [u32; 256] = crc_table();

pub fn crc32(data: &[u8]) -> u32 {
    let mut c = 0xFFFF_FFFFu32;
    for &b in data {
        c = CRC_TABLE[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

fn encode_frame(kind: u8, payload: Vec<u8>) -> Vec<u8> {
    let mut out = Vec::with_capacity(13 + payload.len());
    out.extend_from_slice(&MAGIC.to_le_bytes());
    out.push(kind);
    out.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    out.extend_from_slice(&payload);
    let crc = crc32(&out);
    out.extend_from_slice(&crc.to_le_bytes());
    out
}

fn put_u64(buf: &mut Vec<u8>, v: u64) {
    buf.extend_from_slice(&v.to_le_bytes());
}

fn put_cell_payload(cell: &Cell, kind: u8) -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&(cell.key.len() as u32).to_le_bytes());
    p.extend_from_slice(&cell.key);
    put_u64(&mut p, cell.version);
    if kind == REC_PUT {
        let val = cell.val.as_ref().expect("put cell must carry a value");
        p.extend_from_slice(&(val.len() as u32).to_le_bytes());
        p.extend_from_slice(val);
    }
    p
}

/// Serialize a committed transaction (segment file body).
/// Cells are sorted by key for deterministic files.
pub fn encode_segment(version: u64, cells: &mut [Cell]) -> Vec<u8> {
    cells.sort_by(|a, b| a.key.cmp(&b.key));
    let mut out = Vec::new();
    let mut hdr = Vec::new();
    put_u64(&mut hdr, version);
    out.extend_from_slice(&encode_frame(REC_BEGIN, hdr));
    for c in cells.iter() {
        let kind = if c.val.is_some() { REC_PUT } else { REC_DELETE };
        out.extend_from_slice(&encode_frame(kind, put_cell_payload(c, kind)));
    }
    let mut ftr = Vec::new();
    put_u64(&mut ftr, version);
    out.extend_from_slice(&encode_frame(REC_COMMIT, ftr));
    out
}

/// Serialize a compacted base file body.
pub fn encode_base(horizon: u64, cells: &mut [Cell]) -> Vec<u8> {
    cells.sort_by(|a, b| a.key.cmp(&b.key));
    let mut out = Vec::new();
    let mut hdr = Vec::new();
    put_u64(&mut hdr, horizon);
    out.extend_from_slice(&encode_frame(REC_BASE, hdr));
    for c in cells.iter() {
        let kind = if c.val.is_some() { REC_PUT } else { REC_DELETE };
        out.extend_from_slice(&encode_frame(kind, put_cell_payload(c, kind)));
    }
    out
}

// ---------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------

fn read_exact_opt(data: &[u8], pos: &mut usize, n: usize) -> Result<Vec<u8>> {
    if *pos + n > data.len() {
        return Err(Error::corrupt(format!(
            "unexpected end of file at byte {} (need {})",
            pos, n
        )));
    }
    let out = data[*pos..*pos + n].to_vec();
    *pos += n;
    Ok(out)
}

fn read_u32(data: &[u8], pos: &mut usize) -> Result<u32> {
    let b = read_exact_opt(data, pos, 4)?;
    Ok(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
}

fn read_u64(data: &[u8], pos: &mut usize) -> Result<u64> {
    let b = read_exact_opt(data, pos, 8)?;
    Ok(u64::from_le_bytes([
        b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
    ]))
}

fn decode_cell(kind: u8, payload: &[u8]) -> Result<Cell> {
    let mut pos = 0;
    let key_len = read_u32(payload, &mut pos)? as usize;
    let key = read_exact_opt(payload, &mut pos, key_len)?;
    let version = read_u64(payload, &mut pos)?;
    let val = if kind == REC_PUT {
        let val_len = read_u32(payload, &mut pos)? as usize;
        let v = read_exact_opt(payload, &mut pos, val_len)?;
        if pos != payload.len() {
            return Err(Error::corrupt("put cell has trailing bytes"));
        }
        Some(v)
    } else {
        if pos != payload.len() {
            return Err(Error::corrupt("delete cell has trailing bytes"));
        }
        None
    };
    Ok(Cell { key, val, version })
}

/// Parse one segment/base file body.
pub fn decode_file(name: &str, data: &[u8]) -> Result<Segment> {
    let mut pos = 0;
    let mut cells = Vec::new();
    let mut begin_version = None;
    let mut commit_version = None;
    let mut is_base = false;

    while pos < data.len() {
        let frame_start = pos;
        let magic = read_u32(data, &mut pos)?;
        if magic != MAGIC {
            return Err(Error::corrupt(format!(
                "{name}: bad magic at byte {frame_start}"
            )));
        }
        let kind = *read_exact_opt(data, &mut pos, 1)?
            .first()
            .ok_or_else(|| Error::corrupt("missing record kind"))?;
        let len = read_u32(data, &mut pos)? as usize;
        let payload = read_exact_opt(data, &mut pos, len)?;
        let want_crc = read_u32(data, &mut pos)?;
        let header_and_payload = &data[frame_start..frame_start + 9 + len];
        if crc32(header_and_payload) != want_crc {
            return Err(Error::corrupt(format!(
                "{name}: crc mismatch at byte {frame_start}"
            )));
        }

        let mut p = Cursor::new(&payload);
        match kind {
            REC_BEGIN => {
                let mut b = [0u8; 8];
                p.read_exact(&mut b)?;
                begin_version = Some(u64::from_le_bytes(b));
            }
            REC_COMMIT => {
                let mut b = [0u8; 8];
                p.read_exact(&mut b)?;
                commit_version = Some(u64::from_le_bytes(b));
            }
            REC_BASE => {
                let mut b = [0u8; 8];
                p.read_exact(&mut b)?;
                begin_version = Some(u64::from_le_bytes(b));
                is_base = true;
            }
            REC_PUT | REC_DELETE => {
                cells.push(decode_cell(kind, &payload)?);
            }
            other => {
                return Err(Error::corrupt(format!(
                    "{name}: unknown record kind {other}"
                )))
            }
        }
    }

    let version = begin_version
        .ok_or_else(|| Error::corrupt(format!("{name}: missing begin/base header")))?;
    if !is_base {
        match commit_version {
            Some(cv) if cv == version => {}
            Some(cv) => {
                return Err(Error::corrupt(format!(
                    "{name}: begin version {version} != commit version {cv}"
                )))
            }
            None => return Err(Error::corrupt(format!("{name}: missing commit"))),
        }
    }
    Ok(Segment {
        version,
        is_base,
        cells,
    })
}

// ---------------------------------------------------------------------------
// File naming
// ---------------------------------------------------------------------------

pub fn segment_name(version: u64) -> String {
    format!("seg-{version:016x}")
}

pub fn base_name(version: u64) -> String {
    format!("base-{version:016x}")
}

pub fn parse_versioned_name(name: &str) -> Option<(u64, bool)> {
    let (prefix, is_base) = name
        .strip_prefix("seg-")
        .map(|v| (v, false))
        .or_else(|| name.strip_prefix("base-").map(|v| (v, true)))?;
    let version = u64::from_str_radix(prefix, 16).ok()?;
    Some((version, is_base))
}

pub fn is_temp_name(name: &str) -> bool {
    name.starts_with('.') && name.ends_with(".tmp")
}

/// Read an entire small file through the VFS.
pub fn read_whole(vfs: &SharedVfs, path: &Path) -> Result<Vec<u8>> {
    let mut f = vfs.open_read(path)?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf)?;
    Ok(buf)
}

/// Helper kept for symmetry / tests: round-trip bytes through a writer.
pub fn write_vec(w: &mut Vec<u8>, bytes: &[u8]) {
    w.write_all(bytes).expect("vec write cannot fail");
}
