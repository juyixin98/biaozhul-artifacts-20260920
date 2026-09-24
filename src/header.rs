//! On-disk header page and the write-ahead *intent* records it carries.
//!
//! Layout (page 0, [`crate::store::PAGE_SIZE`] bytes):
//!
//! ```text
//! offset  size  field
//! 0       4     magic "EXHI"
//! 4       1     format version
//! 5       1     hash kind id (see hash.rs)
//! 6       2     bucket capacity (entries per bucket)
//! 8       4     global depth
//! 12      4     bucket count
//! 16      4     free-list head page (u32::MAX = empty)
//! 20      4     high-water mark (#pages ever bump-allocated)
//! 24      1     max global depth
//! 25      4     max key length
//! 29      4     max value length
//! 33      4     current directory run start page
//! 37      4     current directory run page count
//! 41      4     intent tag (0 = none)
//! 45      32    intent payload (8 x u32)
//! 77      ...   reserved (zero)
//! last 8  8     FNV-1a-64 checksum of all preceding bytes
//! ```
//!
//! Every structural mutation (bucket split, bucket merge, directory shrink)
//! first persists an intent describing the operation, then performs page
//! writes, then commits with a single header write that both applies the
//! metadata change and clears the intent. Because the commit header reaches
//! disk atomically as one checksummed page, recovery seeing a live intent
//! always observes the *pre-commit* metadata (free list / global depth /
//! bucket count unchanged) and can simply replay the idempotent page writes
//! (see `recover` in `index.rs`).
use crate::error::{IndexError, Result};

pub const MAGIC: &[u8; 4] = b"EXHI";
pub const VERSION: u8 = 1;
pub const HEADER_SIZE: usize = 4096;
const CRC_OFFSET: usize = HEADER_SIZE - 8;

pub const TAG_NONE: u32 = 0;
pub const TAG_SPLIT: u32 = 1;
pub const TAG_MERGE: u32 = 2;
pub const TAG_SHRINK: u32 = 3;

/// Page-number sentinel ("no page").
pub const NULL_PAGE: u32 = u32::MAX;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Intent {
    /// Splitting the bucket at page `old_bucket`, referenced by every
    /// directory slot whose low `local_depth` bits equal `slot0`. Entries are
    /// repartitioned into freshly allocated pages `new_a` (bit `local_depth`
    /// == 0) and `new_b` (bit == 1); both get local depth `local_depth + 1`.
    /// The fresh directory run starts at `new_dir_start`; `old_global_depth`
    /// and `local_depth` let redo derive its length and slot mapping. When
    /// `local_depth == old_global_depth` the directory was doubled; the old
    /// bucket page and old directory run are freed only at commit.
    Split {
        slot0: u32,
        old_bucket: u32,
        new_a: u32,
        new_b: u32,
        new_dir_start: u32,
        old_dir_start: u32,
        old_global_depth: u32,
        local_depth: u32,
    },
    /// Merging the equal-depth bucket pair whose canonical low-bit slots are
    /// `slot0` and `slot0 | 1<<(local_depth-1)`. The untouched pre-merge
    /// bucket pages are `bucket_a` (low half) and `bucket_b` (high half); the
    /// merged result is written into freshly allocated `merged_page` (local
    /// depth one less). A fresh directory run of the same length starts at
    /// `new_dir_start`; the pre-move run starts at `old_dir_start`. Both old
    /// buckets and the old directory run are freed at commit.
    Merge {
        slot0: u32,
        bucket_a: u32,
        bucket_b: u32,
        merged_page: u32,
        new_dir_start: u32,
        old_dir_start: u32,
    },
    /// Halving the logical directory in place (the run itself is kept).
    Shrink,
}

#[derive(Debug, Clone)]
pub struct Header {
    pub hash_kind_id: u8,
    pub bucket_capacity: u16,
    pub global_depth: u32,
    pub bucket_count: u32,
    pub free_head: u32,
    pub high_water: u32,
    pub max_depth: u8,
    pub key_max: u32,
    pub val_max: u32,
    pub dir_start: u32,
    pub dir_pages: u32,
    pub intent: Option<Intent>,
}

impl Header {
    pub fn capacity(&self) -> usize {
        self.bucket_capacity as usize
    }
    pub fn key_max(&self) -> usize {
        self.key_max as usize
    }
    pub fn val_max(&self) -> usize {
        self.val_max as usize
    }
}

fn put_u32(buf: &mut [u8], off: usize, v: u32) {
    buf[off..off + 4].copy_from_slice(&v.to_le_bytes());
}
fn get_u32(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(buf[off..off + 4].try_into().unwrap())
}

fn fnv1a_64(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

pub fn encode(h: &Header) -> Vec<u8> {
    let mut buf = vec![0u8; HEADER_SIZE];
    buf[0..4].copy_from_slice(MAGIC);
    buf[4] = VERSION;
    buf[5] = h.hash_kind_id;
    buf[6..8].copy_from_slice(&h.bucket_capacity.to_le_bytes());
    put_u32(&mut buf, 8, h.global_depth);
    put_u32(&mut buf, 12, h.bucket_count);
    put_u32(&mut buf, 16, h.free_head);
    put_u32(&mut buf, 20, h.high_water);
    buf[24] = h.max_depth;
    put_u32(&mut buf, 25, h.key_max);
    put_u32(&mut buf, 29, h.val_max);
    put_u32(&mut buf, 33, h.dir_start);
    put_u32(&mut buf, 37, h.dir_pages);

    let mut f = [0u32; 8];
    match h.intent {
        None => put_u32(&mut buf, 41, TAG_NONE),
        Some(Intent::Split {
            slot0,
            old_bucket,
            new_a,
            new_b,
            new_dir_start,
            old_dir_start,
            old_global_depth,
            local_depth,
        }) => {
            put_u32(&mut buf, 41, TAG_SPLIT);
            f[0] = slot0;
            f[1] = old_bucket;
            f[2] = new_a;
            f[3] = new_b;
            f[4] = new_dir_start;
            f[5] = old_dir_start;
            f[6] = old_global_depth;
            f[7] = local_depth;
        }
        Some(Intent::Merge {
            slot0,
            bucket_a,
            bucket_b,
            merged_page,
            new_dir_start,
            old_dir_start,
        }) => {
            put_u32(&mut buf, 41, TAG_MERGE);
            f[0] = slot0;
            f[1] = bucket_a;
            f[2] = bucket_b;
            f[3] = merged_page;
            f[4] = new_dir_start;
            f[5] = old_dir_start;
        }
        Some(Intent::Shrink) => put_u32(&mut buf, 41, TAG_SHRINK),
    }
    for (i, v) in f.iter().enumerate() {
        put_u32(&mut buf, 45 + i * 4, *v);
    }
    let crc = fnv1a_64(&buf[..CRC_OFFSET]);
    buf[CRC_OFFSET..CRC_OFFSET + 8].copy_from_slice(&crc.to_le_bytes());
    buf
}

pub fn decode(buf: &[u8]) -> Result<Header> {
    if buf.len() != HEADER_SIZE {
        return Err(IndexError::Corrupt(format!(
            "header page is {} bytes, expected {}",
            buf.len(),
            HEADER_SIZE
        )));
    }
    if &buf[0..4] != MAGIC {
        return Err(IndexError::Corrupt("bad magic; not an index file".into()));
    }
    let crc_stored = u64::from_le_bytes(buf[CRC_OFFSET..CRC_OFFSET + 8].try_into().unwrap());
    let crc_calc = fnv1a_64(&buf[..CRC_OFFSET]);
    if crc_stored != crc_calc {
        return Err(IndexError::Corrupt(
            "header checksum mismatch (torn header write)".into(),
        ));
    }
    if buf[4] != VERSION {
        return Err(IndexError::Corrupt(format!(
            "unsupported format version {}",
            buf[4]
        )));
    }
    let tag = get_u32(buf, 41);
    let f = |i: usize| get_u32(buf, 45 + i * 4);
    let intent = match tag {
        TAG_NONE => None,
        TAG_SPLIT => Some(Intent::Split {
            slot0: f(0),
            old_bucket: f(1),
            new_a: f(2),
            new_b: f(3),
            new_dir_start: f(4),
            old_dir_start: f(5),
            old_global_depth: f(6),
            local_depth: f(7),
        }),
        TAG_MERGE => Some(Intent::Merge {
            slot0: f(0),
            bucket_a: f(1),
            bucket_b: f(2),
            merged_page: f(3),
            new_dir_start: f(4),
            old_dir_start: f(5),
        }),
        TAG_SHRINK => Some(Intent::Shrink),
        other => {
            return Err(IndexError::Corrupt(format!("unknown intent tag {}", other)));
        }
    };
    Ok(Header {
        hash_kind_id: buf[5],
        bucket_capacity: u16::from_le_bytes(buf[6..8].try_into().unwrap()),
        global_depth: get_u32(buf, 8),
        bucket_count: get_u32(buf, 12),
        free_head: get_u32(buf, 16),
        high_water: get_u32(buf, 20),
        max_depth: buf[24],
        key_max: get_u32(buf, 25),
        val_max: get_u32(buf, 29),
        dir_start: get_u32(buf, 33),
        dir_pages: get_u32(buf, 37),
        intent,
    })
}
