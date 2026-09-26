//! Integer-range arithmetic encoder and decoder.
//!
//! The implementation follows the classic finite-precision interval coder:
//! state is the half-open integer interval `[low, high)` within
//! `[0, 2^CODE_BITS)`. After narrowing to a symbol's cumulative-frequency
//! interval the coder renormalizes with the three standard cases
//! (Mark Nelson / Witten–Neal–Cleary style):
//!
//! * **E1** — `high < HALF`: the whole interval is in the lower half; emit a
//!   `0`, followed by `pending` ones (resolving carries deferred during E3).
//! * **E2** — `low >= HALF`: upper half; emit a `1`, followed by `pending`
//!   zeros.
//! * **E3** — `low >= QUARTER && high < 3*QUARTER`: the interval straddles the
//!   middle; widen it around `HALF` by subtracting `QUARTER`/adding relative
//!   offsets, increment `pending`, and defer the output bit.
//!
//! Encoder and decoder run the *same* renormalization on the *same* state
//! transitions, which is what makes the pending/carry bookkeeping mirror
//! exactly. The stream is terminated with an explicit EOF symbol; after it the
//! encoder flushes enough state bits to distinguish the final interval, and
//! the decoder stops as soon as EOF is decoded (any trailing/truncated bits
//! become irrelevant).

use crate::bitio::{BitReader, BitWriter};
use crate::constants::{CODE_BITS, EOF_SYMBOL, HALF, NUM_SYMBOLS, QUARTER, TOP};
use crate::error::{Error, Result};
use crate::model::Model;
use std::io::{Read, Write};

/// Diagnostics returned by a successful encode/decode run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stats {
    /// Payload bytes read (encode: source bytes; decode: coded payload bytes
    /// consumed, excluding the fixed container header).
    pub input_bytes: u64,
    /// Bytes written to the output sink.
    pub output_bytes: u64,
    /// Payload bits emitted/consumed by the range coder itself.
    pub coded_bits: u64,
    /// Number of model rescales that occurred.
    pub rescales: u64,
    /// Number of symbols coded (source bytes plus the EOF terminator).
    pub symbols: u64,
}

/// Streaming adaptive arithmetic encoder.
pub struct Encoder<W: Write> {
    bits: BitWriter<W>,
    model: Model,
    low: u64,
    high: u64,
    pending: u64,
    symbols: u64,
    /// Output length cap in bytes (payload bits ceiling; header excluded).
    max_output_bytes: Option<u64>,
}

impl<W: Write> Encoder<W> {
    /// Create an encoder over `out` with an unlimited payload size.
    pub fn new(out: W) -> Self {
        Encoder {
            bits: BitWriter::new(out),
            model: Model::new(),
            low: 0,
            high: TOP - 1,
            pending: 0,
            symbols: 0,
            max_output_bytes: None,
        }
    }

    /// Cap the number of payload bytes the encoder may emit. Encoding returns
    /// [`Error::OutputLimitExceeded`] instead of growing past the cap.
    pub fn with_output_limit(mut self, limit: u64) -> Self {
        self.max_output_bytes = Some(limit);
        self
    }

    /// Number of rescales on the internal model so far.
    pub fn rescales(&self) -> u64 {
        self.model.rescale_count()
    }

    #[inline]
    fn guard_output(&self) -> Result<()> {
        if let Some(limit) = self.max_output_bytes {
            // bits_written is incremented before this check, so +7 rounds up.
            if (self.bits.bits_written() + 7) / 8 > limit {
                return Err(Error::OutputLimitExceeded { limit });
            }
        }
        Ok(())
    }

    /// Emit a bit plus the carry-resolving run of opposite bits.
    #[inline]
    fn emit_with_pending(&mut self, bit: u8) -> Result<()> {
        self.bits.write_bit(bit)?;
        let fill = if bit == 0 { 1u8 } else { 0u8 };
        for _ in 0..self.pending {
            self.bits.write_bit(fill)?;
        }
        self.pending = 0;
        self.guard_output()
    }

    /// Narrow the interval to one symbol and renormalize.
    #[inline]
    pub fn encode_symbol(&mut self, symbol: u16) -> Result<()> {
        debug_assert!((symbol as usize) < NUM_SYMBOLS);
        let range = self.high - self.low + 1;
        let total = self.model.total() as u64;
        let cum_low = self.model.cumulative_low(symbol) as u64;
        let freq = self.model.count(symbol) as u64;

        self.high = self.low + range * (cum_low + freq) / total - 1;
        self.low += range * cum_low / total;

        loop {
            if self.high < HALF {
                // E1: lower half.
                self.emit_with_pending(0)?;
            } else if self.low >= HALF {
                // E2: upper half — shift both endpoints down by HALF.
                self.emit_with_pending(1)?;
                self.low -= HALF;
                self.high -= HALF;
            } else if self.low >= QUARTER && self.high < HALF + QUARTER {
                // E3: straddles the middle, defer the bit.
                self.pending += 1;
                self.low -= QUARTER;
                self.high -= QUARTER;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
            // Values stay in [0, TOP): after E1 low/high were < HALF;
            // after E2 they were shifted by HALF then were < HALF;
            // after E3 they were inside [0, HALF). Mask defensively.
            self.low &= TOP - 1;
            self.high &= TOP - 1;
        }

        self.model.update(symbol);
        self.symbols += 1;
        Ok(())
    }

    /// Feed one input byte.
    #[inline]
    pub fn write_byte(&mut self, byte: u8) -> Result<()> {
        self.encode_symbol(u16::from(byte))
    }

    /// Feed a chunk of input bytes.
    pub fn write_all_bytes(&mut self, data: &[u8]) -> Result<()> {
        for &b in data {
            self.write_byte(b)?;
        }
        Ok(())
    }

    /// Write the EOF terminator and flush the final interval, returning the
    /// wrapped sink plus [`Stats`].
    pub fn finish(mut self) -> Result<(W, Stats)> {
        self.encode_symbol(EOF_SYMBOL)?;

        // Final flush (Witten–Neal–Cleary): one extra pending bit selects the
        // final interval — 0 (+pending ones) when low is in the first quarter,
        // otherwise 1 (+pending zeros). Then CODE_BITS zero tail bits follow
        // so a valid stream never relies on the decoder's virtual zero tail;
        // this is what makes byte-truncated streams detectable as errors.
        self.pending += 1;
        if self.low < QUARTER {
            self.emit_with_pending(0)?;
        } else {
            self.emit_with_pending(1)?;
        }
        for _ in 0..CODE_BITS {
            self.bits.write_bit(0)?;
        }

        let (_w, bits_written, _padding) = self.bits.finish()?;
        let output_bytes = (bits_written + 7) / 8;
        let stats = Stats {
            input_bytes: self.symbols.saturating_sub(1),
            output_bytes,
            coded_bits: bits_written,
            rescales: self.model.rescale_count(),
            symbols: self.symbols,
        };
        Ok((_w, stats))
    }
}

/// Streaming adaptive arithmetic decoder.
pub struct Decoder<R: Read> {
    bits: BitReader<R>,
    model: Model,
    low: u64,
    high: u64,
    /// Current lookahead code, `CODE_BITS` bits wide.
    value: u64,
    symbols: u64,
    /// Cap on decoded payload bytes.
    max_output_bytes: u64,
}

impl<R: Read> Decoder<R> {
    /// Create a decoder. The first `CODE_BITS` bits are pulled lazily; if the
    /// stream is shorter the missing tail is treated as zero bits.
    pub fn new(reader: R) -> Self {
        Decoder {
            bits: BitReader::new(reader),
            model: Model::new(),
            low: 0,
            high: TOP - 1,
            value: 0,
            symbols: 0,
            max_output_bytes: crate::constants::DEFAULT_MAX_OUTPUT,
        }
    }

    /// Cap decoded payload length in bytes.
    pub fn with_output_limit(mut self, limit: u64) -> Self {
        self.max_output_bytes = limit;
        self
    }

    /// Number of rescales on the internal model so far.
    pub fn rescales(&self) -> u64 {
        self.model.rescale_count()
    }

    /// Pull the initial code word, treating an absent tail as zeros (this
    /// matches the encoder's zero padding in the final byte).
    fn prime(&mut self) -> Result<()> {
        for _ in 0..CODE_BITS {
            self.value = (self.value << 1) | u64::from(self.bits.read_bit()?);
        }
        Ok(())
    }

    /// Decode one symbol (may be the EOF terminator).
    #[inline]
    fn decode_symbol(&mut self) -> Result<u16> {
        let range = self.high - self.low + 1;
        let total = self.model.total() as u64;
        // Scaled cumulative target: largest t with
        // low + range*t/total - 1 <= value  =>  t = (value-low+1)*total/range-1
        let scaled = ((self.value - self.low + 1) * total - 1) / range;
        debug_assert!(scaled < total);
        let (symbol, cum_low, freq) = self.model.find(scaled as u32);

        self.high = self.low + range * (u64::from(cum_low) + u64::from(freq)) / total - 1;
        self.low += range * u64::from(cum_low) / total;

        loop {
            if self.high < HALF {
                // E1: nothing to subtract.
            } else if self.low >= HALF {
                // E2.
                self.value -= HALF;
                self.low -= HALF;
                self.high -= HALF;
            } else if self.low >= QUARTER && self.high < HALF + QUARTER {
                // E3.
                self.value -= QUARTER;
                self.low -= QUARTER;
                self.high -= QUARTER;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
            self.value = ((self.value << 1) & (TOP - 1)) | u64::from(self.bits.read_bit()?);
        }

        self.model.update(symbol);
        self.symbols += 1;
        Ok(symbol)
    }

    /// Decode the whole stream into `out`, stopping at the EOF terminator.
    ///
    /// Truncated input (no decodable terminator) produces
    /// [`Error::UnexpectedEnd`]. Output beyond the configured cap produces
    /// [`Error::OutputLimitExceeded`].
    pub fn decode_to_writer<W: Write>(mut self, out: &mut W) -> Result<Stats> {
        self.prime()?;
        let mut produced: u64 = 0;
        let before = self.bits.bits_read();
        loop {
            let symbol = self.decode_symbol()?;
            // A well-formed stream carries, for every symbol, all the real
            // renormalization bits the decoder needs (plus a final flush and a
            // CODE_BITS-bit zero tail). So touching the infinite virtual zero
            // tail while a byte symbol is still coming in means the coded
            // bytes were cut short. Fail immediately rather than decoding
            // unbounded garbage up to the output-length cap.
            if self.bits.virtual_bits() > 0 {
                return Err(Error::UnexpectedEnd);
            }
            if symbol == EOF_SYMBOL {
                break;
            }
            if produced >= self.max_output_bytes {
                return Err(Error::OutputLimitExceeded {
                    limit: self.max_output_bytes,
                });
            }
            out.write_all(&[symbol as u8])?;
            produced += 1;
        }
        let coded_bits = self.bits.bits_read() - before;
        let stats = Stats {
            input_bytes: self.bits.bytes_read(),
            output_bytes: produced,
            coded_bits,
            rescales: self.model.rescale_count(),
            symbols: self.symbols,
        };
        Ok(stats)
    }

    /// Decode the whole stream into a freshly allocated vector, bounded by the
    /// configured output limit.
    pub fn decode_to_vec(self) -> Result<(Vec<u8>, Stats)> {
        let mut buf = Vec::new();
        let stats = self.decode_to_writer(&mut buf)?;
        Ok((buf, stats))
    }
}

/// Encode `input` fully (header handling lives in the container module) and
/// return the coded payload plus stats. Payload size may be capped.
pub fn encode_bytes(input: &[u8], max_output_bytes: Option<u64>) -> Result<(Vec<u8>, Stats)> {
    let mut enc = Encoder::new(Vec::new());
    if let Some(limit) = max_output_bytes {
        enc = enc.with_output_limit(limit);
    }
    enc.write_all_bytes(input)?;
    let (payload, stats) = enc.finish()?;
    Ok((payload, stats))
}

/// Decode a coded payload (container header already stripped) into bytes.
pub fn decode_bytes(input: &[u8], max_output_bytes: u64) -> Result<(Vec<u8>, Stats)> {
    let dec = Decoder::new(input).with_output_limit(max_output_bytes);
    dec.decode_to_vec()
}
