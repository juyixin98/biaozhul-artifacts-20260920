//! Injectable I/O layer.
//!
//! Everything that touches the filesystem goes through the [`Io`] trait. The
//! production implementation is [`StdIo`]; tests substitute fault-injecting
//! implementations ([`FaultIo`], [`SimCrashIo`]) to exercise I/O errors and
//! simulated power loss without changing engine code.

use std::collections::HashMap;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

/// A seekable, readable, writable, syncable file handle.
pub trait IoFile: Read + Write + Seek + Send {
    fn sync_all(&mut self) -> io::Result<()>;
}

/// Filesystem operations the store needs. Every method takes an absolute or
/// store-relative path; [`SimCrashIo`] keys its sync tracking on the canonical
/// path string.
pub trait Io: Send + Sync {
    /// Open an existing file read/write, creating it if it does not exist.
    fn open_or_create(&self, path: &Path) -> io::Result<Box<dyn IoFile>>;
    /// True if a file exists at `path`.
    fn exists(&self, path: &Path) -> io::Result<bool>;
    /// Atomic-ish rename (`rename(2)`).
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()>;
    /// Remove a file.
    fn remove(&self, path: &Path) -> io::Result<()>;
    /// Truncate (or extend) a file to exactly `len` bytes.
    fn truncate_file(&self, path: &Path, len: u64) -> io::Result<()>;
    /// fsync a directory (required to persist a rename).
    fn sync_dir(&self, path: &Path) -> io::Result<()>;
    /// Read an entire file into a Vec.
    fn read_file(&self, path: &Path) -> io::Result<Vec<u8>>;
    /// Best-effort unique temporary path next to `base`.
    fn temp_path(&self, base: &Path) -> PathBuf;
}

/// Standard filesystem I/O.
#[derive(Clone)]
pub struct StdIo;

struct StdFile(fs::File);

impl Read for StdFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}
impl Write for StdFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}
impl Seek for StdFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        self.0.seek(pos)
    }
}
impl IoFile for StdFile {
    fn sync_all(&mut self) -> io::Result<()> {
        self.0.sync_all()
    }
}

impl Io for StdIo {
    fn open_or_create(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        let f = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        Ok(Box::new(StdFile(f)))
    }

    fn exists(&self, path: &Path) -> io::Result<bool> {
        Ok(path.exists())
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        fs::rename(from, to)
    }

    fn remove(&self, path: &Path) -> io::Result<()> {
        fs::remove_file(path)
    }

    fn truncate_file(&self, path: &Path, len: u64) -> io::Result<()> {
        let f = fs::OpenOptions::new().write(true).open(path)?;
        f.set_len(len)?;
        f.sync_all()
    }

    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        let f = fs::File::open(path)?;
        f.sync_all()
    }

    fn read_file(&self, path: &Path) -> io::Result<Vec<u8>> {
        fs::read(path)
    }

    fn temp_path(&self, base: &Path) -> PathBuf {
        let n = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        // Keep the full original name (e.g. mvcc.log) and append the suffix,
        // so the result is `mvcc.log.tmp.<pid>.<nanos>`, not `mvcc.<ext>`.
        let name = base
            .file_name()
            .map(|x| x.to_string_lossy().into_owned())
            .unwrap_or_else(|| "file".to_string());
        base.with_file_name(format!("{name}.tmp.{}.{:x}", std::process::id(), n))
    }
}

/// Which injectable operation should fail.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultKind {
    Open,
    Write,
    Sync,
    Rename,
}

/// One programmed failure: after `at_least` matching operations have been
/// observed, return an I/O error for `times` consecutive matches.
#[derive(Clone)]
pub struct FaultRule {
    pub kind: FaultKind,
    pub at_least: u64,
    pub times: u32,
    /// Optional path substring filter (matches on the path string).
    pub path_contains: Option<String>,
}

impl FaultRule {
    pub fn new(kind: FaultKind, at_least: u64) -> Self {
        FaultRule {
            kind,
            at_least,
            times: 1,
            path_contains: None,
        }
    }
}

#[derive(Default)]
struct FaultState {
    counts: HashMap<&'static str, u64>,
    fired: u32,
}

/// Wraps another [`Io`] and injects errors according to [`FaultRule`]s.
/// Shared (cheaply cloneable) so all handles in one process see one counter.
#[derive(Clone)]
pub struct FaultIo {
    inner: Arc<dyn Io>,
    rules: Arc<Vec<FaultRule>>,
    state: Arc<Mutex<FaultState>>,
}

impl FaultIo {
    pub fn new(inner: Arc<dyn Io>, rules: Vec<FaultRule>) -> Self {
        FaultIo {
            inner,
            rules: Arc::new(rules),
            state: Arc::new(Mutex::new(FaultState::default())),
        }
    }

    fn check(&self, kind: FaultKind, path: Option<&Path>) -> io::Result<()> {
        let key = match kind {
            FaultKind::Open => "open",
            FaultKind::Write => "write",
            FaultKind::Sync => "sync",
            FaultKind::Rename => "rename",
        };
        let mut st = self.state.lock().unwrap();
        let seen = *st.counts.get(key).unwrap_or(&0) + 1;
        st.counts.insert(key, seen);
        for r in self.rules.iter() {
            if r.kind != kind || seen < r.at_least {
                continue;
            }
            if let Some(needle) = &r.path_contains {
                match path {
                    Some(p) if p.to_string_lossy().contains(needle) => {}
                    Some(_) => continue,
                    None => continue,
                }
            }
            if st.fired < r.times {
                st.fired += 1;
                return Err(io::Error::other(format!(
                    "injected I/O fault: {:?} (#{})",
                    kind, seen
                )));
            }
        }
        Ok(())
    }
}

impl Io for FaultIo {
    fn open_or_create(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        self.check(FaultKind::Open, Some(path))?;
        let f = self.inner.open_or_create(path)?;
        Ok(Box::new(FaultFile {
            inner: f,
            io: self.clone(),
            path: path.to_path_buf(),
        }))
    }
    fn exists(&self, path: &Path) -> io::Result<bool> {
        self.inner.exists(path)
    }
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        self.check(FaultKind::Rename, Some(from))?;
        self.inner.rename(from, to)
    }
    fn remove(&self, path: &Path) -> io::Result<()> {
        self.inner.remove(path)
    }
    fn truncate_file(&self, path: &Path, len: u64) -> io::Result<()> {
        // Recovery truncation is not fault-injected.
        self.inner.truncate_file(path, len)
    }
    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_dir(path)
    }
    fn read_file(&self, path: &Path) -> io::Result<Vec<u8>> {
        self.inner.read_file(path)
    }
    fn temp_path(&self, base: &Path) -> PathBuf {
        self.inner.temp_path(base)
    }
}

struct FaultFile {
    inner: Box<dyn IoFile>,
    io: FaultIo,
    path: PathBuf,
}

impl Read for FaultFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.inner.read(buf)
    }
}
impl Write for FaultFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.io.check(FaultKind::Write, Some(&self.path))?;
        self.inner.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}
impl Seek for FaultFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        self.inner.seek(pos)
    }
}
impl IoFile for FaultFile {
    fn sync_all(&mut self) -> io::Result<()> {
        self.io.check(FaultKind::Sync, Some(&self.path))?;
        self.inner.sync_all()
    }
}

/// Simulated power loss.
///
/// Bytes written (but not yet `sync_all`'d) are kept in an in-memory overlay.
/// On [`SimCrashIo::crash`] every file is truncated back to the length it had
/// at its last successful sync, discarding unsynced writes exactly like losing
/// the page cache after power failure. Handles remain "open" across the crash
/// (the real OS keeps file descriptors), but their overlays are dropped.
#[derive(Clone)]
pub struct SimCrashIo {
    inner: Arc<dyn Io>,
    syncs: Arc<Mutex<HashMap<PathBuf, u64>>>,
}

impl SimCrashIo {
    pub fn new(inner: Arc<dyn Io>) -> Self {
        SimCrashIo {
            inner,
            syncs: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Discard all unsynced bytes in every tracked file.
    pub fn crash(&self) {
        let mut syncs = self.syncs.lock().unwrap();
        for (path, synced_len) in syncs.iter() {
            // Writes reach the underlying (simulated page cache) immediately;
            // truncating back to the last synced length models losing them.
            if let Ok(rf) = fs::OpenOptions::new().write(true).open(path) {
                let _ = rf.set_len(*synced_len);
            }
        }
        syncs.clear();
    }

    fn note_synced(&self, path: &Path, len: u64) {
        self.syncs
            .lock()
            .unwrap()
            .insert(path.to_path_buf(), len);
    }
}

impl Io for SimCrashIo {
    fn open_or_create(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        let f = self.inner.open_or_create(path)?;
        Ok(Box::new(CrashFile {
            inner: f,
            io: self.clone(),
            path: path.to_path_buf(),
        }))
    }
    fn exists(&self, path: &Path) -> io::Result<bool> {
        self.inner.exists(path)
    }
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        self.inner.rename(from, to)?;
        let mut syncs = self.syncs.lock().unwrap();
        if let Some(len) = syncs.remove(from) {
            syncs.insert(to.to_path_buf(), len);
        }
        Ok(())
    }
    fn remove(&self, path: &Path) -> io::Result<()> {
        self.syncs.lock().unwrap().remove(path);
        self.inner.remove(path)
    }
    fn truncate_file(&self, path: &Path, len: u64) -> io::Result<()> {
        self.inner.truncate_file(path, len)?;
        self.syncs
            .lock()
            .unwrap()
            .insert(path.to_path_buf(), len);
        Ok(())
    }
    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        // Directory sync is meaningful for rename durability; in the
        // simulator we immediately apply renames anyway, so this is a no-op.
        let _ = path;
        Ok(())
    }
    fn read_file(&self, path: &Path) -> io::Result<Vec<u8>> {
        self.inner.read_file(path)
    }
    fn temp_path(&self, base: &Path) -> PathBuf {
        self.inner.temp_path(base)
    }
}

struct CrashFile {
    inner: Box<dyn IoFile>,
    io: SimCrashIo,
    path: PathBuf,
}

impl Read for CrashFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.inner.read(buf)
    }
}
impl Write for CrashFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.inner.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}
impl Seek for CrashFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        self.inner.seek(pos)
    }
}
impl IoFile for CrashFile {
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()?;
        let len = self.inner.seek(SeekFrom::End(0))?;
        self.io.note_synced(&self.path, len);
        Ok(())
    }
}
