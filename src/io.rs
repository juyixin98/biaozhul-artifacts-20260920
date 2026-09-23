//! Injectable file I/O layer.
//!
//! All persistence in the repository goes through the [`FileIO`] trait, so
//! tests can substitute [`MockIO`] to inject failures (append/sync/read
//! errors, torn writes, bit corruption) without touching real disks.
//!
//! Paths are logical, slash-separated, e.g. `data/cpu.tsb`, `late/cpu.tsl`.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

pub trait FileIO {
    /// Appends `data` to the file, creating it (and parent dirs) if needed.
    /// Returns the offset at which the data was written.
    fn append(&mut self, path: &str, data: &[u8]) -> io::Result<u64>;
    /// Reads the whole file; a missing file yields `Ok(vec![])`.
    fn read_all(&self, path: &str) -> io::Result<Vec<u8>>;
    /// Reads exactly `len` bytes at `offset`; short file => error.
    fn read_at(&self, path: &str, offset: u64, len: usize) -> io::Result<Vec<u8>>;
    /// fsync-equivalent: after Ok, previously appended bytes are durable.
    fn sync(&mut self, path: &str) -> io::Result<()>;
    /// Truncates the file to `len` bytes (used by crash recovery).
    fn truncate(&mut self, path: &str, len: u64) -> io::Result<()>;
    /// Current file length in bytes; missing file => 0.
    fn len(&self, path: &str) -> io::Result<u64>;
    /// Lists logical paths under `prefix` (one path component deep).
    fn list(&self, prefix: &str) -> io::Result<Vec<String>>;
}

// ------------------------------------------------------------------ FsIO

/// Real filesystem-backed I/O rooted at a directory.
pub struct FsIO {
    root: PathBuf,
}

impl FsIO {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        FsIO { root: root.into() }
    }

    fn resolve(&self, path: &str) -> PathBuf {
        // Logical paths are built internally from validated series names
        // ([A-Za-z0-9_-] only), so no traversal is possible.
        self.root.join(path)
    }
}

impl FileIO for FsIO {
    fn append(&mut self, path: &str, data: &[u8]) -> io::Result<u64> {
        let p = self.resolve(path);
        if let Some(dir) = p.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut f = OpenOptions::new().create(true).append(true).open(&p)?;
        let offset = f.metadata()?.len();
        f.write_all(data)?;
        Ok(offset)
    }

    fn read_all(&self, path: &str) -> io::Result<Vec<u8>> {
        match File::open(self.resolve(path)) {
            Ok(mut f) => {
                let mut buf = Vec::new();
                f.read_to_end(&mut buf)?;
                Ok(buf)
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Vec::new()),
            Err(e) => Err(e),
        }
    }

    fn read_at(&self, path: &str, offset: u64, len: usize) -> io::Result<Vec<u8>> {
        let mut f = File::open(self.resolve(path))?;
        f.seek(SeekFrom::Start(offset))?;
        let mut buf = vec![0u8; len];
        f.read_exact(&mut buf)?;
        Ok(buf)
    }

    fn sync(&mut self, path: &str) -> io::Result<()> {
        let p = self.resolve(path);
        if !Path::new(&p).exists() {
            return Ok(());
        }
        File::open(p)?.sync_all()
    }

    fn truncate(&mut self, path: &str, len: u64) -> io::Result<()> {
        let f = OpenOptions::new().write(true).open(self.resolve(path))?;
        f.set_len(len)
    }

    fn len(&self, path: &str) -> io::Result<u64> {
        match std::fs::metadata(self.resolve(path)) {
            Ok(m) => Ok(m.len()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(0),
            Err(e) => Err(e),
        }
    }

    fn list(&self, prefix: &str) -> io::Result<Vec<String>> {
        let dir = self.resolve(prefix);
        let mut out = Vec::new();
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(out),
            Err(e) => return Err(e),
        };
        for entry in entries {
            let entry = entry?;
            if let Some(name) = entry.file_name().to_str() {
                out.push(format!("{}/{}", prefix.trim_end_matches('/'), name));
            }
        }
        out.sort();
        Ok(out)
    }
}

// ---------------------------------------------------------------- MockIO

/// In-memory I/O with fault injection, for tests.
#[derive(Default)]
pub struct MockIO {
    pub files: BTreeMap<String, Vec<u8>>,
    /// Number of successful appends after which every append fails.
    pub fail_appends_after: Option<u64>,
    /// Paths (substring match) for which sync fails.
    pub fail_sync_on: Vec<String>,
    /// Paths (substring match) for which reads fail.
    pub fail_reads_on: Vec<String>,
    /// XOR-corrupt the byte at this absolute offset on the next append
    /// to a matching path: (path substring, offset from file start, mask).
    pub corrupt_on_append: Option<(String, u64, u8)>,
    appends: u64,
}

impl MockIO {
    pub fn new() -> Self {
        Self::default()
    }

    fn failing(&self) -> io::Error {
        io::Error::new(io::ErrorKind::Other, "injected fault")
    }
}

impl FileIO for MockIO {
    fn append(&mut self, path: &str, data: &[u8]) -> io::Result<u64> {
        self.appends += 1;
        if let Some(limit) = self.fail_appends_after {
            if self.appends > limit {
                return Err(self.failing());
            }
        }
        let file = self.files.entry(path.to_string()).or_default();
        let offset = file.len() as u64;
        file.extend_from_slice(data);
        if let Some((pat, at, mask)) = &self.corrupt_on_append {
            if path.contains(pat.as_str()) && *at >= offset && (*at as usize) < file.len() {
                file[*at as usize] ^= mask;
            }
        }
        Ok(offset)
    }

    fn read_all(&self, path: &str) -> io::Result<Vec<u8>> {
        if self.fail_reads_on.iter().any(|p| path.contains(p)) {
            return Err(self.failing());
        }
        Ok(self.files.get(path).cloned().unwrap_or_default())
    }

    fn read_at(&self, path: &str, offset: u64, len: usize) -> io::Result<Vec<u8>> {
        if self.fail_reads_on.iter().any(|p| path.contains(p)) {
            return Err(self.failing());
        }
        let file = self.files.get(path).cloned().unwrap_or_default();
        let start = offset as usize;
        let end = start + len;
        if end > file.len() {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "short read"));
        }
        Ok(file[start..end].to_vec())
    }

    fn sync(&mut self, path: &str) -> io::Result<()> {
        if self.fail_sync_on.iter().any(|p| path.contains(p)) {
            return Err(self.failing());
        }
        Ok(())
    }

    fn truncate(&mut self, path: &str, len: u64) -> io::Result<()> {
        let file = self.files.entry(path.to_string()).or_default();
        file.truncate(len as usize);
        Ok(())
    }

    fn len(&self, path: &str) -> io::Result<u64> {
        Ok(self.files.get(path).map(|f| f.len() as u64).unwrap_or(0))
    }

    fn list(&self, prefix: &str) -> io::Result<Vec<String>> {
        let p = format!("{}/", prefix.trim_end_matches('/'));
        Ok(self
            .files
            .keys()
            .filter(|k| k.starts_with(&p) && !k[p.len()..].contains('/'))
            .cloned()
            .collect())
    }
}
