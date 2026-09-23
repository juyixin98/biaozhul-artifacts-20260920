//! Injectable I/O layer.
//!
//! All table reads and writes go through the two tiny traits in this module,
//! which makes the storage backend replaceable in tests:
//!
//! * [`WritableFile`] — sequential, offset-free appends plus a durability
//!   boundary (`sync_all`).
//! * [`RandomAccessFile`] — reads at an absolute offset. Implementations are
//!   free to return *fewer* bytes than requested (the table layer loops until
//!   EOF), so short reads are normal behaviour and the fault injector uses
//!   them to exercise that loop.
//!
//! Implementations provided here:
//!
//! * [`PosixWriter`] / [`PosixReader`] — real files via `std::fs`.
//! * [`MemWriter`] / [`MemReader`] backed by a shared [`MemStore`] — fully
//!   deterministic tables in memory.
//! * [`FaultyWriter`] / [`FaultyReader`] — decorators injecting failures.

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::error::{Error, Result};

// ---------------------------------------------------------------------------
// Traits
// ---------------------------------------------------------------------------

/// Sequential append-only sink.
///
/// `append` may perform a **short write**: it returns the number of bytes
/// actually written (`0..=data.len()`). Callers must retry the remainder.
pub trait WritableFile {
    /// Write as many leading bytes of `data` as possible.
    fn append(&mut self, data: &[u8]) -> Result<usize>;
    /// Flush data *and* metadata to stable storage. This is the table's single
    /// durability boundary.
    fn sync(&mut self) -> Result<()>;
}

/// Random read source; must be safe to share across threads.
pub trait RandomAccessFile: Sync {
    /// Read into `buf` from absolute `offset`. A short read — including an
    /// empty read at/after end of file — is `Ok(n)` with `n < buf.len()`.
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize>;
}

/// Honor [`WritableFile::append`] short writes with a retry loop.
pub fn append_all<W: WritableFile + ?Sized>(w: &mut W, mut data: &[u8]) -> Result<()> {
    while !data.is_empty() {
        let n = w.append(data)?;
        if n == 0 {
            return Err(Error::io_write("writer made no progress"));
        }
        if n > data.len() {
            return Err(Error::io_write("writer returned impossible byte count"));
        }
        data = &data[n..];
    }
    Ok(())
}

/// Loop until `buf` is filled or EOF reached; returns the number of bytes read.
/// A return smaller than `buf.len()` means end of file.
pub fn read_fill<R: RandomAccessFile + ?Sized>(
    r: &R,
    buf: &mut [u8],
    offset: u64,
) -> Result<usize> {
    let mut done = 0usize;
    while done < buf.len() {
        let n = r.read_at(&mut buf[done..], offset + done as u64)?;
        if n == 0 {
            break;
        }
        if n > buf.len() - done {
            return Err(Error::io_read("reader returned too many bytes"));
        }
        done += n;
    }
    Ok(done)
}

// ---------------------------------------------------------------------------
// POSIX implementation
// ---------------------------------------------------------------------------

/// Real on-disk writer. Created/truncated by [`PosixWriter::create`].
pub struct PosixWriter {
    file: File,
}

impl PosixWriter {
    pub fn create(path: &Path) -> Result<PosixWriter> {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .truncate(true)
            .open(path)?;
        Ok(PosixWriter { file })
    }
}

impl WritableFile for PosixWriter {
    fn append(&mut self, data: &[u8]) -> Result<usize> {
        Ok(self.file.write(data)?)
    }
    fn sync(&mut self) -> Result<()> {
        Ok(self.file.sync_all()?)
    }
}

/// Real on-disk random reader (a seek + read guarded by an internal mutex).
pub struct PosixReader {
    file: Mutex<File>,
}

impl PosixReader {
    pub fn open(path: &Path) -> Result<PosixReader> {
        let file = OpenOptions::new().read(true).open(path)?;
        Ok(PosixReader {
            file: Mutex::new(file),
        })
    }
}

impl RandomAccessFile for PosixReader {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize> {
        let mut file = self.file.lock().expect("posix reader mutex poisoned");
        file.seek(SeekFrom::Start(offset))?;
        Ok(file.read(buf)?)
    }
}

// ---------------------------------------------------------------------------
// In-memory implementation (test backend)
// ---------------------------------------------------------------------------

/// A shared byte buffer. [`MemWriter`] appends; [`MemReader`] reads.
#[derive(Clone)]
pub struct MemStore {
    inner: Arc<Mutex<Vec<u8>>>,
}

impl MemStore {
    pub fn new() -> MemStore {
        MemStore {
            inner: Arc::new(Mutex::new(Vec::new())),
        }
    }

    /// Snapshot of everything written.
    pub fn snapshot(&self) -> Vec<u8> {
        self.inner.lock().unwrap().clone()
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Replace contents — used by corruption tests to mutate a stored file.
    pub fn overwrite(&self, data: &[u8]) {
        *self.inner.lock().unwrap() = data.to_vec();
    }
}

impl Default for MemStore {
    fn default() -> Self {
        MemStore::new()
    }
}

pub struct MemWriter {
    store: MemStore,
    pos: usize,
}

impl MemWriter {
    pub fn new(store: MemStore) -> MemWriter {
        MemWriter { store, pos: 0 }
    }
}

impl WritableFile for MemWriter {
    fn append(&mut self, data: &[u8]) -> Result<usize> {
        let mut buf = self.store.inner.lock().unwrap();
        if self.pos != buf.len() {
            return Err(Error::io_write("mem writer not positioned at end"));
        }
        buf.extend_from_slice(data);
        self.pos += data.len();
        Ok(data.len())
    }

    fn sync(&mut self) -> Result<()> {
        Ok(())
    }
}

pub struct MemReader {
    store: MemStore,
}

impl MemReader {
    pub fn new(store: MemStore) -> MemReader {
        MemReader { store }
    }
}

impl RandomAccessFile for MemReader {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize> {
        let data = self.store.inner.lock().unwrap();
        let start = offset as usize;
        if start >= data.len() {
            return Ok(0);
        }
        let end = (start + buf.len()).min(data.len());
        buf[..end - start].copy_from_slice(&data[start..end]);
        Ok(end - start)
    }
}

// ---------------------------------------------------------------------------
// Fault injectors
// ---------------------------------------------------------------------------

/// Failure script for [`FaultyWriter`].
#[derive(Debug, Clone, Default)]
pub struct WriteFaults {
    /// The append call with this zero-based index fails with an IO error and
    /// writes nothing.
    pub fail_append_at: Option<u64>,
    /// The append call with this index accepts at most this many bytes once
    /// (clamped to a strictly short count), simulating a partial write.
    pub short_write_at: Option<(u64, usize)>,
    /// The sync call with this index fails.
    pub fail_sync_at: Option<u64>,
}

impl WriteFaults {
    pub fn clean() -> Self {
        WriteFaults::default()
    }
}

/// Writer decorator that injects IO errors / short writes deterministically.
pub struct FaultyWriter<W> {
    inner: W,
    faults: WriteFaults,
    appends: u64,
    syncs: u64,
}

impl<W: WritableFile> FaultyWriter<W> {
    pub fn new(inner: W, faults: WriteFaults) -> FaultyWriter<W> {
        FaultyWriter {
            inner,
            faults,
            appends: 0,
            syncs: 0,
        }
    }

    #[allow(dead_code)]
    pub fn append_calls(&self) -> u64 {
        self.appends
    }
}

impl<W: WritableFile> WritableFile for FaultyWriter<W> {
    fn append(&mut self, data: &[u8]) -> Result<usize> {
        let call = self.appends;
        self.appends += 1;
        if self.faults.fail_append_at == Some(call) {
            return Err(injected("write fault: injected append failure"));
        }
        let slice = if let Some((at, max_bytes)) = self.faults.short_write_at {
            if at == call && !data.is_empty() {
                // Simulate a genuine partial write: pass only a strict prefix
                // to the underlying device.
                &data[..max_bytes.min(data.len() - 1)]
            } else {
                data
            }
        } else {
            data
        };
        self.inner.append(slice)
    }

    fn sync(&mut self) -> Result<()> {
        let call = self.syncs;
        self.syncs += 1;
        if self.faults.fail_sync_at == Some(call) {
            return Err(injected("write fault: injected sync failure"));
        }
        self.inner.sync()
    }
}

/// Failure script for [`FaultyReader`].
#[derive(Debug, Clone, Default)]
pub struct ReadFaults {
    /// The read call with this zero-based index fails with an IO error.
    pub fail_read_at_call: Option<u64>,
    /// Every read returns at most this many bytes (fragmented IO).
    pub max_bytes_per_read: Option<usize>,
    /// Any read whose requested range contains this absolute offset stops one
    /// byte short of it (a simulated device boundary), exactly once per call.
    pub short_read_at_offset: Option<u64>,
}

impl ReadFaults {
    pub fn clean() -> Self {
        ReadFaults::default()
    }
}

/// Reader decorator injecting IO errors and short reads.
pub struct FaultyReader<R> {
    inner: R,
    faults: ReadFaults,
    calls: AtomicU64,
}

impl<R: RandomAccessFile> FaultyReader<R> {
    pub fn new(inner: R, faults: ReadFaults) -> FaultyReader<R> {
        FaultyReader {
            inner,
            faults,
            calls: AtomicU64::new(0),
        }
    }

    #[allow(dead_code)]
    pub fn call_count(&self) -> u64 {
        self.calls.load(Ordering::Relaxed)
    }
}

impl<R: RandomAccessFile> RandomAccessFile for FaultyReader<R> {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> Result<usize> {
        let call = self.calls.fetch_add(1, Ordering::Relaxed);
        if self.faults.fail_read_at_call == Some(call) {
            return Err(injected("read fault: injected read failure"));
        }
        let mut limit = buf.len();
        if let Some(max) = self.faults.max_bytes_per_read {
            limit = limit.min(max.max(1));
        }
        if let Some(point) = self.faults.short_read_at_offset {
            // If the (already capped) range straddles `point`, stop just before
            // it; always leave at least one byte for the retry loop to gather.
            if offset < point && point < offset + limit as u64 {
                limit = (point - offset) as usize;
            }
        }
        self.inner.read_at(&mut buf[..limit], offset)
    }
}

fn injected(msg: &str) -> Error {
    Error::Io(std::io::Error::other(msg.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mem_write_read_roundtrip() {
        let store = MemStore::new();
        let mut w = MemWriter::new(store.clone());
        append_all(&mut w, b"hello world").unwrap();
        w.sync().unwrap();
        assert_eq!(store.len(), 11);
        let r = MemReader::new(store);
        let mut buf = [0u8; 5];
        assert_eq!(read_fill(&r, &mut buf, 6).unwrap(), 5);
        assert_eq!(&buf, b"world");
        let mut tail = [0u8; 8];
        assert_eq!(read_fill(&r, &mut tail, 8).unwrap(), 3);
        assert_eq!(&tail[..3], b"rld");
    }

    #[test]
    fn faulty_writer_append_error() {
        let store = MemStore::new();
        let mut w = FaultyWriter::new(
            MemWriter::new(store),
            WriteFaults {
                fail_append_at: Some(1),
                ..WriteFaults::default()
            },
        );
        assert!(w.append(b"a").is_ok());
        assert!(w.append(b"b").is_err());
    }

    #[test]
    fn faulty_writer_short_write_is_retried_by_append_all() {
        let store = MemStore::new();
        let mut w = FaultyWriter::new(
            MemWriter::new(store.clone()),
            WriteFaults {
                short_write_at: Some((0, 2)),
                ..WriteFaults::default()
            },
        );
        append_all(&mut w, b"abcdef").unwrap();
        assert_eq!(store.snapshot(), b"abcdef");
    }

    #[test]
    fn faulty_writer_sync_error() {
        let store = MemStore::new();
        let mut w = FaultyWriter::new(
            MemWriter::new(store),
            WriteFaults {
                fail_sync_at: Some(0),
                ..WriteFaults::default()
            },
        );
        assert!(w.sync().is_err());
    }

    #[test]
    fn faulty_reader_fragmented_reads_still_assemble() {
        let store = MemStore::new();
        let mut w = MemWriter::new(store.clone());
        append_all(&mut w, b"abcdefghij").unwrap();
        let r = FaultyReader::new(
            MemReader::new(store),
            ReadFaults {
                max_bytes_per_read: Some(3),
                ..ReadFaults::default()
            },
        );
        let mut buf = [0u8; 10];
        assert_eq!(read_fill(&r, &mut buf, 0).unwrap(), 10);
        assert_eq!(&buf, b"abcdefghij");
    }

    #[test]
    fn faulty_reader_injected_error() {
        let store = MemStore::new();
        let mut w = MemWriter::new(store.clone());
        append_all(&mut w, b"abcdefghij").unwrap();
        let r = FaultyReader::new(
            MemReader::new(store),
            ReadFaults {
                fail_read_at_call: Some(1),
                ..ReadFaults::default()
            },
        );
        let mut buf = [0u8; 4];
        assert!(r.read_at(&mut buf, 0).is_ok());
        assert!(r.read_at(&mut buf, 4).is_err());
    }
}
