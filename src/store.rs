//! File-backed chunked block store with per-level Merkle node files.
//!
//! ## On-disk format (all integers big-endian)
//!
//! ```text
//! <repo>/
//!   manifest           64 bytes (see below)
//!   journal            present only while a write transaction is committed
//!   data.bin           raw file bytes
//!   level-0.bin        n  * 32 bytes — leaf hashes
//!   level-1.bin        ⌈n/2⌉ * 32 bytes
//!   ...                one file per tree level
//! ```
//!
//! ### manifest (64 bytes)
//! | offset | size | field       |
//! |--------|------|-------------|
//! | 0      | 16   | magic `MS-MANIFEST-V1\0\0` |
//! | 16     | 8    | block_size (u64) |
//! | 24     | 8    | data_len (u64) |
//! | 32     | 8    | n (u64, leaf/block count) |
//! | 40     | 24   | reserved, zero |
//!
//! A manifest is published only via temp-file + fsync + rename + dir fsync, so
//! a 64-byte file is either the previous or the new manifest, never torn.
//!
//! ### journal
//! | magic `MS-JOURNAL-V1\0\0\0` (16) |
//! repeated entries:
//! | op:u8 = 1 put / 2 reset | index:u64 | len:u64 | data(len) |
//! For `reset` there is one leading entry carrying the whole new payload and
//! n = ceil(len/block_size). `put` entries may append or overwrite one block.
//!
//! ## Sync boundary per mutating request
//! 1. Journal written atomically (temp+fsync+rename+dir fsync).
//! 2. `data.bin` patched (pwrite + fsync) — truncate when needed.
//! 3. Affected Merkle levels patched incrementally (pwrite + fsync each).
//! 4. New manifest published atomically; journal deleted; directory fsynced.
//!
//! Crash between 1 and 4 is repaired on next open by replaying the journal
//! against `data.bin` and fully rebuilding every level file — always safe
//! whether or not some level bytes had already been patched.

use std::io;
use std::path::{Path, PathBuf};

use crate::merkle::{self, build_levels, empty_root, internal_hash, leaf_hash, Hash32, RangeProof};
use crate::vfs::Vfs;

const MANIFEST_MAGIC: &[u8; 16] = b"MS-MANIFEST-V1\0\0";
const JOURNAL_MAGIC: &[u8; 16] = b"MS-JOURNAL-V1\0\0\0";
const MANIFEST_LEN: usize = 64;

const OP_PUT: u8 = 1;
const OP_RESET: u8 = 2;

#[derive(Debug)]
pub enum StoreError {
    Io(io::Error),
    Corrupt(String),
    InvalidInput(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "io error: {e}"),
            StoreError::Corrupt(s) => write!(f, "repository corrupt: {s}"),
            StoreError::InvalidInput(s) => write!(f, "invalid request: {s}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        StoreError::Io(e)
    }
}

type Result<T> = std::result::Result<T, StoreError>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub block_size: u64,
    pub data_len: u64,
    pub n: u64,
}

pub struct Store<V: Vfs> {
    vfs: V,
    dir: PathBuf,
    block_size: u64,
    data_len: u64,
    n: u64,
}

fn data_path(dir: &Path) -> PathBuf {
    dir.join("data.bin")
}
fn level_path(dir: &Path, level: usize) -> PathBuf {
    dir.join(format!("level-{level}.bin"))
}
fn manifest_path(dir: &Path) -> PathBuf {
    dir.join("manifest")
}
fn journal_path(dir: &Path) -> PathBuf {
    dir.join("journal")
}

fn encode_manifest(m: &Manifest) -> Vec<u8> {
    let mut buf = vec![0u8; MANIFEST_LEN];
    buf[0..16].copy_from_slice(MANIFEST_MAGIC);
    buf[16..24].copy_from_slice(&m.block_size.to_be_bytes());
    buf[24..32].copy_from_slice(&m.data_len.to_be_bytes());
    buf[32..40].copy_from_slice(&m.n.to_be_bytes());
    buf
}

fn decode_manifest(buf: &[u8]) -> Result<Manifest> {
    if buf.len() != MANIFEST_LEN || &buf[0..16] != MANIFEST_MAGIC {
        return Err(StoreError::Corrupt("bad manifest magic/length".into()));
    }
    let rd = |o: usize| -> u64 {
        let mut b = [0u8; 8];
        b.copy_from_slice(&buf[o..o + 8]);
        u64::from_be_bytes(b)
    };
    let block_size = rd(16);
    let data_len = rd(24);
    let n = rd(32);
    if block_size == 0 {
        return Err(StoreError::Corrupt("block_size is zero".into()));
    }
    if !buf[40..].iter().all(|&b| b == 0) {
        return Err(StoreError::Corrupt(
            "reserved manifest bytes non-zero".into(),
        ));
    }
    let want_n = if data_len == 0 {
        0
    } else {
        (data_len - 1) / block_size + 1
    };
    if want_n != n {
        return Err(StoreError::Corrupt(format!(
            "manifest n={n} inconsistent with data_len={data_len}, block_size={block_size}"
        )));
    }
    Ok(Manifest {
        block_size,
        data_len,
        n,
    })
}

#[derive(Debug, Clone)]
struct JournalEntry {
    op: u8,
    index: u64,
    data: Vec<u8>,
}

fn encode_journal(entries: &[JournalEntry]) -> Vec<u8> {
    let mut buf = Vec::new();
    buf.extend_from_slice(JOURNAL_MAGIC);
    for e in entries {
        buf.push(e.op);
        buf.extend_from_slice(&e.index.to_be_bytes());
        buf.extend_from_slice(&(e.data.len() as u64).to_be_bytes());
        buf.extend_from_slice(&e.data);
    }
    buf
}

fn decode_journal(buf: &[u8]) -> Result<Vec<JournalEntry>> {
    if buf.len() < 16 || &buf[0..16] != JOURNAL_MAGIC {
        return Err(StoreError::Corrupt("bad journal magic".into()));
    }
    let mut pos = 16;
    let mut entries = Vec::new();
    while pos < buf.len() {
        if pos + 17 > buf.len() {
            return Err(StoreError::Corrupt("truncated journal header".into()));
        }
        let op = buf[pos];
        if op != OP_PUT && op != OP_RESET {
            return Err(StoreError::Corrupt(format!("unknown journal op {op}")));
        }
        let rd = |o: usize| -> u64 {
            let mut b = [0u8; 8];
            b.copy_from_slice(&buf[o..o + 8]);
            u64::from_be_bytes(b)
        };
        let index = rd(pos + 1);
        let len = rd(pos + 9) as usize;
        pos += 17;
        if pos + len > buf.len() {
            return Err(StoreError::Corrupt("truncated journal payload".into()));
        }
        entries.push(JournalEntry {
            op,
            index,
            data: buf[pos..pos + len].to_vec(),
        });
        pos += len;
    }
    Ok(entries)
}

// Level-file helpers ---------------------------------------------------------

fn read_hash(vfs: &dyn Vfs, path: &Path, idx: u64) -> io::Result<Hash32> {
    let mut buf = [0u8; 32];
    let n = vfs.read_at(path, idx * 32, &mut buf)?;
    if n != 32 {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "level file short read",
        ));
    }
    Ok(buf)
}

fn write_hash(vfs: &dyn Vfs, path: &Path, idx: u64, h: &Hash32) -> io::Result<()> {
    vfs.write_at(path, idx * 32, h)
}

impl<V: Vfs> Store<V> {
    // Open / create ----------------------------------------------------------

    /// Open an existing repository, replaying an outstanding journal if any.
    pub fn open(vfs: V, dir: &Path) -> Result<Self> {
        let mp = manifest_path(dir);
        if !vfs.exists(&mp)? {
            return Err(StoreError::Corrupt(
                "no manifest in directory (use create first)".into(),
            ));
        }
        let raw = vfs.read(&mp)?;
        let m = decode_manifest(&raw)?;

        let mut store = Store {
            vfs,
            dir: dir.to_path_buf(),
            block_size: m.block_size,
            data_len: m.data_len,
            n: m.n,
        };

        let jp = journal_path(dir);
        if store.vfs.exists(&jp)? {
            store.recover()?;
        }
        Ok(store)
    }

    /// Create a fresh empty repository. Fails if one already exists.
    pub fn create(vfs: V, dir: &Path, block_size: u64) -> Result<Self> {
        vfs.mkdir(dir)?;
        if block_size == 0 {
            return Err(StoreError::InvalidInput("block_size must be > 0".into()));
        }
        let mp = manifest_path(dir);
        if vfs.exists(&mp)? {
            return Err(StoreError::InvalidInput("repository already exists".into()));
        }
        let mut store = Store {
            vfs,
            dir: dir.to_path_buf(),
            block_size,
            data_len: 0,
            n: 0,
        };
        store.vfs.write_atomic(&data_path(dir), &[])?;
        store.commit_manifest_remove_journal()?;
        Ok(store)
    }

    // Accessors --------------------------------------------------------------

    pub fn block_size(&self) -> u64 {
        self.block_size
    }
    pub fn data_len(&self) -> u64 {
        self.data_len
    }
    pub fn block_count(&self) -> u64 {
        self.n
    }

    pub fn root(&self) -> Result<Hash32> {
        if self.n == 0 {
            return Ok(empty_root());
        }
        let height = self.height();
        read_hash(&self.vfs, &level_path(&self.dir, height), 0).map_err(StoreError::Io)
    }

    /// Height = level index of the root.
    fn height(&self) -> usize {
        let n = self.n;
        if n <= 1 {
            return 0;
        }
        (63 - (n - 1).leading_zeros()) as usize + 1
    }

    pub fn read_block(&self, index: u64) -> Result<Vec<u8>> {
        if index >= self.n {
            return Err(StoreError::InvalidInput(format!(
                "block index {index} out of range (n={})",
                self.n
            )));
        }
        let start = index * self.block_size;
        let end = ((start + self.block_size).min(self.data_len)) as usize;
        let mut buf = vec![0u8; end - start as usize];
        let got = self.vfs.read_at(&data_path(&self.dir), start, &mut buf)?;
        if got != buf.len() {
            return Err(StoreError::Corrupt("data.bin short read".into()));
        }
        Ok(buf)
    }

    pub fn read_data(&self) -> Result<Vec<u8>> {
        self.vfs.read(&data_path(&self.dir)).map_err(StoreError::Io)
    }

    pub fn manifest(&self) -> Manifest {
        Manifest {
            block_size: self.block_size,
            data_len: self.data_len,
            n: self.n,
        }
    }

    fn node(&self, level: usize, index: u64) -> Result<Hash32> {
        read_hash(&self.vfs, &level_path(&self.dir, level), index).map_err(StoreError::Io)
    }

    /// Build a range proof for blocks `[start,end)`, reading only the proof
    /// nodes from disk — never the full data file.
    pub fn prove_range(&self, start: u64, end: u64) -> Result<RangeProof> {
        merkle::prove(self.n as usize, start as usize, end as usize, |lvl, idx| {
            self.node(lvl, idx as u64).expect("node read")
        })
        .map_err(|e| StoreError::InvalidInput(e.to_string()))
    }

    // Mutations --------------------------------------------------------------

    /// Put (insert/overwrite) one block.
    ///
    /// Rules:
    /// - `index <= n`; `index == n` appends a new block.
    /// - Blocks partition the byte stream contiguously: every block except
    ///   possibly the last is exactly `block_size` bytes.
    /// - A new block can only be appended when the current last block is
    ///   full (i.e. the file length is a multiple of `block_size`). Otherwise
    ///   appending would leave a zero hole between the short last block and
    ///   the new one. Grow the last block to full first.
    /// - The last block may be any length 1..=block_size; changing its length
    ///   changes the file length. Empty blocks are rejected.
    pub fn put_block(&mut self, index: u64, bytes: &[u8]) -> Result<()> {
        if index > self.n {
            return Err(StoreError::InvalidInput(format!(
                "block index {index} beyond n={}",
                self.n
            )));
        }
        if bytes.is_empty() || bytes.len() as u64 > self.block_size {
            return Err(StoreError::InvalidInput(format!(
                "block length {} not in 1..={}",
                bytes.len(),
                self.block_size
            )));
        }
        if index < self.n && index + 1 < self.n && bytes.len() as u64 != self.block_size {
            return Err(StoreError::InvalidInput(
                "only the last block may have a short length".into(),
            ));
        }
        if index == self.n && self.n > 0 && !self.data_len.is_multiple_of(self.block_size) {
            return Err(StoreError::InvalidInput(format!(
                "cannot append block {index}: current last block is short \
                 (data_len={} is not a multiple of block_size={}); fill the \
                 last block to full size first",
                self.data_len, self.block_size
            )));
        }

        let entry = JournalEntry {
            op: OP_PUT,
            index,
            data: bytes.to_vec(),
        };
        // 1. Journal.
        self.vfs.write_atomic(
            &journal_path(&self.dir),
            &encode_journal(std::slice::from_ref(&entry)),
        )?;
        // 2. Data file. Only the current last block (index == n-1) may
        // truncate; appending (index == n) extends; earlier blocks fixed size.
        let allow_truncate = index + 1 == self.n;
        self.apply_put_to_data(&entry, allow_truncate)?;
        // 3. Incremental Merkle patch.
        let new_leaf = leaf_hash(index as usize, bytes);
        self.patch_merkle(index, new_leaf, self.n.max(index + 1))?;
        // 4. Manifest + journal removal.
        if index == self.n {
            self.n += 1;
        }
        self.data_len = self.vfs.len(&data_path(&self.dir))?;
        self.commit_manifest_remove_journal()?;
        Ok(())
    }

    /// Replace the entire payload atomically (journal-guarded full rebuild).
    pub fn reset(&mut self, bytes: &[u8]) -> Result<()> {
        // A reset is ALWAYS journaled — including the empty payload, so that
        // a crash after journaling cannot be mistaken for "no transaction".
        let entries = vec![JournalEntry {
            op: OP_RESET,
            index: 0,
            data: bytes.to_vec(),
        }];
        self.vfs
            .write_atomic(&journal_path(&self.dir), &encode_journal(&entries))?;
        self.rebuild_from_bytes(bytes)?;
        self.commit_manifest_remove_journal()?;
        Ok(())
    }

    /// Reread `data.bin` and rebuild every level file from scratch. Used on
    /// journal recovery and by the full-rebuild comparison tests.
    pub fn force_full_rebuild(&mut self) -> Result<()> {
        let bytes = self.vfs.read(&data_path(&self.dir))?;
        self.rebuild_from_bytes(&bytes)?;
        self.commit_manifest_remove_journal()?;
        Ok(())
    }

    fn apply_put_to_data(&mut self, e: &JournalEntry, allow_truncate: bool) -> Result<()> {
        let dp = data_path(&self.dir);
        let start = e.index * self.block_size;
        self.vfs.write_at(&dp, start, &e.data)?;
        // Only a write to the current LAST block may shrink the file.
        // Overwriting an earlier block never moves the file tail.
        if allow_truncate {
            let new_end = start + e.data.len() as u64;
            let cur_len = self.vfs.len(&dp)?;
            if new_end < cur_len {
                self.vfs.truncate(&dp, new_end)?;
            }
        }
        Ok(())
    }

    /// Recompute one root-to-leaf path after leaf `idx` changed (appending
    /// allowed; `new_n` is the leaf count including the appended leaf).
    fn patch_merkle(&mut self, idx: u64, new_leaf: Hash32, new_n: u64) -> Result<()> {
        let l0 = level_path(&self.dir, 0);
        write_hash(&self.vfs, &l0, idx, &new_leaf)?;
        self.vfs.sync(&l0)?;

        let mut node = new_leaf;
        let mut pos = idx;
        let mut count = new_n;
        let mut level = 0usize;
        while count > 1 {
            let path = level_path(&self.dir, level);
            let parent_pos = pos / 2;
            let parent = if pos.is_multiple_of(2) {
                // Even node: needs right sibling.
                if pos + 1 < count {
                    let right = read_hash(&self.vfs, &path, pos + 1)?;
                    internal_hash(&node, &right)
                } else {
                    // Odd trailing node: promoted unchanged.
                    node
                }
            } else {
                // Odd node: needs left sibling.
                let left = read_hash(&self.vfs, &path, pos - 1)?;
                internal_hash(&left, &node)
            };

            level += 1;
            let next_path = level_path(&self.dir, level);
            write_hash(&self.vfs, &next_path, parent_pos, &parent)?;
            self.vfs.sync(&next_path)?;

            node = parent;
            pos = parent_pos;
            count = count.div_ceil(2);
        }
        Ok(())
    }

    fn rebuild_from_bytes(&mut self, bytes: &[u8]) -> Result<()> {
        let leaves: Vec<Hash32> = bytes
            .chunks(self.block_size as usize)
            .enumerate()
            .map(|(i, c)| leaf_hash(i, c))
            .collect();
        let levels = build_levels(&leaves);

        // data.bin
        let dp = data_path(&self.dir);
        self.vfs.write_atomic(&dp, bytes)?;

        // Remove stale level files (possible shrink), then write current ones.
        let mut lvl = 0usize;
        loop {
            let p = level_path(&self.dir, lvl);
            if lvl < levels.len() {
                let mut buf = Vec::with_capacity(levels[lvl].len() * 32);
                for h in &levels[lvl] {
                    buf.extend_from_slice(h);
                }
                self.vfs.write_atomic(&p, &buf)?;
            } else if self.vfs.exists(&p)? {
                self.vfs.remove(&p)?;
            } else {
                break;
            }
            lvl += 1;
        }

        self.data_len = bytes.len() as u64;
        self.n = leaves.len() as u64;
        Ok(())
    }

    fn commit_manifest_remove_journal(&mut self) -> Result<()> {
        let m = Manifest {
            block_size: self.block_size,
            data_len: self.data_len,
            n: self.n,
        };
        self.vfs
            .write_atomic(&manifest_path(&self.dir), &encode_manifest(&m))?;
        let jp = journal_path(&self.dir);
        if self.vfs.exists(&jp)? {
            self.vfs.remove(&jp)?;
        }
        Ok(())
    }

    /// Replay an outstanding journal and fully rebuild.
    fn recover(&mut self) -> Result<()> {
        let jp = journal_path(&self.dir);
        let raw = self.vfs.read(&jp)?;
        let entries = decode_journal(&raw)?;

        if entries.iter().any(|e| e.op == OP_RESET) {
            // Exactly one reset entry; ignore anything else.
            let e = entries
                .iter()
                .find(|e| e.op == OP_RESET)
                .expect("reset present");
            if e.data.len() as u64 > (u64::MAX / self.block_size) {
                return Err(StoreError::Corrupt("journal payload absurd".into()));
            }
            self.rebuild_from_bytes(&e.data)?;
        } else {
            for e in &entries {
                // The journal for a put transaction holds exactly one entry;
                // it may truncate only when it targeted the old last block.
                let allow_truncate = e.op == OP_PUT && e.index + 1 == self.n;
                self.apply_put_to_data(e, allow_truncate)?;
            }
            let bytes = self.vfs.read(&data_path(&self.dir))?;
            self.rebuild_from_bytes(&bytes)?;
        }
        self.commit_manifest_remove_journal()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{FaultyVfs, MemVfs, Op};

    fn make(bytes: &[u8], bs: u64) -> Store<MemVfs> {
        let mut s = Store::create(MemVfs::new(), Path::new("/repo"), bs).unwrap();
        s.reset(bytes).unwrap();
        s
    }

    fn expected_root(bytes: &[u8], bs: u64) -> Hash32 {
        let leaves: Vec<Hash32> = bytes
            .chunks(bs as usize)
            .enumerate()
            .map(|(i, c)| leaf_hash(i, c))
            .collect();
        merkle::rebuild_root(&leaves)
    }

    #[test]
    fn manifest_roundtrip_and_validation() {
        let m = Manifest {
            block_size: 4,
            data_len: 9,
            n: 3,
        };
        assert_eq!(decode_manifest(&encode_manifest(&m)).unwrap(), m);
        let mut bad = encode_manifest(&m);
        bad[0] ^= 1;
        assert!(decode_manifest(&bad).is_err());
        assert!(decode_manifest(&encode_manifest(&Manifest {
            block_size: 4,
            data_len: 13,
            n: 3
        }))
        .is_err());
    }

    #[test]
    fn incremental_puts_match_full_rebuild() {
        for bs in 1u64..=5 {
            let data: Vec<u8> = (0..20u8).collect();
            let mut s = Store::create(MemVfs::new(), Path::new("/r"), bs).unwrap();
            let chunks: Vec<&[u8]> = data.chunks(bs as usize).collect();
            // `current` tracks the expected on-disk payload across all
            // appends and overwrites; the reference root is always a full
            // rebuild from it.
            let mut current: Vec<u8> = Vec::new();
            for (i, c) in chunks.iter().enumerate() {
                s.put_block(i as u64, c).unwrap();
                current.extend_from_slice(c);
                assert_eq!(
                    s.root().unwrap(),
                    expected_root(&current, bs),
                    "append {i}, bs={bs}"
                );
            }
            // Overwrite a few blocks (including the last, which may be short).
            for i in [0usize, 3, chunks.len() - 1] {
                let mut c = chunks[i].to_vec();
                for b in &mut c {
                    *b = b.wrapping_add(0x55);
                }
                s.put_block(i as u64, &c).unwrap();
                let start = (i as u64 * bs) as usize;
                // Only overwriting the LAST block may shrink the payload.
                if i == chunks.len() - 1 && start + c.len() < current.len() {
                    current.truncate(start + c.len());
                }
                current[start..start + c.len()].copy_from_slice(&c);
                assert_eq!(
                    s.root().unwrap(),
                    expected_root(&current, bs),
                    "overwrite {i}, bs={bs}"
                );
                assert_eq!(
                    s.read_data().unwrap(),
                    current,
                    "payload after overwrite {i}, bs={bs}"
                );
            }
        }
    }

    #[test]
    fn last_block_can_shrink_and_grow() {
        let mut s = make(b"abcdefg", 4); // abcd efg
        assert_eq!(s.block_count(), 2);
        s.put_block(1, b"xy").unwrap(); // truncates
        assert_eq!(s.data_len(), 6);
        assert_eq!(s.read_data().unwrap(), b"abcdxy");
        assert_eq!(s.root().unwrap(), expected_root(b"abcdxy", 4));
        // While the last block is short, appending a new block is rejected.
        assert!(s.put_block(2, b"zzzz").is_err());
        s.put_block(1, b"xyzw").unwrap(); // grows back to full
        assert_eq!(s.root().unwrap(), expected_root(b"abcdxyzw", 4));
        // Now aligned again, appending is allowed.
        s.put_block(2, b"q").unwrap();
        assert_eq!(s.read_data().unwrap(), b"abcdxyzwq");
    }

    #[test]
    fn put_validation() {
        let mut s = make(b"abcd", 4);
        assert!(s.put_block(5, b"x").is_err()); // gap
        assert!(s.put_block(0, &[]).is_err()); // empty
        assert!(s.put_block(0, b"12345").is_err()); // too large
        let mut s2 = make(b"abcdefgh", 4);
        assert!(s2.put_block(0, b"xy").is_err()); // short non-last block
    }

    #[test]
    fn empty_repo_has_empty_root() {
        let s: Store<MemVfs> = Store::create(MemVfs::new(), Path::new("/e"), 8).unwrap();
        assert_eq!(s.root().unwrap(), empty_root());
        assert_eq!(s.block_count(), 0);
    }

    #[test]
    fn range_proof_from_store_verifies() {
        let s = make(b"abcdefghij", 4);
        let p = s.prove_range(0, 1).unwrap();
        let mut blocks = vec![s.read_block(0).unwrap()];
        merkle::verify(&p, 4, 10, &s.root().unwrap(), &blocks).unwrap();
        let p2 = s.prove_range(2, 3).unwrap();
        blocks = vec![s.read_block(2).unwrap()];
        merkle::verify(&p2, 4, 10, &s.root().unwrap(), &blocks).unwrap();
    }

    #[test]
    fn recover_after_each_failure_point() {
        // Baseline: a repo that already contains block 0.
        fn fresh_inner() -> MemVfs {
            let inner = MemVfs::new();
            let mut base = Store::create(inner.clone(), Path::new("/x"), 4).unwrap();
            base.put_block(0, b"abcd").unwrap();
            inner
        }

        // Append a second (full) block, failing the nth mutating VFS call;
        // every interrupted transaction must replay deterministically.
        // Call order per append (n=2 -> levels 0,1):
        //   journal(A,1) data(W,2) level-0(W,3) level-1(W,4) manifest(A,5)
        // Rule 1 fails the journal write itself: no transaction exists, so
        // the repo legitimately keeps the old payload. Rules 2..=5 all
        // recover to the appended payload via journal replay.
        for rule in 1u64..=5 {
            let inner = fresh_inner();
            {
                let vfs = FaultyVfs::new(inner.clone())
                    .fail_on(Op::WriteAtomic, None, rule)
                    .fail_on(Op::WriteAt, None, rule);
                let mut s = Store::open(vfs, Path::new("/x")).unwrap();
                let _ = s.put_block(1, b"efgh");
            }
            // Reopen on a clean wrapper over the SAME backing store.
            let mut s = Store::open(inner.clone(), Path::new("/x")).unwrap();
            let want = if rule == 1 {
                b"abcd".as_slice()
            } else {
                b"abcdefgh".as_slice()
            };
            assert_eq!(s.read_data().unwrap(), want, "rule {rule}");
            assert_eq!(s.root().unwrap(), expected_root(want, 4), "rule {rule}");
            // After recovery the store remains usable: append a fresh block.
            s.put_block(s.block_count(), b"xy").unwrap();
            let mut want2 = want.to_vec();
            want2.extend_from_slice(b"xy");
            assert_eq!(s.read_data().unwrap(), want2, "rule {rule} after append");
        }
    }

    #[test]
    fn recover_torn_level_bytes() {
        let inner = MemVfs::new();
        {
            let mut s = Store::create(inner.clone(), Path::new("/y"), 4).unwrap();
            s.put_block(0, b"abcd").unwrap();
        }
        {
            // Append another block: flip a level-0 byte (torn write) AND make
            // the manifest publish fail, so the journal survives and forces a
            // full rebuild from data.bin on reopen.
            let vfs = FaultyVfs::new(inner.clone())
                .corrupt_on(Op::WriteAt, Some("level-0"), 1)
                .fail_on(Op::WriteAtomic, Some("manifest"), 1);
            let mut s = Store::open(vfs, Path::new("/y")).unwrap();
            let _ = s.put_block(1, b"ef");
        }
        let s = Store::open(inner.clone(), Path::new("/y")).unwrap();
        assert_eq!(s.read_data().unwrap(), b"abcdef");
        assert_eq!(s.root().unwrap(), expected_root(b"abcdef", 4));
    }

    #[test]
    fn silent_level_corruption_without_journal_is_detected() {
        // If level bytes are torn but the transaction committed (journal
        // removed), the on-disk root no longer matches the data. force_full_rebuild
        // recomputes it from data.bin; this documents the trust boundary:
        // either a journal exists (auto-replay) or the committed root is
        // authoritative. Here we rebuild and compare the roots.
        let inner = MemVfs::new();
        let mut s = Store::create(inner.clone(), Path::new("/q"), 4).unwrap();
        s.put_block(0, b"abcd").unwrap();
        {
            // Flip one byte of level-0 directly, bypassing the store.
            use crate::vfs::Vfs;
            let p = Path::new("/q/level-0.bin");
            let mut raw = inner.read(p).unwrap();
            raw[0] ^= 0xff;
            inner.write_atomic(p, &raw).unwrap();
        }
        let mut s2 = Store::open(inner.clone(), Path::new("/q")).unwrap();
        let torn = s2.root().unwrap();
        s2.force_full_rebuild().unwrap();
        assert_eq!(s2.root().unwrap(), expected_root(b"abcd", 4));
        assert_ne!(torn, s2.root().unwrap());
    }

    #[test]
    fn reset_then_crash_recovers() {
        let inner = MemVfs::new();
        {
            let vfs = FaultyVfs::new(inner.clone());
            let mut s = Store::create(vfs, Path::new("/z"), 4).unwrap();
            s.put_block(0, b"abcd").unwrap();
        }
        {
            let vfs = FaultyVfs::new(inner.clone()).fail_on(Op::WriteAtomic, None, 3);
            let mut s = Store::open(vfs, Path::new("/z")).unwrap();
            let _ = s.reset(b"0123456789");
        }
        let s = Store::open(inner.clone(), Path::new("/z")).unwrap();
        assert_eq!(s.read_data().unwrap(), b"0123456789");
        assert_eq!(s.root().unwrap(), expected_root(b"0123456789", 4));
    }

    #[test]
    fn empty_reset_crash_recovers_to_empty() {
        let inner = MemVfs::new();
        {
            let mut s = Store::create(inner.clone(), Path::new("/w"), 4).unwrap();
            s.put_block(0, b"abcd").unwrap();
        }
        {
            // Fail right after the journal (call 1) lands.
            let vfs = FaultyVfs::new(inner.clone()).fail_on(Op::WriteAtomic, None, 2);
            let mut s = Store::open(vfs, Path::new("/w")).unwrap();
            let _ = s.reset(b"");
        }
        let s = Store::open(inner.clone(), Path::new("/w")).unwrap();
        assert_eq!(s.data_len(), 0);
        assert_eq!(s.block_count(), 0);
        assert_eq!(s.root().unwrap(), empty_root());
    }
}
