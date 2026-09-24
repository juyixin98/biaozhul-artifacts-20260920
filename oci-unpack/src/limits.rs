//! Decompression / extraction limits ("过量解压" protection).
//!
//! A small compressed layer can expand by orders of magnitude, and a tar can
//! also carry millions of tiny entries. Both dimensions are bounded here; the
//! limits are deliberately tight for fixture workloads and overridable.

/// Hard caps applied while extracting a *single* layer.
#[derive(Debug, Clone)]
pub struct Limits {
    /// Max bytes read off the compressed blob (also the digest input size).
    pub max_compressed_bytes: u64,
    /// Max bytes emitted by the decompressor (zip-bomb bound).
    pub max_decompressed_bytes: u64,
    /// Max total size of regular file payloads written to the rootfs.
    pub max_file_payload_bytes: u64,
    /// Max number of tar entries per layer (including directories).
    pub max_entries: u64,
    /// Max individual regular file size.
    pub max_single_file_bytes: u64,
    /// Max length of a symlink target / path component string.
    pub max_link_name_len: usize,
    /// Max number of hard links in one layer.
    pub max_hard_links: u64,
    /// Max decompressed payload kept in memory for a manifest-style small read.
    pub max_buffered_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_compressed_bytes: 64 * 1024 * 1024,
            max_decompressed_bytes: 256 * 1024 * 1024,
            max_file_payload_bytes: 256 * 1024 * 1024,
            max_entries: 20_000,
            max_single_file_bytes: 64 * 1024 * 1024,
            max_link_name_len: 1024,
            max_hard_links: 10_000,
            max_buffered_bytes: 8 * 1024 * 1024,
        }
    }
}

/// Mutable per-layer accounting. Every `add_*` returns an error once a cap is
/// crossed, so callers cannot accidentally overshoot by an unbounded amount.
#[derive(Debug, Default)]
pub struct Usage {
    pub compressed: u64,
    pub decompressed: u64,
    pub payload: u64,
    pub entries: u64,
    pub hard_links: u64,
}

impl Usage {
    pub fn add_compressed(&mut self, n: u64, lim: &Limits) -> crate::error::Result<()> {
        self.compressed = self.compressed.saturating_add(n);
        if self.compressed > lim.max_compressed_bytes {
            return Err(Error::limit(format!(
                "compressed layer size {} exceeds limit {}",
                self.compressed, lim.max_compressed_bytes
            )));
        }
        Ok(())
    }

    pub fn add_decompressed(&mut self, n: u64, lim: &Limits) -> crate::error::Result<()> {
        self.decompressed = self.decompressed.saturating_add(n);
        if self.decompressed > lim.max_decompressed_bytes {
            return Err(Error::limit(format!(
                "decompressed stream {} bytes exceeds limit {}",
                self.decompressed, lim.max_decompressed_bytes
            )));
        }
        Ok(())
    }

    pub fn add_payload(&mut self, n: u64, lim: &Limits) -> crate::error::Result<()> {
        self.payload = self.payload.saturating_add(n);
        if self.payload > lim.max_file_payload_bytes {
            return Err(Error::limit(format!(
                "written payload {} bytes exceeds limit {}",
                self.payload, lim.max_file_payload_bytes
            )));
        }
        Ok(())
    }

    pub fn count_entry(&mut self, lim: &Limits) -> crate::error::Result<()> {
        self.entries += 1;
        if self.entries > lim.max_entries {
            return Err(Error::limit(format!(
                "entry count {} exceeds limit {}",
                self.entries, lim.max_entries
            )));
        }
        Ok(())
    }

    pub fn count_hard_link(&mut self, lim: &Limits) -> crate::error::Result<()> {
        self.hard_links += 1;
        if self.hard_links > lim.max_hard_links {
            return Err(Error::limit(format!(
                "hard link count {} exceeds limit {}",
                self.hard_links, lim.max_hard_links
            )));
        }
        Ok(())
    }
}

use crate::error::Error;
