//! Streaming LZ77 encoder.
//!
//! Memory bound: the internal buffer holds at most `WINDOW_SIZE + MAX_MATCH`
//! bytes of history/lookahead after each `update` call, plus the chunk just
//! handed in. The whole input is never resident at once.

use std::collections::HashMap;

use crate::format;

/// Streaming LZ77 encoder over a 32 KiB window.
///
/// Usage: feed input with [`Encoder::update`], collect the returned bytes,
/// then call [`Encoder::finish`] exactly once and collect the tail.
pub struct Encoder {
    /// History (<= WINDOW_SIZE after compaction) plus pending lookahead.
    buf: Vec<u8>,
    /// Next position in `buf` to compress.
    pos: usize,
    /// 3-byte key -> most recent position in `buf` with that key.
    table: HashMap<u32, usize>,
    /// Pending literal bytes not yet emitted as a token.
    literals: Vec<u8>,
    /// Compressed bytes produced but not yet taken by the caller.
    out: Vec<u8>,
    header_written: bool,
    finished: bool,
}

impl Default for Encoder {
    fn default() -> Self {
        Self::new()
    }
}

impl Encoder {
    pub fn new() -> Self {
        Encoder {
            buf: Vec::new(),
            pos: 0,
            table: HashMap::new(),
            literals: Vec::new(),
            out: Vec::new(),
            header_written: false,
            finished: false,
        }
    }

    /// Feed a chunk of input. Returns newly produced compressed bytes.
    ///
    /// # Panics
    /// Panics if called after [`Encoder::finish`].
    pub fn update(&mut self, data: &[u8]) -> Vec<u8> {
        assert!(!self.finished, "update called after finish");
        self.ensure_header();
        self.buf.extend_from_slice(data);
        self.compress_available(false);
        self.take_out()
    }

    /// Flush the remaining input and finalize the stream.
    ///
    /// # Panics
    /// Panics if called twice.
    pub fn finish(&mut self) -> Vec<u8> {
        assert!(!self.finished, "finish called twice");
        self.ensure_header();
        self.compress_available(true);
        self.flush_literals();
        self.finished = true;
        self.take_out()
    }

    fn ensure_header(&mut self) {
        if !self.header_written {
            format::write_header(&mut self.out);
            self.header_written = true;
        }
    }

    fn take_out(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.out)
    }

    /// Compress as far as safely possible. In non-final mode we keep
    /// MAX_MATCH bytes of lookahead so match extension never runs off the
    /// end of the buffer.
    fn compress_available(&mut self, is_final: bool) {
        loop {
            let limit = if is_final {
                self.buf.len()
            } else {
                self.buf.len().saturating_sub(format::MAX_MATCH)
            };
            if self.pos >= limit {
                break;
            }
            self.step();
            self.maybe_compact();
        }
    }

    /// Compress a single position: emit a match or one literal.
    fn step(&mut self) {
        let pos = self.pos;
        let remaining = self.buf.len() - pos;

        if remaining >= format::MIN_MATCH {
            let key = key3(&self.buf[pos..pos + 3]);
            let candidate = self.table.insert(key, pos);
            if let Some(cand) = candidate {
                let distance = pos - cand;
                if distance <= format::WINDOW_SIZE {
                    let len = self.match_len(cand, pos);
                    if len >= format::MIN_MATCH {
                        self.emit_match(pos, len, distance);
                        return;
                    }
                }
            }
        }

        // No usable match: emit one literal.
        self.literals.push(self.buf[pos]);
        self.pos += 1;
        if self.literals.len() == format::MAX_LITERAL_RUN {
            self.flush_literals();
        }
    }

    /// Length of the match between `cand` and `pos`, capped at MAX_MATCH.
    /// Overlapping regions (cand + len >= pos) are allowed, giving RLE.
    fn match_len(&self, cand: usize, pos: usize) -> usize {
        let max = format::MAX_MATCH.min(self.buf.len() - pos);
        let mut len = 0;
        while len < max && self.buf[cand + len] == self.buf[pos + len] {
            len += 1;
        }
        len
    }

    fn emit_match(&mut self, pos: usize, len: usize, distance: usize) {
        self.flush_literals();
        format::write_match(&mut self.out, len, distance);
        // Index the interior of the match so later data can refer into it.
        for i in 1..len {
            let p = pos + i;
            if p + format::MIN_MATCH <= self.buf.len() {
                let key = key3(&self.buf[p..p + 3]);
                self.table.insert(key, p);
            }
        }
        self.pos = pos + len;
    }

    fn flush_literals(&mut self) {
        if self.literals.is_empty() {
            return;
        }
        let run = std::mem::take(&mut self.literals);
        format::write_literal_run(&mut self.out, &run);
    }

    /// Drop history older than the window once the consumed prefix is large,
    /// shifting table positions down to stay relative to `buf`.
    fn maybe_compact(&mut self) {
        if self.pos < 2 * format::WINDOW_SIZE {
            return;
        }
        let shift = self.pos - format::WINDOW_SIZE;
        self.buf.drain(..shift);
        self.pos -= shift;
        self.table.retain(|_, p| {
            if *p >= shift {
                *p -= shift;
                true
            } else {
                false
            }
        });
    }
}

/// 3-byte exact key (no hash collisions possible).
fn key3(b: &[u8]) -> u32 {
    ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32
}

/// One-shot convenience: compress a whole buffer.
pub fn compress(data: &[u8]) -> Vec<u8> {
    let mut enc = Encoder::new();
    let mut out = enc.update(data);
    out.extend_from_slice(&enc.finish());
    out
}
