//! Streaming compressor. Memory use is bounded: a 256-entry frequency table,
//! the code table, and fixed-size I/O buffers — independent of input size.

use crate::bitio::BitWriter;
use crate::error::{Error, Result};
use crate::table::{build_lengths, CodeTable};
use std::io::{Read, Write};

/// Stream magic, includes the format version (`1`).
pub const MAGIC: &[u8; 4] = b"CHF1";

/// Default cap on input size for the CLI (256 MiB).
pub const DEFAULT_MAX_INPUT_BYTES: u64 = 256 * 1024 * 1024;

/// I/O chunk size for streaming passes.
pub const CHUNK: usize = 64 * 1024;

/// Count symbol frequencies in a streaming pass, enforcing `max_bytes`.
/// Returns the frequency table and the total number of bytes read.
pub fn count_frequencies<R: Read>(mut r: R, max_bytes: u64) -> Result<([u64; 256], u64)> {
    let mut freqs = [0u64; 256];
    let mut total = 0u64;
    let mut buf = [0u8; CHUNK];
    loop {
        let n = r.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > max_bytes {
            return Err(Error::InputLimitExceeded { limit: max_bytes });
        }
        for &b in &buf[..n] {
            freqs[b as usize] += 1;
        }
    }
    Ok((freqs, total))
}

/// Write the stream header: magic, original length, and the code-length
/// table as `(symbol, length)` pairs sorted by symbol. Returns bytes written.
pub fn write_header<W: Write>(w: &mut W, table: &CodeTable, original_len: u64) -> Result<u64> {
    let mut size = 0u64;
    w.write_all(MAGIC)?;
    size += 4;
    w.write_all(&original_len.to_le_bytes())?;
    size += 8;
    let k = table.symbol_count() as u16;
    w.write_all(&k.to_le_bytes())?;
    size += 2;
    for s in 0..256usize {
        let l = table.lengths[s];
        if l > 0 {
            w.write_all(&[s as u8, l])?;
            size += 2;
        }
    }
    Ok(size)
}

/// Streaming encoder: writes the header on creation, then accepts input in
/// arbitrarily sized chunks. Memory use is O(1) in the input size.
pub struct StreamEncoder<W: Write> {
    bw: BitWriter<W>,
    table: CodeTable,
}

impl<W: Write> StreamEncoder<W> {
    pub fn new(mut w: W, table: CodeTable, original_len: u64) -> Result<Self> {
        write_header(&mut w, &table, original_len)?;
        Ok(StreamEncoder {
            bw: BitWriter::new(w),
            table,
        })
    }

    pub fn write_chunk(&mut self, data: &[u8]) -> Result<()> {
        for &b in data {
            let (code, len) = self.table.code(b);
            self.bw.write_code(code, len)?;
        }
        Ok(())
    }

    /// Pad the final byte, flush, and return the underlying writer.
    pub fn finish(self) -> Result<W> {
        self.bw.finish()
    }
}

/// One-shot convenience wrapper used by tests and small inputs.
pub fn compress_slice(data: &[u8]) -> Result<Vec<u8>> {
    let mut freqs = [0u64; 256];
    for &b in data {
        freqs[b as usize] += 1;
    }
    let table = CodeTable::from_lengths(build_lengths(&freqs))?;
    let mut enc = StreamEncoder::new(Vec::new(), table, data.len() as u64)?;
    enc.write_chunk(data)?;
    enc.finish()
}
