//! Random-access byte sources feeding the shared decode engine.
//!
//! * [`FileSource`] — a plain `File` driven by `seek + read` (streaming: only
//!   the 128-byte header plus the blocks/nodes/names a query actually touches
//!   are ever read into memory),
//! * [`MmapSource`] — the dependency-free `mmap` reference implementation in
//!   [`crate::mmap`], used to prove both decoders produce identical results,
//! * [`SliceSource`] — an in-memory image for tests.
//!
//! All three share one decode engine in [`crate::reader`].

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use crate::error::{Error, Result};
use crate::mmap::Mmap;

/// Any random-access byte source the decode engine can run on.
pub trait Source: Send + Sync {
    /// Total file length.
    fn len(&self) -> u64;
    /// Whether the file is empty (an IFIX file never is).
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Fill `buf` exactly starting at absolute byte offset `at`.
    ///
    /// Implementations return [`Error::ShortRead`] rather than a partial
    /// result when the declared section runs past end of file.
    fn read_at(&self, buf: &mut [u8], at: u64) -> Result<()>;
}

/// Streaming source over a regular file: one `seek` + one bounded `read` per
/// fetch, no whole-file buffering.
pub struct FileSource {
    file: std::sync::Mutex<File>,
    len: u64,
}

impl FileSource {
    /// Open a file for streaming reads.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let file = File::open(path.as_ref())?;
        let len = file.metadata()?.len();
        Ok(FileSource {
            file: std::sync::Mutex::new(file),
            len,
        })
    }
}

impl Source for FileSource {
    fn len(&self) -> u64 {
        self.len
    }

    fn read_at(&self, buf: &mut [u8], at: u64) -> Result<()> {
        let mut file = self.file.lock().expect("file mutex poisoned");
        file.seek(SeekFrom::Start(at))?;
        let mut got = 0usize;
        while got < buf.len() {
            match file.read(&mut buf[got..]) {
                Ok(0) => {
                    return Err(Error::ShortRead {
                        at,
                        need: buf.len() as u64,
                    })
                }
                Ok(n) => got += n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }
}

/// Reference source over a private read-only `mmap`.
pub struct MmapSource {
    map: Mmap,
    len: u64,
}

impl MmapSource {
    /// Map a whole file read-only.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let file = File::open(path.as_ref())?;
        let len = file.metadata()?.len();
        let map = Mmap::map(&file, len)?;
        Ok(MmapSource { map, len })
    }
}

fn checked_end(at: u64, len: usize) -> Result<u64> {
    at.checked_add(len as u64).ok_or(Error::ShortRead {
        at,
        need: len as u64,
    })
}

impl Source for MmapSource {
    fn len(&self) -> u64 {
        self.len
    }

    fn read_at(&self, buf: &mut [u8], at: u64) -> Result<()> {
        let bytes = self.map.as_slice();
        let end = checked_end(at, buf.len())?;
        if end > bytes.len() as u64 {
            return Err(Error::ShortRead {
                at,
                need: buf.len() as u64,
            });
        }
        buf.copy_from_slice(&bytes[at as usize..end as usize]);
        Ok(())
    }
}

/// In-memory source, primarily for tests and fuzzing.
pub struct SliceSource {
    data: Vec<u8>,
}

impl SliceSource {
    /// Take ownership of an encoded file image.
    pub fn new(data: Vec<u8>) -> Self {
        SliceSource { data }
    }
}

impl Source for SliceSource {
    fn len(&self) -> u64 {
        self.data.len() as u64
    }

    fn read_at(&self, buf: &mut [u8], at: u64) -> Result<()> {
        let end = checked_end(at, buf.len())?;
        if end > self.data.len() as u64 {
            return Err(Error::ShortRead {
                at,
                need: buf.len() as u64,
            });
        }
        buf.copy_from_slice(&self.data[at as usize..end as usize]);
        Ok(())
    }
}
