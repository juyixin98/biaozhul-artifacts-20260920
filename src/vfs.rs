//! Injectable I/O layer.
//!
//! All repository disk access goes through the [`Vfs`] trait:
//! - [`RealVfs`] performs ordinary filesystem operations (with fsync and
//!   atomic temp-file renames for the durability/sync boundaries);
//! - [`MemVfs`] is an in-memory filesystem used by tests;
//! - [`FaultyVfs`] wraps either implementation and injects I/O errors or byte
//!   corruption on a selected call number, so crash/torn-write recovery can be
//!   exercised without real disks.

use std::collections::BTreeMap;
use std::io::{self, Read as _, Seek as _, SeekFrom, Write as _};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

pub trait Vfs: Send + Sync {
    /// Ensure a directory exists.
    fn mkdir(&self, path: &Path) -> io::Result<()>;
    fn exists(&self, path: &Path) -> io::Result<bool>;
    fn read(&self, path: &Path) -> io::Result<Vec<u8>>;
    /// Overwrite atomically: write `tmp`, fsync, rename over target, fsync dir.
    fn write_atomic(&self, path: &Path, data: &[u8]) -> io::Result<()>;
    fn remove(&self, path: &Path) -> io::Result<()>;
    fn len(&self, path: &Path) -> io::Result<u64>;
    fn read_at(&self, path: &Path, offset: u64, buf: &mut [u8]) -> io::Result<usize>;
    /// Create if missing (zero-filled) and overwrite bytes at `offset`.
    fn write_at(&self, path: &Path, offset: u64, data: &[u8]) -> io::Result<()>;
    fn truncate(&self, path: &Path, len: u64) -> io::Result<()>;
    fn sync(&self, path: &Path) -> io::Result<()>;
}

// ---------------------------------------------------------------------------
// Real filesystem.
// ---------------------------------------------------------------------------

pub struct RealVfs;

impl Clone for RealVfs {
    fn clone(&self) -> Self {
        *self
    }
}

impl Copy for RealVfs {}

impl RealVfs {
    pub fn new() -> Self {
        RealVfs
    }
}

impl Default for RealVfs {
    fn default() -> Self {
        Self::new()
    }
}

fn sync_dir(path: &Path) -> io::Result<()> {
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    let f = std::fs::OpenOptions::new().read(true).open(dir)?;
    f.sync_all()
}

impl Vfs for RealVfs {
    fn mkdir(&self, path: &Path) -> io::Result<()> {
        std::fs::create_dir_all(path)
    }

    fn exists(&self, path: &Path) -> io::Result<bool> {
        Ok(path.exists())
    }

    fn read(&self, path: &Path) -> io::Result<Vec<u8>> {
        std::fs::read(path)
    }

    fn write_atomic(&self, path: &Path, data: &[u8]) -> io::Result<()> {
        let mut tmp = PathBuf::from(path);
        let file_name = tmp
            .file_name()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "bad path"))?
            .to_owned();
        tmp.set_file_name(format!(
            ".{}.tmp-{}",
            file_name.to_string_lossy(),
            std::process::id()
        ));
        {
            let mut f = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&tmp)?;
            f.write_all(data)?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, path)?;
        sync_dir(path)
    }

    fn remove(&self, path: &Path) -> io::Result<()> {
        std::fs::remove_file(path)
    }

    fn len(&self, path: &Path) -> io::Result<u64> {
        Ok(std::fs::metadata(path)?.len())
    }

    fn read_at(&self, path: &Path, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        let mut f = std::fs::OpenOptions::new().read(true).open(path)?;
        f.seek(SeekFrom::Start(offset))?;
        f.read(buf)
    }

    fn write_at(&self, path: &Path, offset: u64, data: &[u8]) -> io::Result<()> {
        // pwrite-style patch: preserve all existing bytes outside the range.
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        f.seek(SeekFrom::Start(offset))?;
        f.write_all(data)?;
        f.sync_all()
    }

    fn truncate(&self, path: &Path, len: u64) -> io::Result<()> {
        let f = std::fs::OpenOptions::new().write(true).open(path)?;
        f.set_len(len)?;
        f.sync_all()
    }

    fn sync(&self, path: &Path) -> io::Result<()> {
        let f = std::fs::OpenOptions::new().write(true).open(path)?;
        f.sync_all()
    }
}

// ---------------------------------------------------------------------------
// In-memory filesystem.
// ---------------------------------------------------------------------------

#[derive(Default)]
struct MemInner {
    files: BTreeMap<PathBuf, Vec<u8>>,
}

#[derive(Clone, Default)]
pub struct MemVfs {
    inner: Arc<Mutex<MemInner>>,
}

impl MemVfs {
    pub fn new() -> Self {
        Self::default()
    }

    /// Test helper: raw snapshot of a file (or an empty buffer if absent).
    pub fn snapshot(&self, path: &Path) -> Vec<u8> {
        self.inner
            .lock()
            .unwrap()
            .files
            .get(path)
            .cloned()
            .unwrap_or_default()
    }
}

impl Vfs for MemVfs {
    fn mkdir(&self, _path: &Path) -> io::Result<()> {
        Ok(())
    }

    fn exists(&self, path: &Path) -> io::Result<bool> {
        Ok(self.inner.lock().unwrap().files.contains_key(path))
    }

    fn read(&self, path: &Path) -> io::Result<Vec<u8>> {
        match self.inner.lock().unwrap().files.get(path) {
            Some(d) => Ok(d.clone()),
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                "memfs: no such file",
            )),
        }
    }

    fn write_atomic(&self, path: &Path, data: &[u8]) -> io::Result<()> {
        let mut g = self.inner.lock().unwrap();
        g.files.insert(path.to_path_buf(), data.to_vec());
        Ok(())
    }

    fn remove(&self, path: &Path) -> io::Result<()> {
        let mut g = self.inner.lock().unwrap();
        match g.files.remove(path) {
            Some(_) => Ok(()),
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                "memfs: no such file",
            )),
        }
    }

    fn len(&self, path: &Path) -> io::Result<u64> {
        match self.inner.lock().unwrap().files.get(path) {
            Some(d) => Ok(d.len() as u64),
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                "memfs: no such file",
            )),
        }
    }

    fn read_at(&self, path: &Path, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        let g = self.inner.lock().unwrap();
        match g.files.get(path) {
            Some(d) => {
                let off = offset as usize;
                if off >= d.len() {
                    return Ok(0);
                }
                let n = buf.len().min(d.len() - off);
                buf[..n].copy_from_slice(&d[off..off + n]);
                Ok(n)
            }
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                "memfs: no such file",
            )),
        }
    }

    fn write_at(&self, path: &Path, offset: u64, data: &[u8]) -> io::Result<()> {
        let mut g = self.inner.lock().unwrap();
        let entry = g.files.entry(path.to_path_buf()).or_default();
        let end = offset as usize + data.len();
        if end > entry.len() {
            entry.resize(end, 0);
        }
        entry[offset as usize..end].copy_from_slice(data);
        Ok(())
    }

    fn truncate(&self, path: &Path, len: u64) -> io::Result<()> {
        let mut g = self.inner.lock().unwrap();
        match g.files.get_mut(path) {
            Some(d) => {
                d.resize(len as usize, 0);
                Ok(())
            }
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                "memfs: no such file",
            )),
        }
    }

    fn sync(&self, _path: &Path) -> io::Result<()> {
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Fault-injecting wrapper.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Op {
    Mkdir,
    Exists,
    Read,
    WriteAtomic,
    Remove,
    Len,
    ReadAt,
    WriteAt,
    Truncate,
    Sync,
}

#[derive(Debug, Clone)]
enum Effect {
    /// Fail the nth matching call with an io::Error.
    Fail(u64),
    /// Flip the first byte written by the nth matching call (torn write).
    Corrupt(u64),
}

#[derive(Clone)]
struct Rule {
    op: Op,
    path_contains: Option<String>,
    effect: Effect,
    fired: bool,
    /// Count of calls that matched this rule (1-based at fire time).
    count: u64,
}

fn injected_err(op: Op, path: &Path) -> io::Error {
    io::Error::other(format!("injected {op:?} fault on {}", path.display()))
}

/// Wraps a [`Vfs`]; each rule fires at most once. Typical test use:
/// `FaultyVfs::new(inner).fail_on(Op::WriteAtomic, Some("manifest"), 3)`.
///
/// The `nth` counter counts only calls that match the rule (same op and, if
/// given, path substring), independently for every rule.
pub struct FaultyVfs<V: Vfs> {
    inner: V,
    rules: Arc<Mutex<Vec<Rule>>>,
    /// Independent invocation log: (op, path) -> count.
    all_calls: Arc<Mutex<Vec<(Op, String, u64)>>>,
}

impl<V: Vfs + Clone> Clone for FaultyVfs<V> {
    /// Cloning shares the fault rules and call counters (and clones the
    /// underlying VFS handle — for `Arc`-backed VFSes like [`MemVfs`] this
    /// shares the same backing store).
    fn clone(&self) -> Self {
        FaultyVfs {
            inner: self.inner.clone(),
            rules: Arc::clone(&self.rules),
            all_calls: Arc::clone(&self.all_calls),
        }
    }
}

impl<V: Vfs> FaultyVfs<V> {
    pub fn new(inner: V) -> Self {
        FaultyVfs {
            inner,
            rules: Arc::new(Mutex::new(Vec::new())),
            all_calls: Arc::new(Mutex::new(Vec::new())),
        }
    }

    pub fn fail_on(self, op: Op, path_contains: Option<&str>, nth: u64) -> Self {
        self.rules.lock().unwrap().push(Rule {
            op,
            path_contains: path_contains.map(str::to_string),
            effect: Effect::Fail(nth),
            fired: false,
            count: 0,
        });
        self
    }

    pub fn corrupt_on(self, op: Op, path_contains: Option<&str>, nth: u64) -> Self {
        self.rules.lock().unwrap().push(Rule {
            op,
            path_contains: path_contains.map(str::to_string),
            effect: Effect::Corrupt(nth),
            fired: false,
            count: 0,
        });
        self
    }

    /// True once every rule has triggered.
    pub fn all_fired(&self) -> bool {
        self.rules.lock().unwrap().iter().all(|r| r.fired)
    }

    /// Number of observed calls matching `op` whose path contains `needle`
    /// (empty needle matches all paths). Independent of configured rules.
    pub fn call_count(&self, op: Op, needle: &str) -> u64 {
        self.all_calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(o, p, _)| *o == op && p.contains(needle))
            .map(|(_, _, c)| *c)
            .sum()
    }

    /// Record one call and return the effect whose turn it is (fired once).
    fn fire(&self, op: Op, path: &Path) -> Option<Effect> {
        let pstr = path.to_string_lossy().into_owned();
        {
            let mut calls = self.all_calls.lock().unwrap();
            match calls.iter_mut().find(|(o, p, _)| *o == op && *p == pstr) {
                Some((_, _, c)) => *c += 1,
                None => calls.push((op, pstr.clone(), 1)),
            }
        }
        let mut rules = self.rules.lock().unwrap();
        for r in rules.iter_mut() {
            if r.fired || r.op != op {
                continue;
            }
            if let Some(needle) = &r.path_contains {
                if !pstr.contains(needle) {
                    continue;
                }
            }
            r.count += 1;
            let nth = match r.effect {
                Effect::Fail(n) | Effect::Corrupt(n) => n,
            };
            if nth == r.count {
                r.fired = true;
                return Some(r.effect.clone());
            }
        }
        None
    }

    /// Gate for non-write operations: only Fail matters.
    fn gate(&self, op: Op, path: &Path) -> io::Result<()> {
        match self.fire(op, path) {
            Some(Effect::Fail(_)) => Err(injected_err(op, path)),
            _ => Ok(()),
        }
    }

    /// Gate for write operations: Fail errors, Corrupt flips the first byte.
    fn gate_write(&self, op: Op, path: &Path, data: &[u8]) -> io::Result<Vec<u8>> {
        match self.fire(op, path) {
            Some(Effect::Fail(_)) => Err(injected_err(op, path)),
            Some(Effect::Corrupt(_)) if !data.is_empty() => {
                let mut buf = data.to_vec();
                buf[0] ^= 0xff;
                Ok(buf)
            }
            _ => Ok(data.to_vec()),
        }
    }
}

impl<V: Vfs> Vfs for FaultyVfs<V> {
    fn mkdir(&self, path: &Path) -> io::Result<()> {
        self.gate(Op::Mkdir, path)?;
        self.inner.mkdir(path)
    }

    fn exists(&self, path: &Path) -> io::Result<bool> {
        self.gate(Op::Exists, path)?;
        self.inner.exists(path)
    }

    fn read(&self, path: &Path) -> io::Result<Vec<u8>> {
        self.gate(Op::Read, path)?;
        self.inner.read(path)
    }

    fn write_atomic(&self, path: &Path, data: &[u8]) -> io::Result<()> {
        let buf = self.gate_write(Op::WriteAtomic, path, data)?;
        self.inner.write_atomic(path, &buf)
    }

    fn remove(&self, path: &Path) -> io::Result<()> {
        self.gate(Op::Remove, path)?;
        self.inner.remove(path)
    }

    fn len(&self, path: &Path) -> io::Result<u64> {
        self.gate(Op::Len, path)?;
        self.inner.len(path)
    }

    fn read_at(&self, path: &Path, offset: u64, buf: &mut [u8]) -> io::Result<usize> {
        self.gate(Op::ReadAt, path)?;
        self.inner.read_at(path, offset, buf)
    }

    fn write_at(&self, path: &Path, offset: u64, data: &[u8]) -> io::Result<()> {
        let buf = self.gate_write(Op::WriteAt, path, data)?;
        self.inner.write_at(path, offset, &buf)
    }

    fn truncate(&self, path: &Path, len: u64) -> io::Result<()> {
        self.gate(Op::Truncate, path)?;
        self.inner.truncate(path, len)
    }

    fn sync(&self, path: &Path) -> io::Result<()> {
        self.gate(Op::Sync, path)?;
        self.inner.sync(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memfs_write_read_and_holes() {
        let fs = MemVfs::new();
        fs.write_at(Path::new("f"), 0, b"hello").unwrap();
        fs.write_at(Path::new("f"), 10, b"xyz").unwrap();
        assert_eq!(fs.len(Path::new("f")).unwrap(), 13);
        let d = fs.read(Path::new("f")).unwrap();
        assert_eq!(&d[..5], b"hello");
        assert_eq!(&d[5..10], &[0; 5]);
        assert_eq!(&d[10..], b"xyz");
        fs.truncate(Path::new("f"), 6).unwrap();
        assert_eq!(fs.len(Path::new("f")).unwrap(), 6);
    }

    #[test]
    fn faulty_fires_once_then_passes() {
        let fs = FaultyVfs::new(MemVfs::new()).fail_on(Op::WriteAt, Some("f"), 2);
        fs.write_at(Path::new("f"), 0, b"a").unwrap();
        assert!(fs.write_at(Path::new("f"), 0, b"b").is_err());
        fs.write_at(Path::new("f"), 0, b"c").unwrap();
        assert!(fs.all_fired());
    }

    #[test]
    fn corrupt_flips_byte() {
        let fs = FaultyVfs::new(MemVfs::new()).corrupt_on(Op::WriteAt, Some("f"), 1);
        fs.write_at(Path::new("f"), 0, b"abc").unwrap();
        let d = fs.read(Path::new("f")).unwrap();
        assert_eq!(&d, &[b'a' ^ 0xff, b'b', b'c']);
    }
}
