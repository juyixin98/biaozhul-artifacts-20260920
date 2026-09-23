//! On-disk segment ("run") format.
//!
//! A run is a self-describing, independently verifiable sorted run:
//!
//! ```text
//! offset 0:  magic           8 bytes  b"EXTSORT1"
//! repeated:  frame header    16 bytes [seq:u64 LE][ulen:u32 LE][crc:u32 LE]
//!            frame payload   ulen bytes (the raw input line, no newline)
//! trailer:   end magic       8 bytes  b"EXEND001"
//!            end header     16 bytes  [count:u64 LE][aggr_crc:u32 LE][crc:u32 LE]
//! ```
//!
//! Per-frame `crc` is CRC-32 over the 12 header bytes (`seq` + `ulen`) followed
//! by the payload. `aggr_crc` is a streaming CRC-32 over the concatenation of
//! every frame's header+payload bytes in write order; it detects dropped,
//! reordered or corrupted frames even when a single frame CRC happens to
//! still match. The trailer's own `crc` covers its 12 payload bytes.
//!
//! The frame/trailer distinction cannot rely on the first byte (a frame header
//! may start with any byte), so the reader speculatively reads 8 bytes and
//! compares them to `EXEND001`; on mismatch those 8 bytes are the first 8
//! bytes of a frame header.
//!
//! Every run is first written to `*.tmp`, fsynced, renamed to its final name
//! and the parent directory is fsynced. Only then is it referenced from the
//! manifest — this ordering is the crash-safety boundary.

use std::io::Write;

use crate::crc32::Crc32;
use crate::error::{Error, Result};
use crate::fs::{BufReader, BufWriter, Fs, RFile, WFile};

pub const RUN_MAGIC: &[u8; 8] = b"EXTSORT1";
pub const END_MAGIC: &[u8; 8] = b"EXEND001";
pub const FRAME_HEADER_LEN: usize = 16;
pub const TRAILER_PAYLOAD_LEN: usize = 12;

/// One record in a run: the original input line plus its global sequence
/// number (0-based, assigned in input order). The sequence number is the
/// stability tie-breaker.
#[derive(Clone, Debug)]
pub struct Record {
    pub seq: u64,
    pub line: Vec<u8>,
}

/// Summary recovered from a verified run trailer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RunInfo {
    pub count: u64,
    pub aggr_crc: u32,
}

enum Frame {
    Record(Record),
    End(RunInfo),
}

// ---------------------------------------------------------------------------
// Writer
// ---------------------------------------------------------------------------

pub struct RunWriter {
    out: BufWriter<Box<dyn WFile>>,
    aggr: Crc32,
    count: u64,
}

impl RunWriter {
    /// Creates a new run at `tmp_rel`. The caller renames after [`Self::finish`].
    pub fn create(fs: &dyn Fs, tmp_rel: &str, io_buf: usize) -> Result<Self> {
        let raw = fs.create_write(tmp_rel)?;
        let mut out = BufWriter::new(raw, io_buf);
        out.write_all(RUN_MAGIC)?;
        Ok(RunWriter { out, aggr: Crc32::new(), count: 0 })
    }

    pub fn write_record(&mut self, rec: &Record) -> Result<()> {
        let mut header = Vec::with_capacity(FRAME_HEADER_LEN);
        header.write_all(&rec.seq.to_le_bytes())?;
        header.write_all(&(rec.line.len() as u32).to_le_bytes())?;

        let mut crc = Crc32::new();
        crc.update(&header);
        crc.update(&rec.line);
        let frame_crc = crc.finish();
        header.write_all(&frame_crc.to_le_bytes())?;

        self.aggr.update(&header);
        self.aggr.update(&rec.line);
        self.out.write_all(&header)?;
        self.out.write_all(&rec.line)?;
        self.count += 1;
        Ok(())
    }

    /// Writes the trailer and fsyncs. The temp file is now complete; the
    /// caller must still rename + directory-fsync.
    pub fn finish(mut self) -> Result<RunInfo> {
        let aggr_crc = self.aggr.finish();
        let mut payload = Vec::with_capacity(TRAILER_PAYLOAD_LEN);
        payload.write_all(&self.count.to_le_bytes())?;
        payload.write_all(&aggr_crc.to_le_bytes())?;
        let crc = Crc32::checksum(&payload);
        self.out.write_all(END_MAGIC)?;
        self.out.write_all(&payload)?;
        self.out.write_all(&crc.to_le_bytes())?;
        self.out.sync_all()?;
        Ok(RunInfo { count: self.count, aggr_crc })
    }
}

// ---------------------------------------------------------------------------
// Reader
// ---------------------------------------------------------------------------

struct FrameReader {
    file: BufReader<Box<dyn RFile>>,
}

impl FrameReader {
    fn new(file: Box<dyn RFile>, io_buf: usize) -> Self {
        FrameReader { file: BufReader::new(file, io_buf) }
    }

    fn read_exact(&mut self, buf: &mut [u8]) -> Result<()> {
        self.file.read_exact(buf).map_err(|e| {
            if e.kind() == std::io::ErrorKind::UnexpectedEof {
                Error::corrupt("truncated run file")
            } else {
                Error::Io(e)
            }
        })
    }

    fn read_magic8(&mut self) -> Result<[u8; 8]> {
        let mut m = [0u8; 8];
        self.read_exact(&mut m)?;
        Ok(m)
    }

    fn read_trailer(&mut self) -> Result<RunInfo> {
        let mut payload = [0u8; TRAILER_PAYLOAD_LEN];
        self.read_exact(&mut payload)?;
        let mut tcrc = [0u8; 4];
        self.read_exact(&mut tcrc)?;
        if Crc32::checksum(&payload) != u32::from_le_bytes(tcrc) {
            return Err(Error::corrupt("trailer CRC mismatch"));
        }
        Ok(RunInfo {
            count: u64::from_le_bytes(payload[0..8].try_into().unwrap()),
            aggr_crc: u32::from_le_bytes(payload[8..12].try_into().unwrap()),
        })
    }

    fn finish_frame(&mut self, prefix: [u8; 8], aggr: Option<&mut Crc32>) -> Result<Record> {
        let mut header = vec![0u8; FRAME_HEADER_LEN];
        header[..8].copy_from_slice(&prefix);
        self.read_exact(&mut header[8..])?;

        let seq = u64::from_le_bytes(header[0..8].try_into().unwrap());
        let ulen = u32::from_le_bytes(header[8..12].try_into().unwrap()) as usize;
        let want_crc = u32::from_le_bytes(header[12..16].try_into().unwrap());

        let mut line = vec![0u8; ulen];
        self.read_exact(&mut line)?;

        let mut crc = Crc32::new();
        crc.update(&header[..12]);
        crc.update(&line);
        if crc.finish() != want_crc {
            return Err(Error::corrupt(format!("frame CRC mismatch (seq={seq})")));
        }
        if let Some(aggr) = aggr {
            aggr.update(&header);
            aggr.update(&line);
        }
        Ok(Record { seq, line })
    }

    fn next(&mut self, aggr: Option<&mut Crc32>) -> Result<Frame> {
        let probe = self.read_magic8()?;
        if &probe == END_MAGIC {
            let info = self.read_trailer()?;
            let mut extra = [0u8; 1];
            match self.file.read(&mut extra) {
                Ok(0) => Ok(Frame::End(info)),
                Ok(_) => Err(Error::corrupt("trailing garbage after run trailer")),
                Err(e) => Err(Error::Io(e)),
            }
        } else {
            Ok(Frame::Record(self.finish_frame(probe, aggr)?))
        }
    }
}

/// Verifies a run end to end: magic, every frame CRC, the aggregate CRC and
/// the trailer.
pub fn verify_run(fs: &dyn Fs, rel: &str, io_buf: usize) -> Result<RunInfo> {
    let file = fs.open_read(rel)?;
    let mut r = FrameReader::new(file, io_buf);

    if r.read_magic8()? != *RUN_MAGIC {
        return Err(Error::corrupt(format!("{rel}: bad run magic")));
    }

    let mut aggr = Crc32::new();
    let mut count = 0u64;
    let info = loop {
        match r.next(Some(&mut aggr))? {
            Frame::Record(_) => count += 1,
            Frame::End(info) => break info,
        }
    };
    if info.count != count {
        return Err(Error::corrupt(format!(
            "{rel}: record count mismatch: trailer={} streamed={count}",
            info.count
        )));
    }
    if info.aggr_crc != aggr.finish() {
        return Err(Error::corrupt(format!("{rel}: aggregate CRC mismatch")));
    }
    Ok(info)
}

/// Streaming reader over a verified run used by the merge pass.
pub struct RunStream {
    r: FrameReader,
    finished: bool,
}

impl RunStream {
    /// Opens and performs the structural magic check; the run is expected to
    /// have been verified via [`verify_run`] already.
    pub fn open(fs: &dyn Fs, rel: &str, io_buf: usize) -> Result<Self> {
        let file = fs.open_read(rel)?;
        let mut r = FrameReader::new(file, io_buf);
        if r.read_magic8()? != *RUN_MAGIC {
            return Err(Error::corrupt(format!("{rel}: bad run magic")));
        }
        Ok(RunStream { r, finished: false })
    }

    /// Reads the next record, or `Ok(None)` once the trailer is reached. The
    /// trailer is fully consumed and its CRC checked.
    pub fn next_record(&mut self) -> Result<Option<Record>> {
        if self.finished {
            return Ok(None);
        }
        match self.r.next(None)? {
            Frame::Record(rec) => Ok(Some(rec)),
            Frame::End(_) => {
                self.finished = true;
                Ok(None)
            }
        }
    }
}
