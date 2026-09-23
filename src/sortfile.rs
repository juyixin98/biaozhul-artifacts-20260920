//! On-disk run-segment format and its sequential writer/reader.
//!
//! # Disk format
//!
//! A run is one *committed* (renamed + fsynced) file:
//!
//! ```text
//! magic   : 8 bytes = b"EXTSORT\x02"
//! frames  : zero or more record frames
//! trailer : frame type byte = FRAME_TRAILER, then u64 LE record count
//! ```
//!
//! Every frame begins with a 1-byte discriminator so a pure sequential reader
//! can tell records from the trailer:
//!
//! ```text
//! FRAME_CHUNK   = 0x10
//! FRAME_TRAILER = 0x20
//! ```
//!
//! Chunk frame layout:
//!
//! ```text
//! ftype   : u8        FRAME_CHUNK
//! flags   : u8        bit0 = FIRST chunk of a record
//!                     bit1 = CONT  — another chunk follows this one
//!                     single-chunk record: FIRST, no CONT
//!                     final chunk of multi-chunk record: neither bit set
//! length  : u32 LE    payload length (<= CHUNK_MAX)
//! crc32   : u32 LE    CRC-32/IEEE of the payload
//! payload : `length` bytes
//! ```
//!
//! The FIRST chunk payload is:
//!
//! ```text
//! u64 LE   seq        original input sequence number (stability tie-break)
//! u32 LE   key_count  number of key components
//! per key: u32 LE len + len key bytes
//! bytes    value prefix (record bytes excluding the line terminator)
//! ```
//!
//! Line terminators are not persisted: output normalization adds exactly one
//! `\n` after every emitted record (including the last).
//!
//! Later chunks hold only continuation value bytes. This lets a record of
//! unbounded length be spilled with bounded memory: a giant line streams
//! chunk-by-chunk while its (bounded) key stays in the head.
//!
//! Integrity: each chunk carries a payload CRC verified as bytes are consumed;
//! the trailer repeats the record count, detecting torn tails; magic detects a
//! wrong/truncated file.

use std::io;

use crate::crc::Crc;
use crate::error::{Error, Result};
use crate::io::VFile;

/// Magic prefix of every run file.
pub const MAGIC: &[u8; 8] = b"EXTSORT\x02";

const FRAME_CHUNK: u8 = 0x10;
const FRAME_TRAILER: u8 = 0x20;

const FLAG_FIRST: u8 = 0x01;
const FLAG_CONT: u8 = 0x02;

/// Maximum payload bytes in one chunk.
pub const CHUNK_MAX: usize = 16 * 1024;
/// Value bytes targeted for a giant record's FIRST chunk.
pub const GIANT_HEAD_VALUE: usize = 8 * 1024;

const CHUNK_HDR_LEN: usize = 1 + 1 + 4 + 4; // ftype, flags, len, crc

/// Result of pulling bytes from a giant record's input source.
pub struct Pull {
    /// Bytes made available.
    pub bytes: usize,
    /// True iff these bytes end the record (the trailing newline is consumed
    /// by the drain and not included). Whether the record ended with a newline
    /// at all is known up front by the scanner and passed to
    /// [`RunWriter::write_giant`].
    pub end_of_record: bool,
}

/// Source streamed by [`RunWriter::write_giant`] for an over-large record.
pub trait GiantDrain {
    /// Fill the beginning of `buf` with up to `buf.len()` record bytes.
    fn pull(&mut self, buf: &mut [u8]) -> Result<Pull>;
}

/// Sequential run writer. Dropping without [`RunWriter::commit`] leaves a
/// `*.tmp` file that recovery ignores.
pub struct RunWriter {
    file: Box<dyn VFile>,
    tmp_path: String,
    final_path: String,
    records: u64,
}

impl RunWriter {
    pub fn create(vfs: &dyn crate::io::Vfs, dir: &str, name: &str) -> Result<Self> {
        let tmp_path = format!("{dir}/{name}.run.tmp");
        let final_path = format!("{dir}/{name}.run");
        let mut file = vfs.create(&tmp_path)?;
        file.write_all(MAGIC)?;
        Ok(RunWriter {
            file,
            tmp_path,
            final_path,
            records: 0,
        })
    }

    pub fn record_count(&self) -> u64 {
        self.records
    }
    pub fn tmp_path(&self) -> &str {
        &self.tmp_path
    }
    pub fn final_path(&self) -> &str {
        &self.final_path
    }

    fn key_overhead(keys: &[Vec<u8>]) -> usize {
        8 + 4 + keys.iter().map(|k| 4 + k.len()).sum::<usize>()
    }

    fn encode_head(seq: u64, keys: &[Vec<u8>], prefix: &[u8]) -> Vec<u8> {
        let mut p = Vec::with_capacity(Self::key_overhead(keys) + prefix.len());
        p.extend_from_slice(&seq.to_le_bytes());
        p.extend_from_slice(&(keys.len() as u32).to_le_bytes());
        for k in keys {
            p.extend_from_slice(&(k.len() as u32).to_le_bytes());
            p.extend_from_slice(k);
        }
        p.extend_from_slice(prefix);
        p
    }

    fn write_chunk(&mut self, flags: u8, payload: &[u8]) -> Result<()> {
        debug_assert!(payload.len() <= CHUNK_MAX);
        let mut crc = Crc::new();
        crc.write(payload);
        let mut hdr = [0u8; CHUNK_HDR_LEN];
        hdr[0] = FRAME_CHUNK;
        hdr[1] = flags;
        hdr[2..6].copy_from_slice(&(payload.len() as u32).to_le_bytes());
        hdr[6..10].copy_from_slice(&crc.finish().to_le_bytes());
        self.file.write_all(&hdr)?;
        self.file.write_all(payload)
    }

    /// Write one fully-resident record.
    pub fn write_record(&mut self, seq: u64, keys: &[Vec<u8>], value: &[u8]) -> Result<()> {
        let overhead = Self::key_overhead(keys);
        if overhead + value.len() <= CHUNK_MAX {
            let payload = Self::encode_head(seq, keys, value);
            self.write_chunk(FLAG_FIRST, &payload)?;
        } else {
            // Fully buffered but too big for one chunk: stream from a slice.
            let head_value = (CHUNK_MAX - overhead).min(value.len());
            let payload = Self::encode_head(seq, keys, &value[..head_value]);
            self.write_chunk(FLAG_FIRST | FLAG_CONT, &payload)?;
            let mut pos = head_value;
            while pos < value.len() {
                let n = CHUNK_MAX.min(value.len() - pos);
                let end = pos + n == value.len();
                self.write_chunk(if end { 0 } else { FLAG_CONT }, &value[pos..pos + n])?;
                pos += n;
            }
        }
        self.records += 1;
        Ok(())
    }

    /// Stream a giant record. `prefix` is the buffered head value; the rest
    /// is pulled from `drain`.
    ///
    /// CONT flags are decided with one chunk of lookahead: a chunk is marked
    /// CONT only when a further byte is known to follow. This keeps a record
    /// that ends exactly on a chunk boundary from producing a zero-length
    /// tail chunk.
    pub fn write_giant(
        &mut self,
        seq: u64,
        keys: &[Vec<u8>],
        prefix: &[u8],
        drain: &mut dyn GiantDrain,
    ) -> Result<()> {
        let _overhead = Self::key_overhead(keys);
        let mut tmp = vec![0u8; CHUNK_MAX];

        // ---- Build the first chunk (head + value up to CHUNK_MAX) ----
        let mut pending = Self::encode_head(seq, keys, prefix);
        let mut ended = false;
        while pending.len() < CHUNK_MAX {
            let pull = drain.pull(&mut tmp[..CHUNK_MAX - pending.len()])?;
            if pull.bytes > 0 {
                pending.extend_from_slice(&tmp[..pull.bytes]);
            }
            if pull.end_of_record {
                ended = true;
                break;
            }
            if pull.bytes == 0 {
                return Err(Error::corrupt(
                    &self.tmp_path,
                    "giant drain returned no progress and no end",
                ));
            }
        }
        if ended {
            // Entire record fits in the head chunk.
            self.write_chunk(FLAG_FIRST, &pending)?;
            self.records += 1;
            return Ok(());
        }

        // ---- Continuation chunks, emitted with one-chunk lookahead ----
        let mut pending_is_first = true;
        loop {
            let mut cur: Vec<u8> = Vec::with_capacity(CHUNK_MAX);
            let mut eor = false;
            while cur.len() < CHUNK_MAX {
                let pull = drain.pull(&mut tmp[..CHUNK_MAX - cur.len()])?;
                if pull.bytes > 0 {
                    cur.extend_from_slice(&tmp[..pull.bytes]);
                }
                if pull.end_of_record {
                    eor = true;
                    break;
                }
                if pull.bytes == 0 {
                    return Err(Error::corrupt(
                        &self.tmp_path,
                        "giant drain stalled mid-record",
                    ));
                }
            }

            if eor {
                if cur.is_empty() {
                    // Record ended exactly as `pending` filled: pending is the
                    // true last chunk (no CONT, no extra chunk).
                    let f = if pending_is_first { FLAG_FIRST } else { 0 };
                    self.write_chunk(f, &pending)?;
                } else {
                    // pending has more after it; cur is the terminal chunk.
                    let pf = if pending_is_first {
                        FLAG_FIRST | FLAG_CONT
                    } else {
                        FLAG_CONT
                    };
                    self.write_chunk(pf, &pending)?;
                    self.write_chunk(0, &cur)?;
                }
                break;
            }

            // cur is a full chunk => at least one byte follows pending.
            let pf = if pending_is_first {
                FLAG_FIRST | FLAG_CONT
            } else {
                FLAG_CONT
            };
            self.write_chunk(pf, &pending)?;
            pending = cur;
            pending_is_first = false;
        }

        self.records += 1;
        Ok(())
    }

    /// Write trailer, fsync, close, atomically rename to the committed name.
    pub fn commit(mut self, vfs: &dyn crate::io::Vfs) -> Result<(String, u64)> {
        let mut tr = [0u8; 9];
        tr[0] = FRAME_TRAILER;
        tr[1..9].copy_from_slice(&self.records.to_le_bytes());
        self.file.write_all(&tr)?;
        self.file.flush()?;
        self.file.sync_all()?;
        drop(self.file);
        vfs.rename(&self.tmp_path, &self.final_path)?;
        Ok((self.final_path, self.records))
    }
}

/// Decoded head of one run record.
#[derive(Debug)]
pub struct RecordHead {
    pub seq: u64,
    pub keys: Vec<Vec<u8>>,
    /// Value bytes carried in the FIRST chunk (whole value for small records).
    pub prefix: Vec<u8>,
    /// CONT flag of the first chunk: true when value continues.
    pub has_more: bool,
}

/// Reader position.
#[derive(PartialEq, Eq)]
enum Pos {
    /// Magic verified, nothing read yet.
    Start,
    /// Last thing consumed was a chunk with CONT clear; next is frame type.
    RecordBoundary,
    /// Inside a record, `remaining` bytes of current chunk left.
    InChunk,
    /// Trailer frame read; nothing follows.
    Trailer,
}

/// Sequential run reader verifying per-chunk CRCs.
pub struct RunReader {
    file: Box<dyn VFile>,
    path: String,
    pos: Pos,
    flags: u8,
    remaining: u32,
    crc: Crc,
    expected_crc: u32,
    records_seen: u64,
}

impl RunReader {
    pub fn open(vfs: &dyn crate::io::Vfs, path: &str) -> Result<Self> {
        let mut file = vfs.open(path)?;
        let mut magic = [0u8; 8];
        read_exact(&mut file, &mut magic, path)?;
        if &magic != MAGIC {
            return Err(Error::corrupt(path, "bad magic header"));
        }
        Ok(RunReader {
            file,
            path: path.to_string(),
            pos: Pos::Start,
            flags: 0,
            remaining: 0,
            crc: Crc::new(),
            expected_crc: 0,
            records_seen: 0,
        })
    }

    pub fn path(&self) -> &str {
        &self.path
    }

    fn read_u8(&mut self) -> Result<Option<u8>> {
        let mut b = [0u8; 1];
        match read_exact_opt(&mut self.file, &mut b) {
            Ok(()) => Ok(Some(b[0])),
            Err(Error::Io(e, _)) if e.kind() == io::ErrorKind::UnexpectedEof => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Read the next chunk header (called at a frame boundary). Returns false
    /// if the trailer is found instead.
    fn enter_frame(&mut self) -> Result<bool> {
        let ftype = self
            .read_u8()?
            .ok_or_else(|| Error::corrupt(&self.path, "EOF where frame expected"))?;
        if ftype == FRAME_TRAILER {
            self.pos = Pos::Trailer;
            return Ok(false);
        }
        if ftype != FRAME_CHUNK {
            return Err(Error::corrupt(
                &self.path,
                format!("invalid frame type byte 0x{ftype:02x}"),
            ));
        }
        let mut rest = [0u8; CHUNK_HDR_LEN - 1];
        read_exact(&mut self.file, &mut rest, &self.path)?;
        self.flags = rest[0];
        self.remaining = u32::from_le_bytes([rest[1], rest[2], rest[3], rest[4]]);
        self.expected_crc = u32::from_le_bytes([rest[5], rest[6], rest[7], rest[8]]);
        if self.remaining as usize > CHUNK_MAX {
            return Err(Error::corrupt(
                &self.path,
                format!("chunk length {} exceeds frame max", self.remaining),
            ));
        }
        self.crc = Crc::new();
        self.pos = Pos::InChunk;
        Ok(true)
    }

    /// Discard the rest of the current chunk and verify its CRC.
    fn discard_chunk_body(&mut self) -> Result<()> {
        let mut buf = [0u8; 8192];
        while self.remaining > 0 {
            let want = buf.len().min(self.remaining as usize);
            let n = self.file.read(&mut buf[..want])?;
            if n == 0 {
                return Err(Error::corrupt(&self.path, "chunk payload truncated"));
            }
            self.crc.write(&buf[..n]);
            self.remaining -= n as u32;
        }
        self.check_crc()
    }

    fn check_crc(&mut self) -> Result<()> {
        if self.crc.finish() != self.expected_crc {
            return Err(Error::corrupt(
                &self.path,
                "chunk CRC mismatch — segment corrupted",
            ));
        }
        if self.flags & FLAG_CONT == 0 {
            self.pos = Pos::RecordBoundary;
        }
        Ok(())
    }

    /// Skip every remaining chunk of the current record.
    pub fn drain_record(&mut self) -> Result<()> {
        // Current (possibly partly consumed) chunk.
        if self.pos == Pos::InChunk {
            self.discard_chunk_body()?;
        }
        // Following CONT chunks.
        while self.flags & FLAG_CONT != 0 {
            if !self.enter_frame()? {
                return Err(Error::corrupt(&self.path, "trailer inside record"));
            }
            if self.flags & FLAG_FIRST != 0 {
                return Err(Error::corrupt(
                    &self.path,
                    "FIRST chunk before record ended",
                ));
            }
            self.discard_chunk_body()?;
        }
        Ok(())
    }

    /// Decode the next record head. Returns None at the trailer.
    pub fn next_head(&mut self) -> Result<Option<RecordHead>> {
        // Finish and verify any record the caller did not fully consume:
        // drain_record handles a partial current chunk plus its CONT chain and
        // leaves us at a RecordBoundary (or Trailer).
        if self.pos == Pos::InChunk {
            self.drain_record()?;
        }
        if self.pos == Pos::Trailer {
            return Ok(None);
        }
        if !self.enter_frame()? {
            return Ok(None);
        }
        if self.flags & FLAG_FIRST == 0 {
            return Err(Error::corrupt(
                &self.path,
                "record does not begin with a FIRST chunk",
            ));
        }
        // Pull the whole first payload (bounded by CHUNK_MAX).
        let len = self.remaining as usize;
        let mut payload = vec![0u8; len];
        let mut got = 0;
        while got < len {
            let n = self.file.read(&mut payload[got..])?;
            if n == 0 {
                return Err(Error::corrupt(&self.path, "first chunk truncated"));
            }
            self.crc.write(&payload[got..got + n]);
            self.remaining -= n as u32;
            got += n;
        }
        if self.crc.finish() != self.expected_crc {
            return Err(Error::corrupt(&self.path, "head chunk CRC mismatch"));
        }
        let has_more = self.flags & FLAG_CONT != 0;
        if !has_more {
            self.pos = Pos::RecordBoundary;
        }
        // Stay InChunk if has_more: remaining == 0 signals "enter next frame".
        let head = decode_head(&self.path, &payload)?;
        self.records_seen += 1;
        Ok(Some(RecordHead {
            seq: head.seq,
            keys: head.keys,
            prefix: head.prefix,
            has_more,
        }))
    }

    /// Read continuation value bytes. Returns 0 at end of record.
    pub fn read_value(&mut self, buf: &mut [u8]) -> Result<usize> {
        if buf.is_empty() || self.pos == Pos::RecordBoundary {
            return Ok(0);
        }
        if self.remaining == 0 {
            // Enter the next chunk of this record.
            if !self.enter_frame()? {
                return Err(Error::corrupt(&self.path, "trailer inside record value"));
            }
            if self.flags & FLAG_FIRST != 0 {
                return Err(Error::corrupt(
                    &self.path,
                    "FIRST chunk inside a record value",
                ));
            }
        }
        let want = buf.len().min(self.remaining as usize);
        let n = self.file.read(&mut buf[..want])?;
        if n == 0 {
            return Err(Error::corrupt(&self.path, "value chunk truncated"));
        }
        self.crc.write(&buf[..n]);
        self.remaining -= n as u32;
        if self.remaining == 0 {
            self.check_crc()?;
        }
        Ok(n)
    }

    /// Verify the trailer and record count. Consumes any records the caller
    /// never visited so corruption later in a run is still caught.
    pub fn finish(mut self) -> Result<u64> {
        // Walk every remaining record; next_head drains skipped records and
        // read_value drains value chunks, CRC-checking all of them.
        let mut buf = [0u8; 8192];
        while let Some(head) = self.next_head()? {
            while self.read_value(&mut buf)? > 0 {}
            let _ = head.seq;
        }
        // Now positioned at the trailer; read its 8-byte count.
        let mut count_bytes = [0u8; 8];
        read_exact(&mut self.file, &mut count_bytes, &self.path)?;
        let count = u64::from_le_bytes(count_bytes);
        if self.read_u8()?.is_some() {
            return Err(Error::corrupt(&self.path, "bytes after trailer"));
        }
        if count != self.records_seen {
            return Err(Error::corrupt(
                &self.path,
                format!(
                    "trailer reports {count} records but {0} seen",
                    self.records_seen
                ),
            ));
        }
        Ok(count)
    }
}

struct HeadDecoded {
    seq: u64,
    keys: Vec<Vec<u8>>,
    prefix: Vec<u8>,
}

fn decode_head(path: &str, p: &[u8]) -> Result<HeadDecoded> {
    if p.len() < 12 {
        return Err(Error::corrupt(path, "head chunk too short"));
    }
    let seq = u64::from_le_bytes(p[0..8].try_into().unwrap());
    let key_count = u32::from_le_bytes(p[8..12].try_into().unwrap()) as usize;
    let mut pos = 12;
    let mut keys = Vec::with_capacity(key_count);
    for _ in 0..key_count {
        if pos + 4 > p.len() {
            return Err(Error::corrupt(path, "truncated key length"));
        }
        let kl = u32::from_le_bytes(p[pos..pos + 4].try_into().unwrap()) as usize;
        pos += 4;
        if pos + kl > p.len() {
            return Err(Error::corrupt(path, "truncated key bytes"));
        }
        keys.push(p[pos..pos + kl].to_vec());
        pos += kl;
    }
    Ok(HeadDecoded {
        seq,
        keys,
        prefix: p[pos..].to_vec(),
    })
}

fn read_exact(f: &mut Box<dyn VFile>, buf: &mut [u8], path: &str) -> Result<()> {
    let mut got = 0;
    while got < buf.len() {
        let n = f.read(&mut buf[got..])?;
        if n == 0 {
            return Err(Error::corrupt(
                path,
                format!("unexpected EOF (wanted {} bytes)", buf.len()),
            ));
        }
        got += n;
    }
    Ok(())
}

fn read_exact_opt(f: &mut Box<dyn VFile>, buf: &mut [u8]) -> Result<()> {
    let mut got = 0;
    while got < buf.len() {
        let n = f.read(&mut buf[got..])?;
        if n == 0 {
            return Err(Error::io(
                io::Error::new(io::ErrorKind::UnexpectedEof, "eof"),
                String::new(),
            ));
        }
        got += n;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::io::RealVfs;

    fn tmp_dir(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("extsort-sf-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    struct SliceDrain<'a> {
        data: &'a [u8],
        pos: usize,
    }
    impl<'a> GiantDrain for SliceDrain<'a> {
        fn pull(&mut self, buf: &mut [u8]) -> Result<Pull> {
            // Deliberately small steps to exercise multi-chunk boundaries.
            let step = (buf.len() / 3 + 1)
                .min(buf.len())
                .min(self.data.len() - self.pos);
            buf[..step].copy_from_slice(&self.data[self.pos..self.pos + step]);
            self.pos += step;
            Ok(Pull {
                bytes: step,
                end_of_record: self.pos == self.data.len(),
            })
        }
    }

    fn read_all_value(r: &mut RunReader, head: &RecordHead) -> Vec<u8> {
        let mut out = head.prefix.clone();
        let mut buf = [0u8; 4096];
        loop {
            let z = r.read_value(&mut buf).unwrap();
            if z == 0 {
                break;
            }
            out.extend_from_slice(&buf[..z]);
        }
        out
    }

    #[test]
    fn small_records_roundtrip() {
        let d = tmp_dir("small");
        let vfs = RealVfs::new(&d);
        let mut w = RunWriter::create(&vfs, ".", "r1").unwrap();
        w.write_record(0, &[b"key1".to_vec()], b"hello").unwrap();
        w.write_record(1, &[b"key2".to_vec()], b"world").unwrap();
        let (path, n) = w.commit(&vfs).unwrap();
        assert_eq!(n, 2);

        let mut r = RunReader::open(&vfs, &path).unwrap();
        let h0 = r.next_head().unwrap().unwrap();
        assert_eq!(h0.seq, 0);
        assert!(!h0.has_more);
        assert_eq!(h0.prefix, b"hello");
        assert_eq!(read_all_value(&mut r, &h0), b"hello");
        let h1 = r.next_head().unwrap().unwrap();
        assert_eq!(read_all_value(&mut r, &h1), b"world");
        assert!(r.next_head().unwrap().is_none());
        r.finish().unwrap();
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn giant_record_roundtrip() {
        let d = tmp_dir("giant");
        let vfs = RealVfs::new(&d);
        let big = vec![b'x'; 100_000];
        let mut w = RunWriter::create(&vfs, ".", "g").unwrap();
        let mut drain = SliceDrain { data: &big, pos: 0 };
        w.write_giant(7, &[b"k".to_vec()], b"abc", &mut drain)
            .unwrap();
        let (path, n) = w.commit(&vfs).unwrap();
        assert_eq!(n, 1);

        let mut r = RunReader::open(&vfs, &path).unwrap();
        let h = r.next_head().unwrap().unwrap();
        assert_eq!(h.seq, 7);
        assert!(h.has_more);
        let out = read_all_value(&mut r, &h);
        assert_eq!(&out[..3], b"abc");
        assert_eq!(out.len(), 3 + big.len());
        assert!(out[3..].iter().all(|&b| b == b'x'));
        assert!(r.next_head().unwrap().is_none());
        r.finish().unwrap();
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn exactly_one_giant_chunk_works() {
        // Value fits in the head chunk but writer is driven through drain.
        let d = tmp_dir("onechunk");
        let vfs = RealVfs::new(&d);
        let val = vec![b'q'; 100];
        let mut w = RunWriter::create(&vfs, ".", "o").unwrap();
        let mut drain = SliceDrain { data: &val, pos: 0 };
        w.write_giant(1, &[b"k".to_vec()], b"z", &mut drain)
            .unwrap();
        let (path, _) = w.commit(&vfs).unwrap();
        let mut r = RunReader::open(&vfs, &path).unwrap();
        let h = r.next_head().unwrap().unwrap();
        assert!(!h.has_more);
        let out = read_all_value(&mut r, &h);
        assert_eq!(out.len(), 1 + val.len());
        r.finish().unwrap();
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn empty_run_valid() {
        let d = tmp_dir("empty");
        let vfs = RealVfs::new(&d);
        let w = RunWriter::create(&vfs, ".", "e").unwrap();
        let (path, n) = w.commit(&vfs).unwrap();
        assert_eq!(n, 0);
        let mut r = RunReader::open(&vfs, &path).unwrap();
        assert!(r.next_head().unwrap().is_none());
        assert_eq!(r.finish().unwrap(), 0);
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn detects_crc_corruption() {
        let d = tmp_dir("crc");
        let vfs = RealVfs::new(&d);
        let mut w = RunWriter::create(&vfs, ".", "c").unwrap();
        w.write_record(0, &[b"k".to_vec()], b"payload").unwrap();
        let (path, _) = w.commit(&vfs).unwrap();
        let real = d.join("c.run");
        crate::io::corrupt_overwrite(&real, 40, &[0xFF]).unwrap();
        let mut r = RunReader::open(&vfs, &path).unwrap();
        let err = r.next_head().unwrap_err();
        assert!(matches!(err, Error::Corrupt { .. }), "got {err:?}");
        std::fs::remove_dir_all(&d).ok();
    }

    #[test]
    fn detects_bad_magic_and_tail() {
        let d = tmp_dir("magic");
        let vfs = RealVfs::new(&d);
        let mut w = RunWriter::create(&vfs, ".", "m").unwrap();
        w.write_record(0, &[b"k".to_vec()], b"abc").unwrap();
        let (path, _) = w.commit(&vfs).unwrap();
        let real = d.join("m.run");
        // Corrupt the very first magic byte.
        crate::io::corrupt_overwrite(&real, 0, &[0x00]).unwrap();
        let r = RunReader::open(&vfs, &path);
        assert!(matches!(r, Err(Error::Corrupt { .. })));

        // Truncate the trailer.
        let mut w2 = RunWriter::create(&vfs, ".", "m2").unwrap();
        w2.write_record(0, &[b"k".to_vec()], b"abc").unwrap();
        let (p2, _) = w2.commit(&vfs).unwrap();
        let real2 = d.join("m2.run");
        let len = std::fs::metadata(&real2).unwrap().len();
        crate::io::corrupt_truncate(&real2, len - 2).unwrap();
        let mut rr = RunReader::open(&vfs, &p2).unwrap();
        rr.next_head().unwrap();
        assert!(matches!(rr.finish().unwrap_err(), Error::Corrupt { .. }));
        std::fs::remove_dir_all(&d).ok();
    }
}
