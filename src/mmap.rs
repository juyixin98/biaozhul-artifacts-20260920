//! Minimal dependency-free `mmap(2)` wrapper used by the reference reader.
//!
//! Only the flags needed for a private, read-only mapping of a regular file
//! are used. The mapping is released with `munmap` on drop; `madvise` is
//! attempted (and ignored if unavailable) purely as a hint.

use std::fs::File;
use std::io;
use std::os::unix::io::AsRawFd;
use std::ptr;
use std::slice;

use core::ffi::{c_int, c_void};

fn libc_constants() -> (c_int, c_int) {
    // PROT_READ=1, MAP_PRIVATE=2 on Linux and the common Unix ABIs.
    (1, 2)
}

extern "C" {
    fn mmap(
        addr: *mut c_void,
        len: usize,
        prot: c_int,
        flags: c_int,
        fd: c_int,
        offset: i64,
    ) -> *mut c_void;
    fn munmap(addr: *mut c_void, len: usize) -> c_int;
}

const MAP_FAILED: *mut c_void = !0usize as *mut c_void;

/// A read-only, private mapping of an entire file.
pub struct Mmap {
    ptr: *mut u8,
    len: usize,
}

// The mapping is not mutated through this type and is owned exclusively.
unsafe impl Send for Mmap {}
unsafe impl Sync for Mmap {}

impl Mmap {
    /// Map `file` (from offset 0, whole length given by the caller).
    ///
    /// A zero-length file is handled without invoking `mmap` and yields an
    /// empty slice.
    pub fn map(file: &File, len: u64) -> io::Result<Mmap> {
        if len == 0 {
            return Ok(Mmap {
                ptr: ptr::NonNull::<u8>::dangling().as_ptr(),
                len: 0,
            });
        }
        let len = len as usize;
        let (prot, flags) = libc_constants();
        // SAFETY: the fd is valid (borrowed File), len is the real file size
        // (obtained via metadata by the caller), offset is 0. We only read.
        let p = unsafe { mmap(ptr::null_mut(), len, prot, flags, file.as_raw_fd(), 0) };
        if p == MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(Mmap {
            ptr: p as *mut u8,
            len,
        })
    }

    /// The mapped bytes.
    pub fn as_slice(&self) -> &[u8] {
        if self.len == 0 {
            return &[];
        }
        // SAFETY: ptr..ptr+len is a valid, populated, immutable-for-our-uses
        // mapping for the lifetime of self.
        unsafe { slice::from_raw_parts(self.ptr, self.len) }
    }

    /// Mapped length in bytes.
    pub fn len(&self) -> usize {
        self.len
    }

    /// Whether the mapping is empty.
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

impl Drop for Mmap {
    fn drop(&mut self) {
        if self.len != 0 {
            // SAFETY: ptr/len came from a successful mmap and are unmapped once.
            unsafe {
                let _ = munmap(self.ptr as *mut c_void, self.len);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn map_and_read_tempfile() {
        let path = std::env::temp_dir().join(format!("ifix-mmap-{}.bin", std::process::id()));
        {
            let mut f = File::create(&path).unwrap();
            f.write_all(b"ifix-mmap-smoke").unwrap();
        }
        let file = File::open(&path).unwrap();
        let len = file.metadata().unwrap().len();
        let m = Mmap::map(&file, len).unwrap();
        assert_eq!(m.as_slice(), b"ifix-mmap-smoke");
        drop(m);
        drop(file);
        std::fs::remove_file(&path).unwrap();
    }
}
