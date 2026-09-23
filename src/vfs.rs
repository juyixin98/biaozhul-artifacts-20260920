//! Injectable filesystem I/O layer.
//!
//! All repository disk access goes through the [`Vfs`] trait. Two
//! implementations exist:
//!
//! * [`StdVfs`] — thin wrapper over `std::fs` used by the real server.
//! * [`FaultyVfs`] — wraps any inner Vfs and can be armed at runtime to inject
//!   failures (`io::Error`) or corrupting behavior at specific call sites.
//!   Fault rules are matched against a string *site* name the repository
//!   passes on every call (e.g. `"rename_root"`), so tests can simulate
//!   exactly the failure window "root switch interrupted after the new block
//!   data was written but before the root pointer is published".
//!
//! Synchronization boundary: a Vfs only performs one filesystem operation at
//! a time; multi-operation atomicity (temp-file + fsync + rename) is provided
//! by [`Vfs::write_atomic`]. Cross-process mutual exclusion is out of scope —
//! one repository process owns a data directory.

use std::collections::BTreeMap;
use std::io::{self, Write};
use std::path::Path;
use std::sync::{Arc, Mutex};

/// Errors the I/O layer can surface.
#[derive(Debug)]
pub enum VfsError {
    /// Plain OS-level failure.
    Io(io::Error),
    /// Deliberately injected failure from a [`FaultyVfs`] rule.
    Injected(String),
}

impl VfsError {
    pub fn not_found(msg: &str) -> Self {
        VfsError::Io(io::Error::new(io::ErrorKind::NotFound, msg))
    }

    pub fn is_not_found(&self) -> bool {
        match self {
            VfsError::Io(e) => e.kind() == io::ErrorKind::NotFound,
            VfsError::Injected(_) => false,
        }
    }
}

impl std::fmt::Display for VfsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            VfsError::Io(e) => write!(f, "io error: {e}"),
            VfsError::Injected(s) => write!(f, "injected failure: {s}"),
        }
    }
}

impl std::error::Error for VfsError {}

impl From<io::Error> for VfsError {
    fn from(e: io::Error) -> Self {
        VfsError::Io(e)
    }
}

pub type VfsResult<T> = Result<T, VfsError>;

/// What a fault rule does when triggered.
#[derive(Debug, Clone)]
pub enum FaultAction {
    /// Return an `io::Error` of the given kind (default `Other`) and perform
    /// nothing.
    Fail,
    /// Perform the write but corrupt the bytes at a fixed offset first
    /// (write-only faults; no-op for other operations).
    CorruptByte { offset: u64, xor: u8 },
    /// Perform the operation, then return an error anyway ("write reported a
    /// failure although the data may have landed").
    SucceedThenFail,
}

#[derive(Debug, Clone)]
struct Rule {
    /// Remaining trigger count; `None` = unlimited.
    remaining: Option<usize>,
    action: FaultAction,
}

/// Thread-safe shared fault configuration.
#[derive(Clone, Default)]
pub struct FaultMap {
    inner: Arc<Mutex<BTreeMap<String, Rule>>>,
}

impl FaultMap {
    pub fn new() -> Self {
        Self::default()
    }

    /// Arm a fault at `site`: the next `times` matching calls (or every call
    /// when `times` is `None`) take `action`.
    pub fn arm(&self, site: impl Into<String>, action: FaultAction, times: Option<usize>) {
        self.inner.lock().unwrap().insert(
            site.into(),
            Rule {
                remaining: times,
                action,
            },
        );
    }

    pub fn fail_once(&self, site: impl Into<String>) {
        self.arm(site, FaultAction::Fail, Some(1));
    }

    pub fn clear(&self, site: &str) {
        self.inner.lock().unwrap().remove(site);
    }

    pub fn clear_all(&self) {
        self.inner.lock().unwrap().clear();
    }

    /// Atomically consume one trigger at `site`, returning the action to run.
    fn consume(&self, site: &str) -> Option<FaultAction> {
        let mut map = self.inner.lock().unwrap();
        let rule = map.get_mut(site)?;
        if let Some(left) = &mut rule.remaining {
            if *left == 0 {
                return None;
            }
            *left -= 1;
        }
        let action = rule.action.clone();
        if let Some(left) = rule.remaining {
            if left == 0 {
                map.remove(site);
            }
        }
        Some(action)
    }
}

/// The injectable filesystem abstraction. Every `site` argument names the
/// logical call site for fault matching and is ignored by [`StdVfs`].
pub trait Vfs: Send + Sync {
    fn create_dir_all(&self, path: &Path, site: &str) -> VfsResult<()>;
    fn read(&self, path: &Path, site: &str) -> VfsResult<Vec<u8>>;
    fn exists(&self, path: &Path, site: &str) -> VfsResult<bool>;
    fn remove_file(&self, path: &Path, site: &str) -> VfsResult<()>;
    fn rename(&self, from: &Path, to: &Path, site: &str) -> VfsResult<()>;
    /// Create `path`, write `data`, fsync the file. The file is left on disk.
    fn write_temp(&self, path: &Path, data: &[u8], site: &str) -> VfsResult<()>;
    fn list_dir(&self, path: &Path, site: &str) -> VfsResult<Vec<String>>;

    /// Stage `data` at `temp`, then atomically rename onto `target`. On a
    /// rename failure the temp file is removed (best effort) — a real crash
    /// leaves it behind, which is what [`crate::vfs::FaultyVfs`] simulates.
    fn write_temp_and_rename(
        &self,
        temp: &Path,
        target: &Path,
        data: &[u8],
        site: &str,
    ) -> VfsResult<()> {
        self.write_temp(temp, data, &format!("{site}_tmp"))?;
        if let Err(e) = self.rename(temp, target, site) {
            let _ = self.remove_file(temp, "cleanup_temp");
            return Err(e);
        }
        // Best-effort durability of the directory entry itself.
        if let Ok(dir) = std::fs::File::open(target.parent().unwrap_or(Path::new("."))) {
            let _ = dir.sync_all();
        }
        Ok(())
    }

    /// Write `data` to `target` via a sibling temp file, fsync, then atomic
    /// rename. `site` names the *rename* step; the temp-write step uses
    /// `"{site}_tmp"`.
    fn write_atomic(&self, target: &Path, data: &[u8], site: &str) -> VfsResult<()> {
        let name = target.file_name().and_then(|n| n.to_str()).unwrap_or("tmp");
        let temp = target.with_file_name(format!(".{name}.tmp-{:x}", pseudo_random()));
        self.write_temp_and_rename(&temp, target, data, site)
    }
}

/// Cheap non-cryptographic entropy for unique temp suffixes (PIDs collide in
/// tests that run many repos in one process).
fn pseudo_random() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    COUNTER.fetch_add(1, Ordering::Relaxed)
        ^ std::process::id() as u64
        ^ (std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.subsec_nanos() as u64)
            .unwrap_or(0))
}

// ---------------- StdVfs ----------------

pub struct StdVfs;

impl StdVfs {
    pub fn new() -> Self {
        StdVfs
    }
}

impl Default for StdVfs {
    fn default() -> Self {
        Self::new()
    }
}

impl Vfs for StdVfs {
    fn create_dir_all(&self, path: &Path, _site: &str) -> VfsResult<()> {
        std::fs::create_dir_all(path)?;
        Ok(())
    }

    fn read(&self, path: &Path, _site: &str) -> VfsResult<Vec<u8>> {
        Ok(std::fs::read(path)?)
    }

    fn exists(&self, path: &Path, _site: &str) -> VfsResult<bool> {
        Ok(path.exists())
    }

    fn remove_file(&self, path: &Path, _site: &str) -> VfsResult<()> {
        std::fs::remove_file(path)?;
        Ok(())
    }

    fn rename(&self, from: &Path, to: &Path, _site: &str) -> VfsResult<()> {
        std::fs::rename(from, to)?;
        Ok(())
    }

    fn write_temp(&self, path: &Path, data: &[u8], _site: &str) -> VfsResult<()> {
        let mut f = std::fs::File::create(path)?;
        f.write_all(data)?;
        f.sync_all()?;
        Ok(())
    }

    fn list_dir(&self, path: &Path, _site: &str) -> VfsResult<Vec<String>> {
        let mut out = Vec::new();
        if !path.exists() {
            return Ok(out);
        }
        for e in std::fs::read_dir(path)? {
            let e = e?;
            if let Some(n) = e.file_name().to_str() {
                out.push(n.to_string());
            }
        }
        Ok(out)
    }
}

// ---------------- FaultyVfs ----------------

/// Wraps another [`Vfs`] and applies rules from a shared [`FaultMap`].
pub struct FaultyVfs {
    inner: Box<dyn Vfs>,
    faults: FaultMap,
}

impl FaultyVfs {
    pub fn new(inner: Box<dyn Vfs>, faults: FaultMap) -> Self {
        FaultyVfs { inner, faults }
    }

    pub fn faults(&self) -> &FaultMap {
        &self.faults
    }

    fn injected(name: &str) -> VfsError {
        VfsError::Injected(name.to_string())
    }
}

impl Vfs for FaultyVfs {
    fn create_dir_all(&self, path: &Path, site: &str) -> VfsResult<()> {
        if let Some(action) = self.faults.consume(site) {
            if matches!(action, FaultAction::Fail) {
                return Err(Self::injected(site));
            }
        }
        self.inner.create_dir_all(path, site)
    }

    fn read(&self, path: &Path, site: &str) -> VfsResult<Vec<u8>> {
        if let Some(action) = self.faults.consume(site) {
            if matches!(action, FaultAction::Fail) {
                return Err(Self::injected(site));
            }
        }
        self.inner.read(path, site)
    }

    fn exists(&self, path: &Path, site: &str) -> VfsResult<bool> {
        if let Some(action) = self.faults.consume(site) {
            if matches!(action, FaultAction::Fail) {
                return Err(Self::injected(site));
            }
        }
        self.inner.exists(path, site)
    }

    fn remove_file(&self, path: &Path, site: &str) -> VfsResult<()> {
        if let Some(action) = self.faults.consume(site) {
            if matches!(action, FaultAction::Fail) {
                return Err(Self::injected(site));
            }
        }
        self.inner.remove_file(path, site)
    }

    fn rename(&self, from: &Path, to: &Path, site: &str) -> VfsResult<()> {
        match self.faults.consume(site) {
            Some(FaultAction::Fail) => Err(Self::injected(site)),
            Some(FaultAction::SucceedThenFail) => {
                self.inner.rename(from, to, site)?;
                Err(Self::injected(site))
            }
            _ => self.inner.rename(from, to, site),
        }
    }

    fn write_temp(&self, path: &Path, data: &[u8], site: &str) -> VfsResult<()> {
        match self.faults.consume(site) {
            Some(FaultAction::Fail) => Err(Self::injected(site)),
            Some(FaultAction::CorruptByte { offset, xor }) => {
                let mut owned = data.to_vec();
                if !owned.is_empty() {
                    let off = (offset as usize).min(owned.len() - 1);
                    owned[off] ^= xor;
                }
                self.inner.write_temp(path, &owned, site)
            }
            Some(FaultAction::SucceedThenFail) => {
                self.inner.write_temp(path, data, site)?;
                Err(Self::injected(site))
            }
            None => self.inner.write_temp(path, data, site),
        }
    }

    fn write_temp_and_rename(
        &self,
        temp: &Path,
        target: &Path,
        data: &[u8],
        site: &str,
    ) -> VfsResult<()> {
        // Temp-write-phase fault: corrupt or fail before anything is staged.
        let mut owned: Vec<u8>;
        let data = match self.faults.consume(&format!("{site}_tmp")) {
            Some(FaultAction::Fail) => return Err(Self::injected(site)),
            Some(FaultAction::CorruptByte { offset, xor }) => {
                owned = data.to_vec();
                if !owned.is_empty() {
                    let off = (offset as usize).min(owned.len() - 1);
                    owned[off] ^= xor;
                }
                owned
            }
            Some(FaultAction::SucceedThenFail) => {
                self.inner.write_temp_and_rename(temp, target, data, site)?;
                return Err(Self::injected(site));
            }
            None => data.to_vec(),
        };
        // Rename-phase fault.
        match self.faults.consume(site) {
            Some(FaultAction::Fail) => {
                // Crash after staging: the temp file is really written and
                // fsynced, the rename never happens, and nothing cleans up —
                // the process "died" mid-publish.
                self.inner.write_temp(temp, &data, &format!("{site}_tmp"))?;
                Err(Self::injected(site))
            }
            Some(FaultAction::SucceedThenFail) => {
                self.inner
                    .write_temp_and_rename(temp, target, &data, site)?;
                Err(Self::injected(site))
            }
            Some(FaultAction::CorruptByte { offset, xor }) => {
                let mut owned2 = data;
                if !owned2.is_empty() {
                    let off = (offset as usize).min(owned2.len() - 1);
                    owned2[off] ^= xor;
                }
                self.inner
                    .write_temp_and_rename(temp, target, &owned2, site)
            }
            None => self.inner.write_temp_and_rename(temp, target, &data, site),
        }
    }

    fn list_dir(&self, path: &Path, site: &str) -> VfsResult<Vec<String>> {
        if let Some(action) = self.faults.consume(site) {
            if matches!(action, FaultAction::Fail) {
                return Err(Self::injected(site));
            }
        }
        self.inner.list_dir(path, site)
    }
}

/// Helper used in tests: append bytes to a file bypassing the Vfs.
#[allow(dead_code)]
pub fn append_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    f.write_all(bytes)
}

// `Box<dyn Vfs>` forwards verbatim so generic and object-safe stores share
// the same code paths.
impl Vfs for Box<dyn Vfs> {
    fn create_dir_all(&self, path: &Path, site: &str) -> VfsResult<()> {
        (**self).create_dir_all(path, site)
    }
    fn read(&self, path: &Path, site: &str) -> VfsResult<Vec<u8>> {
        (**self).read(path, site)
    }
    fn exists(&self, path: &Path, site: &str) -> VfsResult<bool> {
        (**self).exists(path, site)
    }
    fn remove_file(&self, path: &Path, site: &str) -> VfsResult<()> {
        (**self).remove_file(path, site)
    }
    fn rename(&self, from: &Path, to: &Path, site: &str) -> VfsResult<()> {
        (**self).rename(from, to, site)
    }
    fn write_temp(&self, path: &Path, data: &[u8], site: &str) -> VfsResult<()> {
        (**self).write_temp(path, data, site)
    }
    fn write_temp_and_rename(
        &self,
        temp: &Path,
        target: &Path,
        data: &[u8],
        site: &str,
    ) -> VfsResult<()> {
        (**self).write_temp_and_rename(temp, target, data, site)
    }
    fn list_dir(&self, path: &Path, site: &str) -> VfsResult<Vec<String>> {
        (**self).list_dir(path, site)
    }
}
