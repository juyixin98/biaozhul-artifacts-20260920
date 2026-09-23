//! RocksDB-backed versioned key/value store.
//!
//! Column families:
//!
//! * **meta** (`M`) — singleton `cur` holding the current version counter.
//! * **data** (`D`) — live state keyed by the raw user key. Presence in this
//!   CF means the key exists; the stored payload is a one-byte presence tag
//!   followed by the value, so an *empty value* (`0x01`) is distinguishable
//!   from a *delete* (key simply absent — tombstones are physically removed).
//! * **snapshots** (`S`) — immutable, per-version copies of every entry:
//!   `u64be(version) ‖ key → u64be(timestamp_ns) ‖ tag ‖ value`. Historical
//!   versions are therefore independently queryable forever.
//!
//! A version is published in a single atomic [`rocksdb::WriteBatch`]: either
//! every mutation, the snapshot rows, the version manifest and the counter
//! land together, or none do. A crash mid-publish can therefore never expose
//! a half-built root.

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use rocksdb::{ColumnFamilyDescriptor, IteratorMode, Options, WriteBatch, DB};

use crate::encoding::{Cursor, Hex32};
use crate::error::{Error, Result};
use crate::tree::{KV, SnapshotTree};

const CF_META: &str = "meta";
const CF_DATA: &str = "data";
const CF_SNAP: &str = "snapshots";

const META_CURRENT: &[u8] = b"cur";

const TAG_PRESENT: u8 = 0x01;

/// One item of a write batch.
#[derive(Debug, Clone)]
pub enum WriteOp {
    /// Set `key` to `value`. An empty `value` is a real, retrievable value.
    Put { key: Vec<u8>, value: Vec<u8> },
    /// Remove `key`. Deleting a non-existent key is a no-op (still bumps the
    /// version, producing an identical root).
    Delete { key: Vec<u8> },
}

/// Metadata of one published, immutable version.
#[derive(Debug, Clone)]
pub struct VersionInfo {
    pub version: u64,
    pub root: [u8; 32],
    pub leaf_count: u64,
    pub created_at_unix_ns: u64,
}

/// Handle to the on-disk service state.
pub struct Store {
    db: DB,
}

impl Store {
    /// Open (or create) the database at `path`, creating column families and
    /// initialising version 0 (empty tree) on first open.
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let mut db_opts = Options::default();
        db_opts.create_if_missing(true);
        db_opts.create_missing_column_families(true);
        db_opts.set_max_open_files(512);
        db_opts.set_keep_log_file_num(2);

        // List/create CFs idempotently so an existing DB opens too.
        let cfs = DB::list_cf(&Options::default(), path.as_ref())
            .unwrap_or_else(|_| vec![CF_META.to_string(), CF_DATA.to_string(), CF_SNAP.to_string()]);
        let descriptors: Vec<ColumnFamilyDescriptor> = {
            let mut names = cfs;
            for needed in [CF_META, CF_DATA, CF_SNAP] {
                if !names.iter().any(|n| n == needed) {
                    names.push(needed.to_string());
                }
            }
            names
                .into_iter()
                .map(|name| ColumnFamilyDescriptor::new(name, Options::default()))
                .collect()
        };

        let db = DB::open_cf_descriptors(&db_opts, path, descriptors)?;
        let store = Store { db };

        // Initialise the counter at version 0 on a brand-new database.
        if store.get_meta(META_CURRENT)?.is_none() {
            store.put_meta(META_CURRENT, &0u64.to_be_bytes())?;
        }
        Ok(store)
    }

    // -- low level helpers -------------------------------------------------

    fn get_meta(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let cf = self.db.cf_handle(CF_META).ok_or_else(|| Error::Storage("missing CF meta".into()))?;
        Ok(self.db.get_cf(&cf, key)?)
    }

    fn put_meta(&self, key: &[u8], val: &[u8]) -> Result<()> {
        let cf = self.db.cf_handle(CF_META).ok_or_else(|| Error::Storage("missing CF meta".into()))?;
        self.db.put_cf(&cf, key, val)?;
        Ok(())
    }

    /// Currently published version (0 before the first batch).
    pub fn current_version(&self) -> Result<u64> {
        match self.get_meta(META_CURRENT)? {
            Some(b) if b.len() == 8 => Ok(u64::from_be_bytes(b.try_into().unwrap())),
            Some(_) => Err(Error::Storage("corrupt current-version key".into())),
            None => Ok(0),
        }
    }

    // -- version manifests -------------------------------------------------

    fn manifest_key(version: u64) -> Vec<u8> {
        let mut k = Vec::with_capacity(9);
        k.push(b'v');
        k.extend_from_slice(&version.to_be_bytes());
        k
    }

    /// Read the manifest for any published version.
    pub fn version_info(&self, version: u64) -> Result<Option<VersionInfo>> {
        let cf = self
            .db
            .cf_handle(CF_SNAP)
            .ok_or_else(|| Error::Storage("missing CF snapshots".into()))?;
        let Some(raw) = self.db.get_cf(&cf, Self::manifest_key(version))? else {
            return Ok(None);
        };
        let mut cur = Cursor::new(&raw);
        let root_bytes = cur.take(32)?;
        let leaf_count = cur.u64()?;
        let ts = cur.u64()?;
        if cur.remaining() != 0 {
            return Err(Error::Storage("trailing bytes in version manifest".into()));
        }
        let mut root = [0u8; 32];
        root.copy_from_slice(root_bytes);
        Ok(Some(VersionInfo { version, root, leaf_count, created_at_unix_ns: ts }))
    }

    /// List published versions in ascending order (1..=current).
    pub fn list_versions(&self) -> Result<Vec<VersionInfo>> {
        let cur = self.current_version()?;
        let mut out = Vec::new();
        // Version 0 is the implicit empty tree and is listed separately.
        for v in 1..=cur {
            if let Some(info) = self.version_info(v)? {
                out.push(info);
            }
        }
        Ok(out)
    }

    // -- state reads --------------------------------------------------------

    fn encode_value(value: &[u8]) -> Vec<u8> {
        let mut out = Vec::with_capacity(1 + value.len());
        out.push(TAG_PRESENT);
        out.extend_from_slice(value);
        out
    }

    /// Look up a key in the live state. `None` = absent (deleted or never
    /// written); `Some(empty)` vs `Some(nonempty)` is preserved.
    pub fn get_current(&self, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let cf = self
            .db
            .cf_handle(CF_DATA)
            .ok_or_else(|| Error::Storage("missing CF data".into()))?;
        match self.db.get_cf(&cf, key)? {
            None => Ok(None),
            Some(raw) => match raw.first() {
                Some(&TAG_PRESENT) => Ok(Some(raw[1..].to_vec())),
                _ => Err(Error::Storage("corrupt data payload (bad tag)".into())),
            },
        }
    }

    /// Materialise the sorted snapshot for a version.
    ///
    /// Version 0 is always the empty tree. Other versions read the immutable
    /// snapshot rows under a RocksDB snapshot for a consistent view.
    pub fn tree_at(&self, version: u64) -> Result<SnapshotTree> {
        if version == 0 {
            return SnapshotTree::from_sorted(Vec::new());
        }
        // Existence check via the manifest → clean UnknownVersion error.
        if self.version_info(version)?.is_none() {
            return Err(Error::UnknownVersion(version));
        }

        let cf = self
            .db
            .cf_handle(CF_SNAP)
            .ok_or_else(|| Error::Storage("missing CF snapshots".into()))?;
        let snapshot = self.db.snapshot();

        let prefix = version.to_be_bytes();
        let mut start = Vec::with_capacity(8);
        start.extend_from_slice(&prefix);

        let mut entries: Vec<KV> = Vec::new();
        let iter = snapshot.iterator_cf(&cf, IteratorMode::From(&start, rocksdb::Direction::Forward));
        for item in iter {
            let (db_key, raw_val) = item?;
            // Snapshot CF keys are exactly u64be(version) ‖ key.
            if db_key.len() < 8 || db_key[..8] != prefix {
                break;
            }
            let mut cur = Cursor::new(&raw_val);
            let _ts = cur.u64()?;
            let tag = cur.take(1)?[0];
            if tag != TAG_PRESENT {
                return Err(Error::Storage("corrupt snapshot payload (bad tag)".into()));
            }
            let value = cur.bytes()?.to_vec();
            if cur.remaining() != 0 {
                return Err(Error::Storage("trailing bytes in snapshot row".into()));
            }
            entries.push(KV { key: db_key[8..].to_vec(), value });
        }
        // Rows come out in RocksDB byte order, which is exactly the key
        // ordering required by the tree.
        SnapshotTree::from_sorted(entries)
    }

    // -- atomic publish ----------------------------------------------------

    /// Fold `ops` over the current live state and publish a new immutable
    /// version. On success returns the new version's metadata.
    ///
    /// Duplicate keys inside one batch resolve to the LAST operation in input
    /// order (last put/delete wins).
    pub fn apply_batch(&self, ops: Vec<WriteOp>) -> Result<VersionInfo> {
        // 1. Read the current full state (point reads are cheaper than a full
        //    scan when batches are small; state size is modest here).
        let mut state: std::collections::BTreeMap<Vec<u8>, Vec<u8>> = std::collections::BTreeMap::new();
        {
            let cf = self
                .db
                .cf_handle(CF_DATA)
                .ok_or_else(|| Error::Storage("missing CF data".into()))?;
            for item in self.db.iterator_cf(&cf, IteratorMode::Start) {
                let (k, raw) = item?;
                match raw.first() {
                    Some(&TAG_PRESENT) => state.insert(k.to_vec(), raw[1..].to_vec()),
                    _ => return Err(Error::Storage("corrupt data payload (bad tag)".into())),
                };
            }
        }

        // 2. Apply ops in input order; later ops overwrite earlier ones.
        let mut affected: std::collections::BTreeSet<Vec<u8>> = std::collections::BTreeSet::new();
        for op in ops {
            match op {
                WriteOp::Put { key, value } => {
                    state.insert(key.clone(), value);
                    affected.insert(key);
                }
                WriteOp::Delete { key } => {
                    state.remove(&key);
                    affected.insert(key);
                }
            }
        }

        // 3. Build the next tree BEFORE writing anything, so a failure here
        //    leaves the database completely untouched.
        let current = self.current_version()?;
        let new_version = current.checked_add(1).ok_or_else(|| Error::bad("version overflow"))?;

        let entries: Vec<KV> = state
            .iter()
            .map(|(k, v)| KV { key: k.clone(), value: v.clone() })
            .collect();
        let tree = SnapshotTree::from_sorted(entries)?;

        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);

        // 4. Publish everything atomically.
        let cf_data = self
            .db
            .cf_handle(CF_DATA)
            .ok_or_else(|| Error::Storage("missing CF data".into()))?;
        let cf_snap = self
            .db
            .cf_handle(CF_SNAP)
            .ok_or_else(|| Error::Storage("missing CF snapshots".into()))?;
        let cf_meta = self
            .db
            .cf_handle(CF_META)
            .ok_or_else(|| Error::Storage("missing CF meta".into()))?;

        let mut batch = WriteBatch::default();

        // 4a. Update only touched live keys.
        for key in &affected {
            match state.get(key) {
                Some(value) => batch.put_cf(&cf_data, key, Self::encode_value(value)),
                None => batch.delete_cf(&cf_data, key),
            };
        }

        // 4b. Write the full immutable snapshot under version-prefixed keys.
        let vp = new_version.to_be_bytes();
        for kv in tree.entries() {
            let mut skey = Vec::with_capacity(8 + kv.key.len());
            skey.extend_from_slice(&vp);
            skey.extend_from_slice(&kv.key);

            let mut sval = Vec::with_capacity(8 + 1 + 4 + kv.value.len());
            sval.extend_from_slice(&ts.to_be_bytes());
            sval.push(TAG_PRESENT);
            crate::encoding::enc_bytes(&mut sval, &kv.value);
            batch.put_cf(&cf_snap, skey, sval);
        }

        // 4c. Version manifest: root, leaf count, timestamp.
        let mut manifest = Vec::with_capacity(32 + 8 + 8);
        manifest.extend_from_slice(tree.root());
        manifest.extend_from_slice(&(tree.leaf_count()).to_be_bytes());
        manifest.extend_from_slice(&ts.to_be_bytes());
        batch.put_cf(&cf_snap, Self::manifest_key(new_version), manifest);

        // 4d. Advance the current pointer LAST inside the batch.
        batch.put_cf(&cf_meta, META_CURRENT, new_version.to_be_bytes());

        self.db.write(batch)?;

        Ok(VersionInfo {
            version: new_version,
            root: *tree.root(),
            leaf_count: tree.leaf_count(),
            created_at_unix_ns: ts,
        })
    }

    /// Convenience accessor for tests/API.
    pub fn root_hex(&self, version: u64) -> Result<Hex32> {
        let info = self
            .version_info(version)?
            .ok_or(Error::UnknownVersion(version))?;
        Ok(Hex32(info.root))
    }
}
