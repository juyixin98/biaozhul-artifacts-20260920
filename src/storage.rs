//! Storage backends behind one random-read interface.
//!
//! Three backends are provided so callers and tests can compare the ordinary
//! `pread`-style reader against a memory-mapped reference:
//!
//! * [`FileStorage`]  — `read_at` on a `File`, no seek state, page-cache only.
//! * [`MmapStorage`]  — a private `mmap(MAP_PRIVATE)` of the whole file.
//! * [`MemStorage`]   — an in-memory byte slice (used by tests/builders).

use crate::error::{IfixError, Result};
use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::Path;

/// Exact-length random read. Implementations MUST either fill `buf` fully or
/// return an error; short reads are retried internally.
pub trait Storage: Send + Sync {
    fn len(&self) -> u64;
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()>;

    fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Read a `u32` little-endian value.
    fn read_u32(&self, offset: u64) -> Result<u32> {
        let mut b = [0u8; 4];
        self.read_at(&mut b, offset)?;
        Ok(u32::from_le_bytes(b))
    }
}

// --- File backend -----------------------------------------------------------

pub struct FileStorage {
    file: File,
    len: u64,
}

impl FileStorage {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let file = File::open(path.as_ref())?;
        let len = file.metadata()?.len();
        Ok(FileStorage { file, len })
    }
}

impl Storage for FileStorage {
    fn len(&self) -> u64 {
        self.len
    }

    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()> {
        let mut filled = 0usize;
        while filled < buf.len() {
            let n = self
                .file
                .read_at(&mut buf[filled..], offset + filled as u64)?;
            if n == 0 {
                return Err(IfixError::format(
                    "TRUNCATED",
                    format!(
                        "unexpected EOF: wanted {} bytes at offset {}, file length {}",
                        buf.len(),
                        offset,
                        self.len
                    ),
                ));
            }
            filled += n;
        }
        Ok(())
    }
}

// --- Memory backend ---------------------------------------------------------

pub struct MemStorage {
    data: Vec<u8>,
}

impl MemStorage {
    pub fn new(data: Vec<u8>) -> Self {
        MemStorage { data }
    }
}

impl Storage for MemStorage {
    fn len(&self) -> u64 {
        self.data.len() as u64
    }

    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()> {
        let start = offset as usize;
        let end = start
            .checked_add(buf.len())
            .ok_or_else(|| IfixError::format("OFFSET_OVERFLOW", "read range overflowed usize"))?;
        let slice = self.data.get(start..end).ok_or_else(|| {
            IfixError::format(
                "TRUNCATED",
                format!(
                    "read past end: wanted {} bytes at offset {}, length {}",
                    buf.len(),
                    offset,
                    self.data.len()
                ),
            )
        })?;
        buf.copy_from_slice(slice);
        Ok(())
    }
}

// --- mmap backend (hand-written FFI, no libc crate) -------------------------

#[cfg(unix)]
mod mmap_ffi {
    use std::os::fd::RawFd;

    pub const PROT_READ: i32 = 1;
    pub const MAP_PRIVATE: i32 = 2;
    pub const MAP_FAILED: usize = usize::MAX;

    extern "C" {
        pub fn mmap(
            addr: *mut core::ffi::c_void,
            length: usize,
            prot: i32,
            flags: i32,
            fd: RawFd,
            offset: i64,
        ) -> *mut core::ffi::c_void;

        pub fn munmap(addr: *mut core::ffi::c_void, length: usize) -> i32;
    }
}

pub struct MmapStorage {
    addr: *mut u8,
    len: usize,
    _file: File,
}

// The mapping is private and read-only; raw pointer is only dereferenced
// inside `read_at` against a validated length. Send/Sync are equivalent to
// sharing a read-only file mapping.
unsafe impl Send for MmapStorage {}
unsafe impl Sync for MmapStorage {}

impl MmapStorage {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let file = File::open(path.as_ref())?;
        let len = file.metadata()?.len() as usize;
        if len == 0 {
            return Err(IfixError::format("TRUNCATED", "cannot mmap an empty file"));
        }
        // SAFETY: `len` is the measured file size (non-zero), the fd is valid
        // for the lifetime of this struct (`_file` is owned), `PROT_READ` +
        // `MAP_PRIVATE` never writes through or back to the file, and every
        // access in `read_at` is bounds-checked against `len`.
        let raw = unsafe {
            mmap_ffi::mmap(
                core::ptr::null_mut(),
                len,
                mmap_ffi::PROT_READ,
                mmap_ffi::MAP_PRIVATE,
                std::os::fd::AsRawFd::as_raw_fd(&file),
                0,
            )
        };
        if (raw as usize) == mmap_ffi::MAP_FAILED {
            return Err(IfixError::Io(std::io::Error::last_os_error()));
        }
        Ok(MmapStorage {
            addr: raw as *mut u8,
            len,
            _file: file,
        })
    }
}

impl Drop for MmapStorage {
    fn drop(&mut self) {
        // SAFETY: `addr`/`len` are exactly the values returned by a
        // successful `mmap`; the mapping is still live because `self` owns
        // the only reference and Drop runs once.
        unsafe {
            mmap_ffi::munmap(self.addr as *mut core::ffi::c_void, self.len);
        }
    }
}

impl Storage for MmapStorage {
    fn len(&self) -> u64 {
        self.len as u64
    }

    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<()> {
        let start = offset as usize;
        let end = start.checked_add(buf.len()).ok_or_else(|| {
            IfixError::format("OFFSET_OVERFLOW", "mmap read range overflowed usize")
        })?;
        if end > self.len {
            return Err(IfixError::format(
                "TRUNCATED",
                format!(
                    "mmap read past end: wanted {} bytes at offset {}, length {}",
                    buf.len(),
                    offset,
                    self.len
                ),
            ));
        }
        // SAFETY: `start..end` is within the mapped, initialized region of a
        // read-only private mapping; `buf` is a distinct caller-provided
        // buffer so the ranges cannot alias.
        let src = unsafe { core::slice::from_raw_parts(self.addr.add(start), buf.len()) };
        buf.copy_from_slice(src);
        Ok(())
    }
}
