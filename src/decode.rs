//! Streaming decompressor with strict header validation, output limits, and
//! a configurable policy for incomplete code tables.

use crate::bitio::BitReader;
use crate::encode::MAGIC;
use crate::error::{map_eof, Error, Result};
use crate::table::{CodeTable, MAX_CODE_LEN};
use std::io::Read;

/// Default cap on decompressed output for the CLI (256 MiB).
pub const DEFAULT_MAX_OUTPUT_BYTES: u64 = 256 * 1024 * 1024;

/// Policy for code tables whose Kraft sum is below 1 (incomplete).
///
/// Canonical encoders legitimately emit incomplete tables (e.g. a single
/// distinct symbol), so the default is `Permit`: decoding proceeds and an
/// undefined bit sequence is reported as `Error::UndefinedCode`. `Reject`
/// refuses such tables up front. Oversubscribed tables are always rejected
/// regardless of this policy.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum IncompletePolicy {
    Permit,
    Reject,
}

#[derive(Clone, Copy, Debug)]
pub struct DecodeOptions {
    pub max_output_bytes: u64,
    pub incomplete_policy: IncompletePolicy,
}

impl Default for DecodeOptions {
    fn default() -> Self {
        DecodeOptions {
            max_output_bytes: DEFAULT_MAX_OUTPUT_BYTES,
            incomplete_policy: IncompletePolicy::Permit,
        }
    }
}

/// Streaming decoder. Created over a reader positioned at the stream start;
/// the header is parsed and validated eagerly. Memory use is O(1).
pub struct StreamDecoder<R: Read> {
    br: BitReader<R>,
    table: CodeTable,
    remaining: u64,
}

impl<R: Read> StreamDecoder<R> {
    pub fn new(mut r: R, opts: &DecodeOptions) -> Result<Self> {
        let mut magic = [0u8; 4];
        r.read_exact(&mut magic).map_err(map_eof)?;
        if &magic != MAGIC {
            return Err(Error::BadMagic);
        }
        let mut b8 = [0u8; 8];
        r.read_exact(&mut b8).map_err(map_eof)?;
        let original_len = u64::from_le_bytes(b8);
        if original_len > opts.max_output_bytes {
            return Err(Error::OutputTooLarge {
                needed: original_len,
                limit: opts.max_output_bytes,
            });
        }
        let mut b2 = [0u8; 2];
        r.read_exact(&mut b2).map_err(map_eof)?;
        let k = u16::from_le_bytes(b2) as usize;

        let mut lengths = [0u8; 256];
        let mut seen = [false; 256];
        for _ in 0..k {
            let mut e = [0u8; 2];
            r.read_exact(&mut e).map_err(map_eof)?;
            let (sym, len) = (e[0], e[1]);
            if seen[sym as usize] {
                return Err(Error::DuplicateSymbol(sym));
            }
            seen[sym as usize] = true;
            if len == 0 || len > MAX_CODE_LEN {
                return Err(Error::InvalidCodeLength(len));
            }
            lengths[sym as usize] = len;
        }

        let table = CodeTable::from_lengths(lengths)?;
        if !table.complete && opts.incomplete_policy == IncompletePolicy::Reject {
            return Err(Error::IncompleteTable);
        }
        if original_len > 0 && table.symbol_count() == 0 {
            return Err(Error::EmptyTableWithData);
        }

        Ok(StreamDecoder {
            br: BitReader::new(r),
            table,
            remaining: original_len,
        })
    }

    /// Bytes still expected according to the header.
    pub fn remaining(&self) -> u64 {
        self.remaining
    }

    /// Decode up to `out.len()` bytes. Returns the number written;
    /// 0 means the declared output has been fully produced.
    pub fn decode_chunk(&mut self, out: &mut [u8]) -> Result<usize> {
        let table = &self.table;
        let br = &mut self.br;
        let mut n = 0;
        while n < out.len() && self.remaining > 0 {
            let sym = table.decode_symbol(|| br.read_bit())?;
            out[n] = sym;
            n += 1;
            self.remaining -= 1;
        }
        Ok(n)
    }

    /// Strict end-of-stream check; call after `decode_chunk` returns 0.
    pub fn finish(self) -> Result<()> {
        self.br.finish_check()
    }
}

/// One-shot convenience wrapper used by tests and small inputs.
pub fn decompress_slice(data: &[u8], opts: &DecodeOptions) -> Result<Vec<u8>> {
    let mut dec = StreamDecoder::new(data, opts)?;
    let mut out = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        let n = dec.decode_chunk(&mut buf)?;
        if n == 0 {
            break;
        }
        out.extend_from_slice(&buf[..n]);
    }
    dec.finish()?;
    Ok(out)
}
