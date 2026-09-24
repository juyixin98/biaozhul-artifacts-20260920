//! Streaming readers that hash and meter every byte.
//!
//! Layer blobs are processed in two streaming passes. The *verify* pass feeds
//! compressed bytes through SHA-256, gzip-inflates them and walks the tar entry
//! stream while accounting limits, never buffering whole payloads. The *apply*
//! pass replays the same stream while mutating the staging rootfs. Nothing is
//! trusted: the apply pass re-hashes and re-asserts the digest.
//!
//! Reader composition for a gzip layer:
//!
//! `file → HashingReader (compressed budget) → GzDecoder → MeteringReader`
//! `(decompressed budget) → tar::Archive`.

use std::io::{self, Read};

use flate2::read::GzDecoder;

use crate::digest::Hasher;
use crate::error::{Error, Result};
use crate::limits::{Limits, Usage};

/// Feeds every byte read through a SHA-256 hasher and the compressed-size
/// budget. Call [`HashingReader::finish`] after the stream is fully drained.
pub struct HashingReader<R> {
    inner: R,
    hasher: Hasher,
    usage: Usage,
    limits: Limits,
}

impl<R: Read> HashingReader<R> {
    pub fn new(inner: R, usage: Usage, limits: Limits) -> Self {
        Self {
            inner,
            hasher: Hasher::new(),
            usage,
            limits,
        }
    }

    /// Returns (digest, usage). The reader must be at EOF.
    pub fn finish(self) -> (String, Usage) {
        (self.hasher.finalize(), self.usage)
    }
}

impl<R: Read> Read for HashingReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        if n > 0 {
            self.hasher.update(&buf[..n]);
            self.usage
                .add_compressed(n as u64, &self.limits)
                .map_err(io::Error::other)?;
        }
        Ok(n)
    }
}

/// Meters the *decompressed* byte stream, hashes it (the OCI `diff_id` is the
/// SHA-256 of the uncompressed tar) and propagates [`Error`]s through the
/// `io::Error` other-cause slot.
pub struct MeteringReader<R> {
    inner: R,
    usage: Usage,
    limits: Limits,
    hasher: Hasher,
}

impl<R: Read> MeteringReader<R> {
    pub fn new(inner: R, usage: Usage, limits: Limits) -> Self {
        Self {
            inner,
            usage,
            limits,
            hasher: Hasher::new(),
        }
    }

    pub fn into_usage(self) -> Usage {
        self.usage
    }

    /// Digest of every decompressed byte seen so far (consumed after EOF).
    pub fn into_parts(self) -> (Usage, String) {
        (self.usage, self.hasher.finalize())
    }
}

impl<R: Read> Read for MeteringReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = self.inner.read(buf)?;
        if n > 0 {
            self.hasher.update(&buf[..n]);
            self.usage
                .add_decompressed(n as u64, &self.limits)
                .map_err(io::Error::other)?;
        }
        Ok(n)
    }
}

/// Build the gzip decoder. `GzDecoder` already decodes multi-member gzip
/// streams by default, so bytes hidden after the first member are still
/// consumed and counted rather than silently ignored.
pub fn gzip_decoder<R: Read>(inner: R) -> GzDecoder<R> {
    GzDecoder::new(inner)
}

/// Drain a tar entry completely in the verify pass: counts payload against
/// both the per-file and aggregate limits without writing anything.
pub fn drain_entry<R: Read>(
    r: &mut R,
    declared_size: u64,
    usage: &mut Usage,
    limits: &Limits,
) -> Result<u64> {
    if declared_size > limits.max_single_file_bytes {
        return Err(Error::limit(format!(
            "file declares {declared_size} bytes, limit is {}",
            limits.max_single_file_bytes
        )));
    }
    let mut buf = vec![0u8; 64 * 1024];
    let mut total: u64 = 0;
    loop {
        match r.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                total = total.saturating_add(n as u64);
                if total > limits.max_single_file_bytes {
                    return Err(Error::limit(format!(
                        "file payload exceeds single-file limit {}",
                        limits.max_single_file_bytes
                    )));
                }
                usage.add_payload(n as u64, limits)?;
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(decode_io_err(e)),
        }
    }
    Ok(total)
}

/// Map an io error carrying our [`Error`] (from the metering readers) back to
/// it; anything else is treated as a plain I/O failure.
pub fn decode_io_err(e: io::Error) -> Error {
    match e.downcast::<Error>() {
        Ok(err) => err,
        Err(e) => Error::io(e),
    }
}

/// Force a reader to physical EOF. `tar::Archive` stops as soon as it sees the
/// two zero terminator blocks, which can leave the second block (and, for an
/// uncompressed blob, any trailing bytes) unread — and therefore unhashed.
/// Draining the reader chain after the archive walk makes the digest cover
/// every byte of the on-disk blob.
pub fn drain_to_eof<R: Read>(r: &mut R) -> io::Result<u64> {
    let mut buf = [0u8; 64 * 1024];
    let mut total = 0u64;
    loop {
        match r.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => total = total.saturating_add(n as u64),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(total)
}
