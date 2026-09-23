//! Injectable storage I/O layer.
//!
//! The storage engine never touches `std::fs` directly. It talks to the two
//! traits below, so tests can deterministically inject the two failure modes
//! that matter for durability:
//!
//! * **partial/failed writes** (simulated `ENOSPC`): after a configured total
//!   number of bytes has been written, the tail of a straddling write is
//!   rejected with `ENOSPC` while its fitting prefix is persisted, leaving a
//!   torn record on disk;
//! * **failed `fsync`** (simulated barrier / power failure): the next N sync
//!   calls return `EIO`.
//!
//! All file I/O is positional (`*_at`), so there is no shared cursor per file
//! and no seek state to corrupt. The production backend ([`RealIo`])
//! implements the traits with `std::fs` and the Unix `FileExt` pread/pwrite
//! equivalents.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;

/// A single open file supporting positional reads/writes and durability calls.
pub trait IoFile: Send {
    /// Write all of `buf` at absolute byte `offset`.
    fn write_all_at(&mut self, buf: &[u8], offset: u64) -> io::Result<()>;
    /// Positional read; returns the number of bytes read (0 at EOF).
    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize>;
    /// Flush file data and metadata to stable storage (`fsync`).
    fn sync(&mut self) -> io::Result<()>;
    /// Truncate or extend the file to exactly `len` bytes.
    fn set_len(&mut self, len: u64) -> io::Result<()>;
    /// Current file length in bytes.
    fn len(&self) -> io::Result<u64>;
    /// Whether the file is currently empty.
    fn is_empty(&self) -> io::Result<bool> {
        Ok(self.len()? == 0)
    }
}

/// Opens files and performs namespace operations inside one data directory.
pub trait IoBackend: Send + Sync {
    /// Exclusive create; fails if the path already exists.
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn IoFile>>;
    /// Open an existing file for read + write.
    fn open_write(&self, path: &Path) -> io::Result<Box<dyn IoFile>>;
    /// Open an existing file read-only.
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn IoFile>>;
    /// `fsync` a directory (required to persist a rename/create).
    fn sync_dir(&self, path: &Path) -> io::Result<()>;
    /// Atomically rename `from` to `to` within the filesystem.
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()>;
    /// Read an entire small file.
    fn read_exact_file(&self, path: &Path) -> io::Result<Vec<u8>>;
}

// ---------------------------------------------------------------------------
// Production backend
// ---------------------------------------------------------------------------

#[derive(Debug, Default, Clone)]
pub struct RealIo;

impl RealIo {
    pub fn new() -> Self {
        RealIo
    }
}

struct RealFile {
    f: fs::File,
}

#[cfg(unix)]
use std::os::unix::fs::FileExt;

#[cfg(unix)]
impl IoFile for RealFile {
    fn write_all_at(&mut self, buf: &[u8], offset: u64) -> io::Result<()> {
        self.f.write_all_at(buf, offset)
    }
    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        self.f.read_at(buf, offset)
    }
    fn sync(&mut self) -> io::Result<()> {
        self.f.sync_all()
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        self.f.set_len(len)
    }
    fn len(&self) -> io::Result<u64> {
        Ok(self.f.metadata()?.len())
    }
}

impl IoBackend for RealIo {
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        let f = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(path)?;
        Ok(Box::new(RealFile { f }))
    }

    fn open_write(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        let f = fs::OpenOptions::new().read(true).write(true).open(path)?;
        Ok(Box::new(RealFile { f }))
    }

    fn open_read(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        let f = fs::File::open(path)?;
        Ok(Box::new(RealFile { f }))
    }

    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        fs::File::open(path)?.sync_all()
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        fs::rename(from, to)
    }

    fn read_exact_file(&self, path: &Path) -> io::Result<Vec<u8>> {
        fs::read(path)
    }
}

// ---------------------------------------------------------------------------
// Fault-injecting backend
// ---------------------------------------------------------------------------

/// Raw Linux error numbers so injected failures are indistinguishable from
/// real kernel errors: `ENOSPC` = 28, `EIO` = 5.
pub const ENOSPC: i32 = 28;
pub const EIO: i32 = 5;

/// Mutable fault configuration shared by every file handed out by a
/// [`FaultyIo`] backend. Reconfigure the shared `Arc<FaultRules>` at runtime;
/// that is what the `/dev/fault` HTTP endpoint does.
#[derive(Debug, Default)]
pub struct FaultRules {
    /// Cumulative bytes that writes may cover before failing.
    /// `u64::MAX` means "no limit".
    write_limit: AtomicU64,
    /// Bytes already charged against the limit.
    bytes_written: AtomicU64,
    /// Remaining `sync` calls that fail.
    fail_sync_remaining: AtomicUsize,
}

impl FaultRules {
    pub fn new() -> Self {
        Self {
            write_limit: AtomicU64::new(u64::MAX),
            bytes_written: AtomicU64::new(0),
            fail_sync_remaining: AtomicUsize::new(0),
        }
    }

    /// After `limit` more bytes have been written, writes fail with ENOSPC.
    /// A write straddling the boundary persists its fitting prefix first.
    pub fn fail_writes_after(&self, limit: u64) {
        self.write_limit.store(
            self.bytes_written.load(Ordering::SeqCst) + limit,
            Ordering::SeqCst,
        );
    }

    /// The next `n` `sync` calls fail with EIO.
    pub fn fail_next_syncs(&self, n: usize) {
        self.fail_sync_remaining.store(n, Ordering::SeqCst);
    }

    /// Remove all injected faults (counters for already-spent bytes remain).
    pub fn clear(&self) {
        self.write_limit.store(u64::MAX, Ordering::SeqCst);
        self.fail_sync_remaining.store(0, Ordering::SeqCst);
    }

    pub fn bytes_written(&self) -> u64 {
        self.bytes_written.load(Ordering::SeqCst)
    }
}

/// Backend that delegates to `inner` but injects failures per `rules`.
pub struct FaultyIo {
    inner: Arc<dyn IoBackend>,
    rules: Arc<FaultRules>,
}

impl FaultyIo {
    pub fn new(inner: Arc<dyn IoBackend>, rules: Arc<FaultRules>) -> Self {
        Self { inner, rules }
    }

    pub fn rules(&self) -> Arc<FaultRules> {
        Arc::clone(&self.rules)
    }
}

struct FaultyFile {
    inner: Box<dyn IoFile>,
    rules: Arc<FaultRules>,
}

impl IoFile for FaultyFile {
    fn write_all_at(&mut self, buf: &[u8], offset: u64) -> io::Result<()> {
        let limit = self.rules.write_limit.load(Ordering::SeqCst);
        let used = self.rules.bytes_written.load(Ordering::SeqCst);
        let budget = limit.saturating_sub(used);
        if (buf.len() as u64) <= budget {
            self.inner.write_all_at(buf, offset)?;
            self.rules
                .bytes_written
                .fetch_add(buf.len() as u64, Ordering::SeqCst);
            Ok(())
        } else {
            // Persist the fitting prefix, charge it, then fail like ENOSPC.
            let fit = budget as usize;
            if fit > 0 {
                self.inner.write_all_at(&buf[..fit], offset)?;
                self.rules
                    .bytes_written
                    .fetch_add(fit as u64, Ordering::SeqCst);
            }
            Err(io::Error::from_raw_os_error(ENOSPC))
        }
    }

    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        self.inner.read_at(buf, offset)
    }

    fn sync(&mut self) -> io::Result<()> {
        let mut remaining = self.rules.fail_sync_remaining.load(Ordering::SeqCst);
        while remaining > 0 {
            match self.rules.fail_sync_remaining.compare_exchange(
                remaining,
                remaining - 1,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => return Err(io::Error::from_raw_os_error(EIO)),
                Err(v) => remaining = v,
            }
        }
        self.inner.sync()
    }

    fn set_len(&mut self, len: u64) -> io::Result<()> {
        self.inner.set_len(len)
    }

    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
}

impl IoBackend for FaultyIo {
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        Ok(Box::new(FaultyFile {
            inner: self.inner.create_new(path)?,
            rules: Arc::clone(&self.rules),
        }))
    }
    fn open_write(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        Ok(Box::new(FaultyFile {
            inner: self.inner.open_write(path)?,
            rules: Arc::clone(&self.rules),
        }))
    }
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn IoFile>> {
        self.inner.open_read(path)
    }
    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_dir(path)
    }
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        self.inner.rename(from, to)
    }
    fn read_exact_file(&self, path: &Path) -> io::Result<Vec<u8>> {
        self.inner.read_exact_file(path)
    }
}

/// Build a path inside `dir` (small helper for callers/tests).
pub fn join(dir: &Path, name: &str) -> PathBuf {
    dir.join(name)
}
