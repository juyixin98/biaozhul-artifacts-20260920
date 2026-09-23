//! Injectable I/O layer.
//!
//! The repository talks only to the [`Vfs`] trait. Two implementations exist:
//!
//! * [`RealVfs`] — a real directory on disk: `pwrite` + `fsync`, the
//!   production backend used by the HTTP server.
//! * [`SimVfs`] — an in-memory media model that reproduces the durability
//!   boundary of a page-cache OS: `pwrite` only touches a dirty layer; bytes
//!   become durable when `sync` is called. A [`CrashPolicy`] can "cut the
//!   power" at any [`CrashPoint`], optionally retaining only a prefix of the
//!   in-flight write (torn page / half-page write). After a crash a new
//!   `SimVfs` is "rebooted" from the same durable [`SimMedia`], exactly like
//!   reopening a directory after power loss.
//!
//! Durability model assumed (and what the tests exercise):
//! * a single `pwrite` may survive torn — any prefix can land, the rest is
//!   lost;
//! * bytes written but never `sync`ed may be lost entirely;
//!   `sync_all` is a barrier — everything before it is durable, and the
//!   superblock is only written *after* the data sync barrier, which is what
//!   makes the publish order safe.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

/// The three files a repository consists of.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum FileId {
    Data,
    SbA,
    SbB,
}

impl FileId {
    pub fn name(self) -> &'static str {
        match self {
            FileId::Data => crate::format::DATA_FILE,
            FileId::SbA => crate::format::SB_FILE[0],
            FileId::SbB => crate::format::SB_FILE[1],
        }
    }
}

/// Points in the commit protocol at which power can be cut.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    /// Before any byte of the new data record is written.
    DataWriteBegin,
    /// After the data pwrite returns, before fsync.
    DataWriteEnd,
    /// At the data fsync call (write still dirty).
    DataSyncBegin,
    /// Immediately after the data fsync returns.
    DataSyncEnd,
    /// Before any byte of the new superblock page is written.
    RootWriteBegin,
    /// After the superblock pwrite returns, before fsync.
    RootWriteEnd,
    /// At the superblock fsync call (page still dirty).
    RootSyncBegin,
    /// Immediately after the superblock fsync returns.
    RootSyncEnd,
}

impl CrashPoint {
    pub const ALL: [CrashPoint; 8] = [
        CrashPoint::DataWriteBegin,
        CrashPoint::DataWriteEnd,
        CrashPoint::DataSyncBegin,
        CrashPoint::DataSyncEnd,
        CrashPoint::RootWriteBegin,
        CrashPoint::RootWriteEnd,
        CrashPoint::RootSyncBegin,
        CrashPoint::RootSyncEnd,
    ];

    fn file(self) -> FileId {
        use CrashPoint::*;
        match self {
            DataWriteBegin | DataWriteEnd | DataSyncBegin | DataSyncEnd => FileId::Data,
            RootWriteBegin | RootWriteEnd | RootSyncBegin | RootSyncEnd => FileId::SbA, // slot chosen per-commit
        }
    }
}

/// How torn the in-flight write is when power is cut mid-write.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Torn {
    /// No byte survives (write never reached the platter).
    None,
    /// Exactly the first half of the write survives.
    Half,
    /// A short prefix survives (137 bytes of the 4 KiB page; for data records
    /// it is clamped inside the record header, i.e. the header itself tears).
    Short,
}

/// "Cut power" once, at the given point, with the given torn-write behaviour.
/// Construct with [`CrashPolicy::new`]; a policy that never fires is
/// [`CrashPolicy::never`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CrashPolicy {
    /// `None` = never crash. `Some` = crash at this point **once**, the
    /// first time it is reached; afterwards the policy is disarmed (so seed
    /// commits before the target round are never affected when the policy
    /// is (re)armed exactly for the final round).
    pub at: Option<CrashPoint>,
    pub torn: Torn,
    fired: bool,
}

impl CrashPolicy {
    pub fn new(at: CrashPoint, torn: Torn) -> Self {
        CrashPolicy {
            at: Some(at),
            torn,
            fired: false,
        }
    }

    pub fn never() -> Self {
        CrashPolicy {
            at: None,
            torn: Torn::None,
            fired: false,
        }
    }
}

#[derive(Debug)]
pub enum IoError {
    /// OS-level I/O failure (real backend), or use after a simulated crash.
    Io(String),
    /// The configured crash policy fired: the process "lost power". Any
    /// further operation on this VFS instance returns the same error; reboot
    /// with [`SimVfs::reopen`].
    SimulatedCrash,
}

impl std::fmt::Display for IoError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            IoError::Io(s) => write!(f, "io error: {s}"),
            IoError::SimulatedCrash => write!(f, "simulated crash (power lost)"),
        }
    }
}
impl std::error::Error for IoError {}

pub type Result<T> = std::result::Result<T, IoError>;

/// Minimal storage interface needed by the repository. All writes are
/// positional (`pwrite` semantics: no file-position mutation) and durability
/// is explicit via [`Vfs::sync`].
pub trait Vfs {
    /// Create the file if it does not exist, then write `data` at `offset`.
    fn pwrite(&mut self, id: FileId, offset: u64, data: &[u8]) -> Result<()>;
    /// Durably persist all previously written bytes of one file.
    fn sync(&mut self, id: FileId) -> Result<()>;
    /// Durably persist directory-entry changes (newly created files).
    fn sync_dir(&mut self) -> Result<()>;
    /// Read the whole durable file, or `None` if it does not exist.
    fn read(&self, id: FileId) -> Result<Option<Vec<u8>>>;
    fn truncate(&mut self, id: FileId, len: u64) -> Result<()>;
    fn exists(&self, id: FileId) -> bool;
}

// ---------------------------------------------------------------------------
// Real filesystem backend
// ---------------------------------------------------------------------------

pub struct RealVfs {
    dir: PathBuf,
}

impl RealVfs {
    pub fn open(dir: impl AsRef<Path>) -> Result<Self> {
        let dir = dir.as_ref().to_path_buf();
        std::fs::create_dir_all(&dir).map_err(|e| IoError::Io(e.to_string()))?;
        Ok(RealVfs { dir })
    }

    fn path(&self, id: FileId) -> PathBuf {
        self.dir.join(id.name())
    }

    fn open_write(&self, id: FileId) -> Result<File> {
        OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(self.path(id))
            .map_err(|e| IoError::Io(e.to_string()))
    }
}

impl Vfs for RealVfs {
    fn pwrite(&mut self, id: FileId, offset: u64, data: &[u8]) -> Result<()> {
        let mut f = self.open_write(id)?;
        f.seek(SeekFrom::Start(offset))
            .map_err(|e| IoError::Io(e.to_string()))?;
        f.write_all(data).map_err(|e| IoError::Io(e.to_string()))?;
        // drop without fsync: bytes sit in the page cache, exactly like the
        // dirty layer in the simulator.
        drop(f);
        Ok(())
    }

    fn sync(&mut self, id: FileId) -> Result<()> {
        let f = self.open_write(id)?;
        f.sync_all().map_err(|e| IoError::Io(e.to_string()))
    }

    fn sync_dir(&mut self) -> Result<()> {
        let f = File::open(&self.dir).map_err(|e| IoError::Io(e.to_string()))?;
        f.sync_all().map_err(|e| IoError::Io(e.to_string()))
    }

    fn read(&self, id: FileId) -> Result<Option<Vec<u8>>> {
        match File::open(self.path(id)) {
            Ok(mut f) => {
                let mut buf = Vec::new();
                f.read_to_end(&mut buf)
                    .map_err(|e| IoError::Io(e.to_string()))?;
                Ok(Some(buf))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(IoError::Io(e.to_string())),
        }
    }

    fn truncate(&mut self, id: FileId, len: u64) -> Result<()> {
        let f = self.open_write(id)?;
        f.set_len(len).map_err(|e| IoError::Io(e.to_string()))
    }

    fn exists(&self, id: FileId) -> bool {
        self.path(id).exists()
    }
}

// ---------------------------------------------------------------------------
// Fault-injecting in-memory backend
// ---------------------------------------------------------------------------

/// Durable media: what survives a reboot. One byte vector per existing file.
#[derive(Debug, Default, Clone)]
pub struct SimMedia {
    files: HashMap<FileId, Vec<u8>>,
}

impl SimMedia {
    pub fn new() -> Self {
        SimMedia::default()
    }

    pub fn file_len(&self, id: FileId) -> u64 {
        self.files.get(&id).map(|v| v.len() as u64).unwrap_or(0)
    }

    pub fn file_bytes(&self, id: FileId) -> Option<&[u8]> {
        self.files.get(&id).map(|v| v.as_slice())
    }
}

type Segments = Vec<(u64, Vec<u8>)>;

pub struct SimVfs {
    durable: SimMedia,
    /// Unsynced writes per file: ordered (offset, bytes) segments.
    dirty: HashMap<FileId, Segments>,
    policy: CrashPolicy,
    crashed: bool,
}

impl SimVfs {
    pub fn fresh(policy: CrashPolicy) -> Self {
        SimVfs {
            durable: SimMedia::new(),
            dirty: HashMap::new(),
            policy,
            crashed: false,
        }
    }

    /// "Reboot" over the same media with a new policy (use
    /// `CrashPoint::RootSyncEnd`/`Torn::None`, i.e. no further crash, for
    /// recovery verification).
    pub fn reopen(self, policy: CrashPolicy) -> Self {
        SimVfs {
            durable: self.durable,
            dirty: HashMap::new(), // page cache is cold after power loss
            policy,
            crashed: false,
        }
    }

    /// Reboot over a *copy* of the current durable media, leaving `self`
    /// usable for further inspection.
    pub fn reopen_cloned(&self, policy: CrashPolicy) -> Self {
        SimVfs {
            durable: self.durable.clone(),
            dirty: HashMap::new(),
            policy,
            crashed: false,
        }
    }

    /// Consume the durable media (for inspection / cross-instance tests).
    pub fn into_media(self) -> SimMedia {
        self.durable
    }

    /// Durable file length (what recovery would see after a crash).
    pub fn durable_len(&self, id: FileId) -> u64 {
        self.durable.file_len(id)
    }

    /// Copy of durable file bytes.
    pub fn durable_bytes(&self, id: FileId) -> Option<Vec<u8>> {
        self.durable.file_bytes(id).map(|b| b.to_vec())
    }

    /// Directly overwrite durable media, bypassing the dirty layer. Used by
    /// tests to craft corruption (bit flips, garbage pages, OOB pointers).
    pub fn corrupt_durable(&mut self, id: FileId, offset: u64, bytes: &[u8]) {
        let v = self
            .durable
            .files
            .entry(id)
            .or_default();
        let end = (offset as usize) + bytes.len();
        if v.len() < end {
            v.resize(end, 0);
        }
        v[offset as usize..end].copy_from_slice(bytes);
    }

    /// (Re)arm a crash policy, or disarm it with [`CrashPolicy::never`].
    pub fn set_policy(&mut self, policy: CrashPolicy) {
        self.policy = policy;
    }

    fn check_alive(&self) -> Result<()> {
        if self.crashed {
            Err(IoError::SimulatedCrash)
        } else {
            Ok(())
        }
    }

    fn merge(durable: &mut SimMedia, id: FileId, off: u64, bytes: &[u8]) {
        let v = durable.files.entry(id).or_default();
        let end = off as usize + bytes.len();
        if v.len() < end {
            v.resize(end, 0);
        }
        v[off as usize..end].copy_from_slice(bytes);
    }

    /// Apply the configured crash policy at `point` for a write to `file`.
    /// `seg` is the in-flight write `(offset, bytes)` whose prefix may be
    /// retained as a torn write. Fires at most once per policy instance.
    fn crash(&mut self, point: CrashPoint, file: FileId, seg: Option<(u64, &[u8])>) -> Result<()> {
        if self.policy.fired || self.policy.at != Some(point) {
            return Ok(());
        }
        self.policy.fired = true;
        // What survives: either nothing, or a prefix of the in-flight write.
        if let Some((off, bytes)) = seg {
            if self.policy.torn != Torn::None && !bytes.is_empty() {
                let n = match self.policy.torn {
                    Torn::None => 0,
                    Torn::Half => bytes.len() / 2,
                    Torn::Short => 137.min(bytes.len().saturating_sub(1)),
                };
                if n > 0 {
                    Self::merge(&mut self.durable, file, off, &bytes[..n]);
                }
            }
        }
        // Power is lost: the entire page cache (all dirty segments, of every
        // file) is discarded.
        self.dirty.clear();
        self.crashed = true;
        Err(IoError::SimulatedCrash)
    }
}

impl Vfs for SimVfs {
    fn pwrite(&mut self, id: FileId, offset: u64, data: &[u8]) -> Result<()> {
        self.check_alive()?;
        let begin = match id {
            FileId::Data => CrashPoint::DataWriteBegin,
            _ => CrashPoint::RootWriteBegin,
        };
        // Crash before the first byte hits even the page cache.
        self.crash(begin, id, Some((offset, data)))?;

        self.dirty
            .entry(id)
            .or_default()
            .push((offset, data.to_vec()));

        let end = match id {
            FileId::Data => CrashPoint::DataWriteEnd,
            _ => CrashPoint::RootWriteEnd,
        };
        self.crash(end, id, Some((offset, data)))?;
        Ok(())
    }

    fn sync(&mut self, id: FileId) -> Result<()> {
        self.check_alive()?;
        let begin = match id {
            FileId::Data => CrashPoint::DataSyncBegin,
            _ => CrashPoint::RootSyncBegin,
        };
        // A crash as fsync starts: the latest write may be torn on media,
        // everything else dirty is lost. Extract the last segment first to
        // avoid borrowing `self.dirty` across `&mut self.crash`.
        if self.policy.at == Some(begin) && !self.policy.fired {
            let last = self
                .dirty
                .get(&id)
                .and_then(|s| s.last())
                .map(|(o, b)| (*o, b.clone()));
            if let Some((off, bytes)) = last {
                self.crash(begin, id, Some((off, &bytes[..])))?;
            } else {
                self.crash(begin, id, None)?;
            }
        }

        if let Some(segs) = self.dirty.remove(&id) {
            for (off, bytes) in &segs {
                Self::merge(&mut self.durable, id, *off, bytes);
            }
        }

        let end = match id {
            FileId::Data => CrashPoint::DataSyncEnd,
            _ => CrashPoint::RootSyncEnd,
        };
        self.crash(end, id, None)?;
        Ok(())
    }

    fn sync_dir(&mut self) -> Result<()> {
        self.check_alive()
    }

    fn read(&self, id: FileId) -> Result<Option<Vec<u8>>> {
        self.check_alive()?;
        // Present the page-cache view: durable bytes with unsynced writes
        // overlaid, so a live process observes its own pwrites even before
        // fsync. After a reboot the dirty layer is empty, so reads return
        // exactly what survived.
        let mut merged: Option<Vec<u8>> = self.durable.file_bytes(id).map(|b| b.to_vec());
        if let Some(segs) = self.dirty.get(&id) {
            for (off, bytes) in segs {
                let v = merged.get_or_insert_with(Vec::new);
                let end = *off as usize + bytes.len();
                if v.len() < end {
                    v.resize(end, 0);
                }
                v[*off as usize..end].copy_from_slice(bytes);
            }
        }
        Ok(merged)
    }

    fn truncate(&mut self, id: FileId, len: u64) -> Result<()> {
        self.check_alive()?;
        let v = self.durable.files.entry(id).or_default();
        v.resize(len as usize, 0);
        if v.is_empty() {
            self.durable.files.remove(&id);
        }
        Ok(())
    }

    fn exists(&self, id: FileId) -> bool {
        self.durable.files.contains_key(&id)
    }
}

// Silence the unused-association warning for CrashPoint::file (kept for
// documentation/tests of the mapping).
const _: fn(CrashPoint) -> FileId = CrashPoint::file;

#[cfg(test)]
mod tests {
    use super::*;

    fn policy(at: CrashPoint, torn: Torn) -> CrashPolicy {
        CrashPolicy::new(at, torn)
    }

    #[test]
    fn clean_sync_moves_dirty_to_durable() {
        let no_crash = CrashPolicy::new(CrashPoint::RootSyncEnd, Torn::None);
        let mut v = SimVfs::fresh(no_crash);
        v.pwrite(FileId::Data, 0, b"hello").unwrap();
        assert_eq!(v.durable_len(FileId::Data), 0);
        v.sync(FileId::Data).unwrap();
        assert_eq!(v.durable_bytes(FileId::Data).unwrap(), b"hello");
    }

    #[test]
    fn crash_at_write_end_loses_everything_without_torn() {
        let mut v = SimVfs::fresh(policy(CrashPoint::DataWriteEnd, Torn::None));
        let err = v.pwrite(FileId::Data, 0, b"hello").unwrap_err();
        assert!(matches!(err, IoError::SimulatedCrash));
        let v = v.reopen(policy(CrashPoint::RootSyncEnd, Torn::None));
        assert!(!v.exists(FileId::Data));
    }

    #[test]
    fn torn_half_write_survives_as_prefix() {
        let mut v = SimVfs::fresh(policy(CrashPoint::RootWriteEnd, Torn::Half));
        let page = [0xABu8; 4096];
        let err = v.pwrite(FileId::SbA, 0, &page).unwrap_err();
        assert!(matches!(err, IoError::SimulatedCrash));
        let v = v.reopen(policy(CrashPoint::RootSyncEnd, Torn::None));
        let got = v.durable_bytes(FileId::SbA).unwrap();
        assert_eq!(got.len(), 2048);
        assert!(got.iter().all(|&b| b == 0xAB));
    }
}
