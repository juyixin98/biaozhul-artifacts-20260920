//! Injectable I/O layer.
//!
//! Everything the engine touches on disk goes through the [`Fs`] trait rather
//! than `std::fs` directly, so tests can install [`FailingFs`] and force
//! `fsync` / read failures at deterministic points.
//!
//! All paths are repository-relative: `/`-separated, never absolute, never
//! containing a `..` component. [`Fs::open`] anchors them under the repository
//! root.

use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::error::{Error, Result};

/// Sequential, read-once file handle.
pub trait RFile: Send + Sync {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize>;

    fn read_exact(&mut self, mut buf: &mut [u8]) -> io::Result<()> {
        while !buf.is_empty() {
            match self.read(buf) {
                Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "short read")),
                Ok(n) => buf = &mut buf[n..],
                Err(ref e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }
}

/// Sequential write file handle with an explicit durability operation.
pub trait WFile: Send + Sync {
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()>;
    fn flush(&mut self) -> io::Result<()>;
    /// `fsync`: durability boundary. A segment is only considered committed
    /// *after* this returns successfully.
    fn sync_all(&mut self) -> io::Result<()>;
}

/// The storage backend. Implementations must be safe to share across threads.
pub trait Fs: Send + Sync {
    fn root(&self) -> &Path;
    fn create_dir_all(&self, rel: &str) -> Result<()>;
    fn exists(&self, rel: &str) -> Result<bool>;
    fn remove_file(&self, rel: &str) -> Result<()>;
    fn rename(&self, from: &str, to: &str) -> Result<()>;
    /// Sorted directory listing of plain file/dir names (not full paths).
    fn list(&self, rel: &str) -> Result<Vec<String>>;
    fn open_read(&self, rel: &str) -> Result<Box<dyn RFile>>;
    fn create_write(&self, rel: &str) -> Result<Box<dyn WFile>>;
    /// fsync of a directory so that a prior rename/create is durable.
    fn sync_dir(&self, rel: &str) -> Result<()>;
}

// ---------------------------------------------------------------------------
// Real filesystem
// ---------------------------------------------------------------------------

pub struct RealFs {
    root: PathBuf,
}

impl RealFs {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        RealFs { root: root.into() }
    }

    fn resolve(&self, rel: &str) -> Result<PathBuf> {
        check_rel(rel)?;
        Ok(self.root.join(rel))
    }
}

/// Reject absolute paths and `..` traversal; normalise `/`.
fn check_rel(rel: &str) -> Result<()> {
    if rel.is_empty() {
        return Err(Error::bad("empty path"));
    }
    let p = Path::new(rel);
    if p.is_absolute() {
        return Err(Error::bad(format!("absolute path not allowed: {rel}")));
    }
    for c in p.components() {
        use std::path::Component;
        match c {
            Component::Normal(_) | Component::CurDir => {}
            other => return Err(Error::bad(format!("illegal path component {other:?} in {rel}"))),
        }
    }
    Ok(())
}

impl Fs for RealFs {
    fn root(&self) -> &Path {
        &self.root
    }

    fn create_dir_all(&self, rel: &str) -> Result<()> {
        let p = self.resolve(rel)?;
        fs::create_dir_all(&p)?;
        Ok(())
    }

    fn exists(&self, rel: &str) -> Result<bool> {
        Ok(self.resolve(rel)?.exists())
    }

    fn remove_file(&self, rel: &str) -> Result<()> {
        let p = self.resolve(rel)?;
        match fs::remove_file(&p) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    fn rename(&self, from: &str, to: &str) -> Result<()> {
        let a = self.resolve(from)?;
        let b = self.resolve(to)?;
        fs::rename(a, b)?;
        Ok(())
    }

    fn list(&self, rel: &str) -> Result<Vec<String>> {
        let p = self.resolve(rel)?;
        let mut names = Vec::new();
        for entry in fs::read_dir(&p)? {
            let entry = entry?;
            names.push(entry.file_name().to_string_lossy().into_owned());
        }
        names.sort();
        Ok(names)
    }

    fn open_read(&self, rel: &str) -> Result<Box<dyn RFile>> {
        let f = fs::File::open(self.resolve(rel)?)?;
        Ok(Box::new(f))
    }

    fn create_write(&self, rel: &str) -> Result<Box<dyn WFile>> {
        let f = fs::File::create(self.resolve(rel)?)?;
        Ok(Box::new(f))
    }

    fn sync_dir(&self, rel: &str) -> Result<()> {
        let f = fs::File::open(self.resolve(rel)?)?;
        f.sync_all()?;
        Ok(())
    }
}

impl RFile for fs::File {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        Read::read(self, buf)
    }
}

impl WFile for fs::File {
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        Write::write_all(self, buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        Write::flush(self)
    }
    fn sync_all(&mut self) -> io::Result<()> {
        fs::File::sync_all(self)
    }
}

impl RFile for Box<dyn RFile> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        (**self).read(buf)
    }
}

impl WFile for Box<dyn WFile> {
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        (**self).write_all(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        (**self).flush()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        (**self).sync_all()
    }
}

// ---------------------------------------------------------------------------
// Buffered wrappers
// ---------------------------------------------------------------------------

pub struct BufReader<R: RFile> {
    inner: R,
    buf: Vec<u8>,
    pos: usize,
    len: usize,
}

impl<R: RFile> BufReader<R> {
    pub fn new(inner: R, capacity: usize) -> Self {
        BufReader { inner, buf: vec![0u8; capacity.max(1)], pos: 0, len: 0 }
    }

    /// Fill `out` from the internal buffer; returns bytes copied.
    fn drain(&mut self, out: &mut [u8]) -> usize {
        let avail = &self.buf[self.pos..self.len];
        let n = avail.len().min(out.len());
        out[..n].copy_from_slice(&avail[..n]);
        self.pos += n;
        n
    }
}

impl<R: RFile> RFile for BufReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        // Serve any buffered bytes first; only pull from the underlying file
        // when the internal buffer is drained.
        if self.pos < self.len {
            return Ok(self.drain(buf));
        }
        self.pos = 0;
        self.len = 0;
        let n = self.inner.read(&mut self.buf[..])?;
        self.len = n;
        if n == 0 {
            return Ok(0);
        }
        Ok(self.drain(buf))
    }
}

pub struct BufWriter<W: WFile> {
    inner: W,
    buf: Vec<u8>,
}

impl<W: WFile> BufWriter<W> {
    pub fn new(inner: W, capacity: usize) -> Self {
        BufWriter { inner, buf: Vec::with_capacity(capacity.max(1)) }
    }
}

impl<W: WFile> WFile for BufWriter<W> {
    fn write_all(&mut self, data: &[u8]) -> io::Result<()> {
        if data.len() >= self.buf.capacity() {
            // Large write: flush anything buffered and bypass the buffer.
            self.flush()?;
            self.inner.write_all(data)?;
            return Ok(());
        }
        if self.buf.len() + data.len() > self.buf.capacity() {
            self.flush()?;
        }
        self.buf.extend_from_slice(data);
        Ok(())
    }

    fn flush(&mut self) -> io::Result<()> {
        if !self.buf.is_empty() {
            self.inner.write_all(&self.buf)?;
            self.buf.clear();
        }
        self.inner.flush()
    }

    fn sync_all(&mut self) -> io::Result<()> {
        self.flush()?;
        self.inner.sync_all()
    }
}

// ---------------------------------------------------------------------------
// Fault injection
// ---------------------------------------------------------------------------

/// Configurable, shared fault state. Counters advance as the engine performs
/// I/O; when a configured threshold is reached the next operation fails.
#[derive(Default, Debug)]
pub struct FailState {
    /// Fail the Nth `sync_all` on a run/merge segment (counting from 1).
    pub fail_on_sync: Option<u64>,
    pub sync_count: u64,
    pub sync_fired: bool,
    /// Fail once reads whose path contains this substring have delivered this
    /// many bytes total. Byte-based counting makes the trigger independent of
    /// the I/O buffer size.
    pub fail_read_path: Option<String>,
    pub fail_on_read_bytes: Option<u64>,
    pub read_bytes: u64,
    pub read_fired: bool,
}

impl FailState {
    pub fn new() -> Arc<Mutex<Self>> {
        Arc::new(Mutex::new(Self::default()))
    }
}

/// A [`Fs`] wrapper that injects failures according to [`FailState`].
pub struct FailingFs {
    inner: Arc<dyn Fs>,
    state: Arc<Mutex<FailState>>,
}

impl FailingFs {
    pub fn new(inner: Arc<dyn Fs>, state: Arc<Mutex<FailState>>) -> Self {
        FailingFs { inner, state }
    }

    /// Only syncs of sorted segments (runs or merged runs) count, so that
    /// input/manifest syncs do not perturb deterministic thresholds.
    fn is_segment_sync(rel: &str) -> bool {
        rel.contains("/runs/") || rel.contains("/merges/")
    }
}

impl Fs for FailingFs {
    fn root(&self) -> &Path {
        self.inner.root()
    }
    fn create_dir_all(&self, rel: &str) -> Result<()> {
        self.inner.create_dir_all(rel)
    }
    fn exists(&self, rel: &str) -> Result<bool> {
        self.inner.exists(rel)
    }
    fn remove_file(&self, rel: &str) -> Result<()> {
        self.inner.remove_file(rel)
    }
    fn rename(&self, from: &str, to: &str) -> Result<()> {
        self.inner.rename(from, to)
    }
    fn list(&self, rel: &str) -> Result<Vec<String>> {
        self.inner.list(rel)
    }
    fn open_read(&self, rel: &str) -> Result<Box<dyn RFile>> {
        let inner = self.inner.open_read(rel)?;
        let armed = {
            let s = self.state.lock().unwrap();
            s.fail_read_path.as_deref().is_some_and(|sub| rel.contains(sub))
                && s.fail_on_read_bytes.is_some()
        };
        Ok(Box::new(FailingRead {
            inner,
            path: rel.to_owned(),
            state: self.state.clone(),
            armed,
        }))
    }
    fn create_write(&self, rel: &str) -> Result<Box<dyn WFile>> {
        let inner = self.inner.create_write(rel)?;
        Ok(Box::new(FailingWrite {
            inner,
            path: rel.to_owned(),
            state: self.state.clone(),
            segment: Self::is_segment_sync(rel),
        }))
    }
    fn sync_dir(&self, rel: &str) -> Result<()> {
        self.inner.sync_dir(rel)
    }
}

struct FailingWrite {
    inner: Box<dyn WFile>,
    path: String,
    state: Arc<Mutex<FailState>>,
    segment: bool,
}

impl WFile for FailingWrite {
    fn write_all(&mut self, buf: &[u8]) -> io::Result<()> {
        self.inner.write_all(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        if self.segment {
            let fire = {
                let mut s = self.state.lock().unwrap();
                s.sync_count += 1;
                match s.fail_on_sync {
                    Some(n) if s.sync_count == n && !s.sync_fired => {
                        s.sync_fired = true;
                        true
                    }
                    _ => false,
                }
            };
            if fire {
                return Err(io::Error::other(format!(
                    "injected sync failure on {} (sync #{})",
                    self.path,
                    self.state.lock().unwrap().sync_count
                )));
            }
        }
        self.inner.sync_all()
    }
}

struct FailingRead {
    inner: Box<dyn RFile>,
    path: String,
    state: Arc<Mutex<FailState>>,
    armed: bool,
}

impl RFile for FailingRead {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if !self.armed {
            return self.inner.read(buf);
        }
        // Cap this read so the configured byte threshold is crossed during
        // this call, perform the read, account the bytes, then fail — a
        // realistic mid-stream I/O error with data already delivered.
        let cap = {
            let s = self.state.lock().unwrap();
            if s.read_fired {
                buf.len()
            } else {
                match s.fail_on_read_bytes {
                    Some(threshold) => {
                        let left = threshold.saturating_sub(s.read_bytes) as usize;
                        if left == 0 { 1 } else { left.min(buf.len()).max(1) }
                    }
                    None => buf.len(),
                }
            }
        };
        let n = self.inner.read(&mut buf[..cap])?;
        let fire = {
            let mut s = self.state.lock().unwrap();
            s.read_bytes = s.read_bytes.saturating_add(n as u64);
            matches!(s.fail_on_read_bytes, Some(t) if !s.read_fired && s.read_bytes >= t)
                && {
                    s.read_fired = true;
                    true
                }
        };
        if fire {
            return Err(io::Error::other(format!(
                "injected read failure on {} after byte threshold",
                self.path
            )));
        }
        Ok(n)
    }
}

/// A tiny helper used by tests to corrupt a segment on disk.
pub fn overwrite_at(fs: &dyn Fs, rel: &str, offset: u64, bytes: &[u8]) -> Result<()> {
    let mut f = ::std::fs::File::open(fs.root().join(rel))?;
    f.seek(SeekFrom::Start(offset))?;
    Write::write_all(&mut f, bytes)?;
    f.sync_all()?;
    Ok(())
}
