//! RocksDB-backed versioned state.
//!
//! ## Key layout (single default column family, byte-prefixed namespaces)
//!
//! | prefix        | key                         | value                  |
//! |---------------|-----------------------------|------------------------|
//! | `meta:ver`    | —                           | u64be current version  |
//! | `root:v{...}` | u64be(version) after prefix | 32-byte root hash      |
//! | `mfst:v{...}` | u64be(version)              | version manifest (see below) |
//! | `node:`       | 32-byte node hash           | node preimage bytes    |
//!
//! ## Manifest encoding
//!
//! ```text
//! u64be version | u64be created_at_ms | 32B root | u64be n | u32be height
//! repeat n: u32be klen | key | u32be vlen | value
//! ```
//!
//! ## Atomicity / failure behaviour
//!
//! A commit stages everything (node records, manifest, root index, current
//! version pointer) into a single RocksDB `WriteBatch` and applies it with one
//! atomic `db.write`. If anything fails before that point the batch is
//! dropped: no new version pointer, no root, no half-built snapshot are ever
//! observable. In-memory state is updated only after the write succeeds. On
//! restart the current KV state is rebuilt from the manifest of the version
//! recorded in `meta:ver`, so a crash mid-process loses nothing committed and
//! reveals nothing uncommitted.

use crate::core::{build_tree, BuiltTree};
use crate::error::{AppError, AppResult};
use rocksdb::{DBCompressionType, IteratorMode, Options, WriteBatch, DB};
use std::collections::BTreeMap;
use std::sync::Mutex;

const META_VER: &[u8] = b"meta:ver";
const ROOT_PREFIX: &[u8] = b"root:v";
const MFST_PREFIX: &[u8] = b"mfst:v";
const NODE_PREFIX: &[u8] = b"node:";

/// A single write op. `Put([])` stores a present *empty* value; `Delete`
/// removes the key entirely — the two are always distinct.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    Put(Vec<u8>),
    Delete,
}

#[derive(Debug, Clone)]
pub struct RootInfo {
    pub version: u64,
    pub root: [u8; 32],
    pub leaf_count: u64,
    pub height: u32,
    pub created_at_ms: u64,
}

struct Inner {
    version: u64,
    /// Current live state. Only present keys live here; an empty `Vec<u8>`
    /// value is a present empty value, distinct from a deleted key.
    current: BTreeMap<Vec<u8>, Vec<u8>>,
}

pub struct Store {
    db: DB,
    inner: Mutex<Inner>,
}

pub struct CommitOutcome {
    pub version: u64,
    pub root: [u8; 32],
    pub leaf_count: u64,
    pub height: u32,
    pub applied_puts: usize,
    pub applied_deletes: usize,
}

fn root_key(version: u64) -> Vec<u8> {
    let mut k = ROOT_PREFIX.to_vec();
    k.extend_from_slice(&version.to_be_bytes());
    k
}

fn mfst_key(version: u64) -> Vec<u8> {
    let mut k = MFST_PREFIX.to_vec();
    k.extend_from_slice(&version.to_be_bytes());
    k
}

fn node_key(hash: &[u8; 32]) -> Vec<u8> {
    let mut k = NODE_PREFIX.to_vec();
    k.extend_from_slice(hash);
    k
}

fn now_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Read a u64 from the end of a key (after a fixed prefix), or decode a value.
fn read_u64_be(b: &[u8]) -> u64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[b.len() - 8..]);
    u64::from_be_bytes(a)
}

// ---- manifest (de)serialization -------------------------------------------

fn encode_manifest(version: u64, created_at_ms: u64, root: &[u8; 32], tree: &BuiltTree) -> Vec<u8> {
    let mut buf = Vec::with_capacity(8 + 8 + 32 + 8 + 4 + tree.leaves.len() * 32);
    buf.extend_from_slice(&version.to_be_bytes());
    buf.extend_from_slice(&created_at_ms.to_be_bytes());
    buf.extend_from_slice(root);
    buf.extend_from_slice(&tree.leaf_count.to_be_bytes());
    buf.extend_from_slice(&tree.height.to_be_bytes());
    for (k, v) in &tree.leaves {
        buf.extend_from_slice(&(k.len() as u32).to_be_bytes());
        buf.extend_from_slice(k);
        buf.extend_from_slice(&(v.len() as u32).to_be_bytes());
        buf.extend_from_slice(v);
    }
    buf
}

pub(crate) struct Manifest {
    pub(crate) version: u64,
    pub(crate) created_at_ms: u64,
    pub(crate) root: [u8; 32],
    pub(crate) leaf_count: u64,
    pub(crate) height: u32,
    pub(crate) leaves: Vec<(Vec<u8>, Vec<u8>)>,
}

fn decode_manifest(buf: &[u8]) -> AppResult<Manifest> {
    struct Cur<'a> {
        buf: &'a [u8],
        pos: usize,
    }
    impl<'a> Cur<'a> {
        fn need(&self, extra: usize) -> AppResult<()> {
            if self.buf.len() < self.pos + extra {
                Err(AppError::Storage(format!(
                    "manifest truncated at {}: need {extra} more bytes (have {})",
                    self.pos,
                    self.buf.len() - self.pos
                )))
            } else {
                Ok(())
            }
        }
        fn u64(&mut self) -> AppResult<u64> {
            self.need(8)?;
            let mut a = [0u8; 8];
            a.copy_from_slice(&self.buf[self.pos..self.pos + 8]);
            self.pos += 8;
            Ok(u64::from_be_bytes(a))
        }
        fn u32(&mut self) -> AppResult<u32> {
            self.need(4)?;
            let mut a = [0u8; 4];
            a.copy_from_slice(&self.buf[self.pos..self.pos + 4]);
            self.pos += 4;
            Ok(u32::from_be_bytes(a))
        }
        fn take(&mut self, len: usize) -> AppResult<&'a [u8]> {
            self.need(len)?;
            let s = &self.buf[self.pos..self.pos + len];
            self.pos += len;
            Ok(s)
        }
    }

    let mut c = Cur { buf, pos: 0 };
    c.need(60)?;
    let version = c.u64()?;
    let created_at_ms = c.u64()?;
    let mut root = [0u8; 32];
    root.copy_from_slice(c.take(32)?);
    let leaf_count = c.u64()?;
    let height = c.u32()?;

    let mut leaves = Vec::with_capacity(leaf_count as usize);
    for _ in 0..leaf_count {
        let klen = c.u32()? as usize;
        let k = c.take(klen)?.to_vec();
        let vlen = c.u32()? as usize;
        let v = c.take(vlen)?.to_vec();
        leaves.push((k, v));
    }
    if c.pos != buf.len() {
        return Err(AppError::Storage(format!(
            "manifest has {} trailing bytes",
            buf.len() - c.pos
        )));
    }
    Ok(Manifest {
        version,
        created_at_ms,
        root,
        leaf_count,
        height,
        leaves,
    })
}

impl Store {
    pub fn open(path: &str) -> AppResult<Self> {
        let mut opts = Options::default();
        opts.create_if_missing(true);
        opts.set_compression_type(DBCompressionType::Snappy);
        // fsync on WAL write — commits must survive process/host restart.
        opts.set_use_fsync(true);

        let db = DB::open(&opts, path)
            .map_err(|e| AppError::Storage(format!("opening RocksDB at {path:?}: {e}")))?;

        let (version, current) = match db.get(META_VER).map_err(wrap_db)? {
            Some(raw) => {
                let mut a = [0u8; 8];
                a.copy_from_slice(&raw);
                let v = u64::from_be_bytes(a);
                // Rebuild live state from the latest committed manifest.
                let mfst_bytes = db.get(mfst_key(v)).map_err(wrap_db)?.ok_or_else(|| {
                    AppError::Storage(format!("meta:ver={v} but manifest {v} is missing"))
                })?;
                let mfst = decode_manifest(&mfst_bytes)?;
                let current: BTreeMap<Vec<u8>, Vec<u8>> = mfst.leaves.into_iter().collect();
                (v, current)
            }
            None => (0u64, BTreeMap::new()),
        };

        Ok(Store {
            db,
            inner: Mutex::new(Inner { version, current }),
        })
    }

    pub fn current_version(&self) -> u64 {
        self.inner.lock().unwrap().version
    }

    /// Atomically apply a write batch and publish the new immutable root.
    pub fn commit(&self, writes: Vec<(Vec<u8>, Op)>) -> AppResult<CommitOutcome> {
        if writes.is_empty() {
            return Err(AppError::bad_request("write batch is empty"));
        }
        for (k, _) in &writes {
            if k.is_empty() {
                return Err(AppError::bad_request("keys must be non-empty"));
            }
        }

        // 1. Resolve last-write-wins under the lock and take a snapshot copy.
        let (next_version, snapshot, applied_puts, applied_deletes) = {
            let inner = self.inner.lock().unwrap();
            // Input order decides ties: the final op for each key wins.
            let mut resolved: BTreeMap<Vec<u8>, Op> = BTreeMap::new();
            for (k, op) in writes {
                resolved.insert(k, op);
            }
            let mut puts = 0usize;
            let mut deletes = 0usize;
            let mut next = inner.current.clone();
            for (k, op) in resolved {
                match op {
                    Op::Put(v) => {
                        next.insert(k, v);
                        puts += 1;
                    }
                    Op::Delete => {
                        next.remove(&k);
                        deletes += 1;
                    }
                }
            }
            (inner.version + 1, next, puts, deletes)
            // lock released here
        };

        // 2. Build the immutable tree (CPU; no I/O, no state mutation).
        let leaves: Vec<(Vec<u8>, Vec<u8>)> = snapshot
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        let tree = build_tree(leaves);
        let root = crate::core::commit_hash(next_version, &tree.top, tree.leaf_count, tree.height);
        let created_at_ms = now_ms();

        // 3. Stage all writes in ONE atomic batch. No API can see any of this
        //    until db.write succeeds.
        let mut batch = WriteBatch::default();
        for (h, preimage) in &tree.records {
            batch.put(node_key(h), preimage);
        }
        let manifest = encode_manifest(next_version, created_at_ms, &root, &tree);
        batch.put(mfst_key(next_version), manifest);
        batch.put(root_key(next_version), root);
        batch.put(META_VER, next_version.to_be_bytes());

        // 4. Atomic publish. On failure the batch is dropped (on return from
        //    this Err) — the old version/root remain the only visible state.
        self.db.write(batch).map_err(|e| {
            AppError::Storage(format!(
                "commit {next_version} failed (nothing published): {e}"
            ))
        })?;

        // 5. Commit durable: update the in-memory view to match.
        {
            let mut inner = self.inner.lock().unwrap();
            // Guard against any weird reordering: versions are monotonic.
            assert_eq!(inner.version + 1, next_version);
            inner.version = next_version;
            inner.current = snapshot;
        }

        Ok(CommitOutcome {
            version: next_version,
            root,
            leaf_count: tree.leaf_count,
            height: tree.height,
            applied_puts,
            applied_deletes,
        })
    }

    fn load_manifest(&self, version: u64) -> AppResult<Manifest> {
        let buf = self
            .db
            .get(mfst_key(version))
            .map_err(wrap_db)?
            .ok_or_else(|| AppError::not_found(format!("version {version} does not exist")))?;
        decode_manifest(&buf)
    }

    /// Resolve a version argument: None → current version (error if no commit
    /// has ever been published).
    fn resolve_version(&self, v: Option<u64>) -> AppResult<u64> {
        match v {
            Some(x) => {
                if x == 0 {
                    return Err(AppError::bad_request(
                        "versions start at 1; query the current root for the empty state",
                    ));
                }
                // existence check → clean 404
                self.db
                    .get(root_key(x))
                    .map_err(wrap_db)?
                    .map(|_| x)
                    .ok_or_else(|| AppError::not_found(format!("version {x} does not exist")))
            }
            None => {
                let cur = self.current_version();
                if cur == 0 {
                    Err(AppError::not_found(
                        "no version published yet; send a write batch first",
                    ))
                } else {
                    Ok(cur)
                }
            }
        }
    }

    pub fn root_info(&self, version: Option<u64>) -> AppResult<RootInfo> {
        let v = self.resolve_version(version)?;
        let mfst = self.load_manifest(v)?;
        Ok(RootInfo {
            version: v,
            root: mfst.root,
            leaf_count: mfst.leaf_count,
            height: mfst.height,
            created_at_ms: mfst.created_at_ms,
        })
    }

    /// Enumerate every published version, ascending (roots are immutable).
    pub fn list_roots(&self) -> AppResult<Vec<RootInfo>> {
        let mut out = Vec::new();
        let iter = self
            .db
            .iterator(IteratorMode::From(ROOT_PREFIX, rocksdb::Direction::Forward));
        for item in iter {
            let (k, val) = item.map_err(wrap_db)?;
            if !k.starts_with(ROOT_PREFIX) {
                break;
            }
            let version = read_u64_be(&k[ROOT_PREFIX.len()..]);
            let mut root = [0u8; 32];
            if val.len() != 32 {
                return Err(AppError::Storage(format!(
                    "root {version} stored with {} bytes, expected 32",
                    val.len()
                )));
            }
            root.copy_from_slice(&val);
            let mfst = self.load_manifest(version)?;
            out.push(RootInfo {
                version,
                root,
                leaf_count: mfst.leaf_count,
                height: mfst.height,
                created_at_ms: mfst.created_at_ms,
            });
        }
        Ok(out)
    }

    /// Fetch a stored node preimage (audit support).
    pub fn node_preimage(&self, hash: &[u8; 32]) -> AppResult<Option<Vec<u8>>> {
        self.db.get(node_key(hash)).map_err(wrap_db)
    }

    /// Build the tree snapshot for a version from its persisted manifest.
    pub(crate) fn tree_at(&self, version: u64) -> AppResult<(Manifest, BuiltTree)> {
        let mfst = self.load_manifest(version)?;
        let tree = build_tree(mfst.leaves.clone());
        if mfst.version != version {
            return Err(AppError::Storage(format!(
                "internal: manifest version {} != requested {version}",
                mfst.version
            )));
        }
        let recomputed_root =
            crate::core::commit_hash(version, &tree.top, tree.leaf_count, tree.height);
        if recomputed_root != mfst.root {
            return Err(AppError::Storage(format!(
                "internal: rebuilt top for version {version} != stored root"
            )));
        }
        Ok((mfst, tree))
    }

    /// Current live state keys (debugging/inspection helper).
    pub fn current_snapshot(&self) -> BTreeMap<Vec<u8>, Vec<u8>> {
        self.inner.lock().unwrap().current.clone()
    }
}

fn wrap_db(e: rocksdb::Error) -> AppError {
    AppError::from(e)
}
