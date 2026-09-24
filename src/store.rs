//! Raw paged storage plus bucket/directory page codecs.
//!
//! The index is one file of fixed-size [`PAGE_SIZE`] pages addressed by a
//! 32-bit page number:
//!
//! ```text
//! page 0          header (see header.rs)
//! page 1..        directory runs and bucket pages, allocated by index.rs
//! ```
//!
//! Page writes go through a single position-independent `pwrite` followed by
//! `fdatasync` at each durability barrier, so write order equals persistence
//! order on a crash (within the guarantees of a conventional filesystem).
use crate::error::{IndexError, Result};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

pub const PAGE_SIZE: usize = 4096;
const PAGE_CRC: u64 = 0; // placeholder documenting the trailing crc slot

fn fnv1a_64(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

pub struct Pager {
    file: File,
}

impl Pager {
    /// Create a new index file. Fails if the path already exists.
    pub fn create(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)?;
        let p = Pager { file };
        Ok(p)
    }

    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        Ok(Pager { file })
    }

    /// Write page 0 (the header).
    pub fn write_header(&mut self, buf: &[u8]) -> Result<()> {
        debug_assert_eq!(buf.len(), PAGE_SIZE);
        self.file.seek(SeekFrom::Start(0))?;
        self.file.write_all(buf)?;
        self.file.sync_data()?;
        Ok(())
    }

    pub fn read_header(&mut self) -> Result<[u8; PAGE_SIZE]> {
        self.read_page(0)
    }

    pub fn read_page(&mut self, page_no: u32) -> Result<[u8; PAGE_SIZE]> {
        let mut buf = [0u8; PAGE_SIZE];
        let off = page_no as u64 * PAGE_SIZE as u64;
        self.file.seek(SeekFrom::Start(off))?;
        self.file.read_exact(&mut buf)?;
        Ok(buf)
    }

    /// Write one page. The caller decides when to call [`Pager::sync`].
    pub fn write_page(&mut self, page_no: u32, buf: &[u8]) -> Result<()> {
        debug_assert_eq!(buf.len(), PAGE_SIZE);
        let off = page_no as u64 * PAGE_SIZE as u64;
        self.file.seek(SeekFrom::Start(off))?;
        self.file.write_all(buf)?;
        Ok(())
    }

    /// Write a contiguous run of pages from `buf` (length must be a whole
    /// number of pages) starting at `start_page`.
    pub fn write_run(&mut self, start_page: u32, buf: &[u8]) -> Result<()> {
        debug_assert!(buf.len().is_multiple_of(PAGE_SIZE));
        let off = start_page as u64 * PAGE_SIZE as u64;
        self.file.seek(SeekFrom::Start(off))?;
        self.file.write_all(buf)?;
        Ok(())
    }

    pub fn sync(&mut self) -> Result<()> {
        self.file.sync_data()?;
        Ok(())
    }

    /// Number of whole pages currently in the file.
    pub fn page_count(&mut self) -> Result<u32> {
        let len = self.file.metadata()?.len();
        Ok((len / PAGE_SIZE as u64) as u32)
    }
}

// ---------------------------------------------------------------------------
// Bucket pages
// ---------------------------------------------------------------------------

const BUCK_MAGIC: &[u8; 4] = b"BUCK";

#[derive(Debug, Clone)]
pub struct Bucket {
    pub local_depth: u32,
    pub entries: Vec<(Vec<u8>, Vec<u8>)>,
}

/// Bytes needed to store a bucket with `capacity` entries bounded by the
/// configured key/value limits.
pub fn bucket_required_size(capacity: usize, key_max: usize, val_max: usize) -> usize {
    // magic(4) + depth(4) + count(4) + per-entry [klen(4)+vlen(4)+key+value] + crc(8)
    12 + capacity * (8 + key_max + val_max) + 8
}

pub fn encode_bucket(b: &Bucket) -> Vec<u8> {
    let mut buf = vec![0u8; PAGE_SIZE];
    buf[0..4].copy_from_slice(BUCK_MAGIC);
    buf[4..8].copy_from_slice(&b.local_depth.to_le_bytes());
    buf[8..12].copy_from_slice(&(b.entries.len() as u32).to_le_bytes());
    let mut off = 12;
    for (k, v) in &b.entries {
        buf[off..off + 4].copy_from_slice(&(k.len() as u32).to_le_bytes());
        off += 4;
        buf[off..off + 4].copy_from_slice(&(v.len() as u32).to_le_bytes());
        off += 4;
        buf[off..off + k.len()].copy_from_slice(k);
        off += k.len();
        buf[off..off + v.len()].copy_from_slice(v);
        off += v.len();
    }
    let crc = fnv1a_64(&buf[..PAGE_SIZE - 8]);
    buf[PAGE_SIZE - 8..].copy_from_slice(&crc.to_le_bytes());
    let _ = PAGE_CRC;
    buf
}

pub fn decode_bucket(buf: &[u8]) -> Result<Bucket> {
    if &buf[0..4] != BUCK_MAGIC {
        return Err(IndexError::Corrupt("bad bucket magic".into()));
    }
    let crc_stored = u64::from_le_bytes(buf[PAGE_SIZE - 8..].try_into().unwrap());
    if fnv1a_64(&buf[..PAGE_SIZE - 8]) != crc_stored {
        return Err(IndexError::Corrupt("bucket checksum mismatch".into()));
    }
    let local_depth = u32::from_le_bytes(buf[4..8].try_into().unwrap());
    let count = u32::from_le_bytes(buf[8..12].try_into().unwrap()) as usize;
    let mut entries = Vec::with_capacity(count);
    let mut off = 12;
    for _ in 0..count {
        if off + 8 > PAGE_SIZE - 8 {
            return Err(IndexError::Corrupt(
                "bucket entry header overruns page".into(),
            ));
        }
        let klen = u32::from_le_bytes(buf[off..off + 4].try_into().unwrap()) as usize;
        off += 4;
        let vlen = u32::from_le_bytes(buf[off..off + 4].try_into().unwrap()) as usize;
        off += 4;
        if off + klen + vlen > PAGE_SIZE - 8 {
            return Err(IndexError::Corrupt("bucket entry overruns page".into()));
        }
        let k = buf[off..off + klen].to_vec();
        off += klen;
        let v = buf[off..off + vlen].to_vec();
        off += vlen;
        entries.push((k, v));
    }
    Ok(Bucket {
        local_depth,
        entries,
    })
}

// ---------------------------------------------------------------------------
// Directory pages
// ---------------------------------------------------------------------------

/// Number of u32 slots that fit in one directory page (8 trailing crc bytes).
pub const DIR_SLOTS_PER_PAGE: usize = (PAGE_SIZE - 8) / 4;

/// Serialize a directory vector into whole pages (each page carries an
/// FNV checksum in its last 8 bytes).
pub fn encode_directory(slots: &[u32]) -> Vec<u8> {
    let pages = slots.len().div_ceil(DIR_SLOTS_PER_PAGE);
    let mut out = vec![0u8; pages * PAGE_SIZE];
    for (i, &s) in slots.iter().enumerate() {
        let o = i * 4;
        out[o..o + 4].copy_from_slice(&s.to_le_bytes());
    }
    for p in 0..pages {
        let base = p * PAGE_SIZE;
        let crc = fnv1a_64(&out[base..base + PAGE_SIZE - 8]);
        out[base + PAGE_SIZE - 8..base + PAGE_SIZE].copy_from_slice(&crc.to_le_bytes());
    }
    out
}

/// Read exactly `nslots` directory slots from a page run starting at
/// `start_page`.
pub fn read_directory(pager: &mut Pager, start_page: u32, nslots: usize) -> Result<Vec<u32>> {
    let need_pages = nslots.div_ceil(DIR_SLOTS_PER_PAGE).max(1);
    let mut raw = vec![0u8; need_pages * PAGE_SIZE];
    for p in 0..need_pages {
        let pg = pager.read_page(start_page + p as u32)?;
        let crc_stored = u64::from_le_bytes(pg[PAGE_SIZE - 8..].try_into().unwrap());
        if fnv1a_64(&pg[..PAGE_SIZE - 8]) != crc_stored {
            return Err(IndexError::Corrupt(format!(
                "directory page {} checksum mismatch",
                start_page + p as u32
            )));
        }
        raw[p * PAGE_SIZE..(p + 1) * PAGE_SIZE].copy_from_slice(&pg);
    }
    let mut slots = Vec::with_capacity(nslots);
    for i in 0..nslots {
        slots.push(u32::from_le_bytes(
            raw[i * 4..i * 4 + 4].try_into().unwrap(),
        ));
    }
    Ok(slots)
}

/// Overwrite a single slot inside a directory page run and persist the page.
pub fn write_directory_slot(
    pager: &mut Pager,
    start_page: u32,
    slot_index: usize,
    bucket_page: u32,
) -> Result<()> {
    let page_idx = slot_index / DIR_SLOTS_PER_PAGE;
    let within = slot_index % DIR_SLOTS_PER_PAGE;
    let page_no = start_page + page_idx as u32;
    let mut pg = pager.read_page(page_no)?;
    let o = within * 4;
    pg[o..o + 4].copy_from_slice(&bucket_page.to_le_bytes());
    let crc = fnv1a_64(&pg[..PAGE_SIZE - 8]);
    pg[PAGE_SIZE - 8..].copy_from_slice(&crc.to_le_bytes());
    pager.write_page(page_no, &pg)?;
    Ok(())
}
