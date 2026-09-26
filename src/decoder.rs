//! Streaming LZ77 decoder with a hard output budget.
//!
//! Safety properties:
//! - match distances may not exceed the window size;
//! - match distances may not reach before the first produced byte;
//! - overlapping copies (distance < length) are supported by copying one
//!   byte at a time from the history;
//! - decoding stops with [`Error::OutputBudgetExceeded`] before the
//!   configured `max_output` is crossed, so hostile streams cannot expand
//!   without bound ("decompression bomb" protection);
//! - memory is bounded: only the last `WINDOW_SIZE` produced bytes plus the
//!   not-yet-parsed input tail are retained.

use crate::error::Error;
use crate::format;

/// Streaming decoder. Feed compressed bytes with [`Decoder::update`],
/// then finalize with [`Decoder::finish`].
pub struct Decoder {
    /// Compressed bytes not yet parsed (a partial token may sit here).
    inbuf: Vec<u8>,
    /// Last up-to-WINDOW_SIZE produced bytes, for back references.
    hist: Vec<u8>,
    /// Total bytes produced so far.
    produced: u64,
    /// Hard cap on total decoded bytes.
    max_output: u64,
    header_done: bool,
    finished: bool,
}

impl Decoder {
    /// Create a decoder that refuses to produce more than `max_output` bytes.
    pub fn new(max_output: u64) -> Self {
        Decoder {
            inbuf: Vec::new(),
            hist: Vec::new(),
            produced: 0,
            max_output,
            header_done: false,
            finished: false,
        }
    }

    /// Total decoded bytes so far.
    pub fn produced(&self) -> u64 {
        self.produced
    }

    /// Feed a chunk of compressed input. Returns newly decoded bytes.
    pub fn update(&mut self, data: &[u8]) -> Result<Vec<u8>, Error> {
        if self.finished {
            return Err(Error::AlreadyFinished);
        }
        self.inbuf.extend_from_slice(data);
        let mut out = Vec::new();
        self.parse(&mut out)?;
        Ok(out)
    }

    /// Signal end of input. Fails if a token (or the header) is incomplete.
    pub fn finish(&mut self) -> Result<(), Error> {
        if self.finished {
            return Err(Error::AlreadyFinished);
        }
        self.finished = true;
        if !self.inbuf.is_empty() {
            // Whatever is left is by definition an incomplete header/token:
            // complete tokens are consumed eagerly by `parse`.
            return Err(Error::TruncatedToken);
        }
        if !self.header_done {
            return Err(Error::TruncatedToken);
        }
        Ok(())
    }

    /// Parse complete tokens from `inbuf`, appending decoded bytes to `out`.
    /// Leaves any incomplete trailing token in `inbuf` for the next call.
    fn parse(&mut self, out: &mut Vec<u8>) -> Result<(), Error> {
        if !self.header_done {
            if self.inbuf.len() < format::HEADER_LEN {
                return Ok(()); // wait for more input
            }
            self.parse_header()?;
            self.inbuf.drain(..format::HEADER_LEN);
            self.header_done = true;
        }

        let mut i = 0;
        while i < self.inbuf.len() {
            let tag = self.inbuf[i];
            if tag & format::MATCH_TAG_BIT == 0 {
                let run = tag as usize + 1;
                if self.inbuf.len() - i < 1 + run {
                    break; // incomplete literal run
                }
                self.reserve_budget(run)?;
                out.extend_from_slice(&self.inbuf[i + 1..i + 1 + run]);
                self.hist.extend_from_slice(&self.inbuf[i + 1..i + 1 + run]);
                self.trim_hist();
                self.produced += run as u64;
                i += 1 + run;
            } else {
                if self.inbuf.len() - i < 3 {
                    break; // incomplete match token
                }
                let len = (tag & !format::MATCH_TAG_BIT) as usize + format::MIN_MATCH;
                let distance = u16::from_be_bytes([self.inbuf[i + 1], self.inbuf[i + 2]]) as usize;
                self.check_distance(distance)?;
                self.reserve_budget(len)?;
                self.copy_match(out, len, distance);
                self.produced += len as u64;
                i += 3;
            }
        }
        self.inbuf.drain(..i);
        Ok(())
    }

    fn parse_header(&self) -> Result<(), Error> {
        let h = &self.inbuf[..format::HEADER_LEN];
        if &h[..4] != format::MAGIC {
            return Err(Error::InvalidMagic);
        }
        if h[4] != format::VERSION {
            return Err(Error::UnsupportedVersion(h[4]));
        }
        if h[5] != 0 || h[7] != 0 {
            return Err(Error::InvalidHeader);
        }
        if h[6] != format::WINDOW_LOG2 {
            return Err(Error::UnsupportedWindow(h[6]));
        }
        Ok(())
    }

    fn check_distance(&self, distance: usize) -> Result<(), Error> {
        if distance > format::WINDOW_SIZE {
            return Err(Error::DistanceExceedsWindow {
                distance: distance as u32,
            });
        }
        if distance as u64 > self.produced {
            return Err(Error::DistanceTooLarge {
                distance: distance as u32,
                produced: self.produced,
            });
        }
        Ok(())
    }

    fn reserve_budget(&self, n: usize) -> Result<(), Error> {
        let attempted = self.produced + n as u64;
        if attempted > self.max_output {
            return Err(Error::OutputBudgetExceeded {
                limit: self.max_output,
                attempted,
            });
        }
        Ok(())
    }

    /// Copy `len` bytes from `distance` back in the output. Byte-by-byte so
    /// overlapping copies (distance < len) repeat the pattern, LZ77-style.
    fn copy_match(&mut self, out: &mut Vec<u8>, len: usize, distance: usize) {
        for _ in 0..len {
            let b = self.hist[self.hist.len() - distance];
            out.push(b);
            self.hist.push(b);
        }
        self.trim_hist();
    }

    /// Keep only the last WINDOW_SIZE bytes of history; amortized.
    fn trim_hist(&mut self) {
        if self.hist.len() >= 2 * format::WINDOW_SIZE {
            self.hist.drain(..format::WINDOW_SIZE);
        }
    }
}

/// One-shot convenience: decompress a whole buffer with an output budget.
pub fn decompress(data: &[u8], max_output: u64) -> Result<Vec<u8>, Error> {
    let mut dec = Decoder::new(max_output);
    let out = dec.update(data)?;
    dec.finish()?;
    Ok(out)
}
