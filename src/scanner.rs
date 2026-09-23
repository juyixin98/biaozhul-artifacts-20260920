//! Bounded-memory input scanner.
//!
//! Records are lines separated by `\n`; a `\r` immediately before `\n` is a
//! terminator and stripped (CRLF).
//!
//! * Lines up to `small_limit` bytes are returned owned as
//!   [`ScannerEvent::Small`] and sorted in memory.
//! * A line that grows past `small_limit` becomes a [`ScannerEvent::Giant`]:
//!   scanning keeps buffering only until its key columns are known (capped by
//!   `head_limit`, the run writer's bounded head-chunk window), then the
//!   caller streams the rest through the [`GiantDrain`] impl straight into a
//!   run. A key that cannot be resolved within `head_limit` is a
//!   [`Error::KeyTooLarge`].
//!
//! Output convention (shared with the in-memory reference): every emitted
//! record is followed by `\n`, even the last — so no per-record newline flag is
//! persisted in runs.

use std::io;

use crate::error::{Error, Result};
use crate::key::{prefix_has_key, KeySpec};
use crate::sortfile::{GiantDrain, Pull};

/// Input stream the scanner reads from.
pub type Input = Box<dyn io::Read + Send>;

/// Internal read buffer (counted toward the memory budget).
pub const READ_BUFFER: usize = 16 * 1024;

/// A record that fits inside the memory window.
#[derive(Debug)]
pub struct SmallRecord {
    pub line: Vec<u8>,
    pub seq: u64,
}

/// Event yielded per input record.
#[derive(Debug)]
pub enum ScannerEvent {
    Small(SmallRecord),
    /// Drain this giant record via the [`GiantDrain`] impl before `next`.
    Giant {
        seq: u64,
        keys: Vec<Vec<u8>>,
        /// Bounded head value (the buffered line prefix).
        prefix: Vec<u8>,
    },
    End,
}

pub struct LineScanner {
    input: Input,
    readbuf: Vec<u8>,
    read_pos: usize,
    read_fill: usize,
    eof: bool,

    small_limit: usize,
    head_limit: usize,
    spec: KeySpec,

    cur: Vec<u8>,
    seq: u64,

    // Giant drain state.
    giant_active: bool,
    /// When true, the line ended during prefix resolution; the first pull
    /// returns no bytes and signals end-of-record.
    giant_immediate_end: bool,
    look: Option<u8>,
    pending_cr: bool,
}

impl LineScanner {
    /// `small_limit` is the resident-record cap (crossing it makes a line a
    /// giant); `head_limit` is the larger head-chunk window within which a
    /// giant's key must be resolvable.
    pub fn new(input: Input, spec: KeySpec, small_limit: usize, head_limit: usize) -> Self {
        LineScanner {
            input,
            readbuf: vec![0u8; READ_BUFFER],
            read_pos: 0,
            read_fill: 0,
            eof: false,
            small_limit,
            head_limit,
            spec,
            cur: Vec::new(),
            seq: 0,
            giant_active: false,
            giant_immediate_end: false,
            look: None,
            pending_cr: false,
        }
    }

    pub fn seq(&self) -> u64 {
        self.seq
    }
    pub fn spec(&self) -> &KeySpec {
        &self.spec
    }

    fn next_input_byte(&mut self) -> Result<Option<u8>> {
        if self.read_pos >= self.read_fill {
            if self.eof {
                return Ok(None);
            }
            let n = self.input.read(&mut self.readbuf)?;
            if n == 0 {
                self.eof = true;
                return Ok(None);
            }
            self.read_pos = 0;
            self.read_fill = n;
        }
        let b = self.readbuf[self.read_pos];
        self.read_pos += 1;
        Ok(Some(b))
    }

    /// Scan the next record. Named `next_record` semantically; kept as `next`
    /// for ergonomics but explicitly allowed since this type is not an
    /// iterator (it returns a [`Result`] and has a draining sub-protocol).
    #[allow(clippy::should_implement_trait)]
    pub fn next(&mut self) -> Result<ScannerEvent> {
        debug_assert!(!self.giant_active, "giant record not fully drained");
        self.cur.clear();
        self.look = None;
        self.pending_cr = false;
        self.giant_immediate_end = false;
        let seq = self.seq;
        let mut nl = false;

        // Phase 1: buffer up to small_limit bytes.
        while self.cur.len() < self.small_limit {
            match self.next_input_byte()? {
                Some(b'\n') => {
                    nl = true;
                    break;
                }
                Some(b) => self.cur.push(b),
                None => break,
            }
        }

        // A line terminated within the resident window is a small record.
        if nl || self.cur.len() < self.small_limit {
            if self.cur.is_empty() && !nl {
                return Ok(ScannerEvent::End);
            }
            if nl && self.cur.last() == Some(&b'\r') {
                self.cur.pop();
            }
            self.seq += 1;
            return Ok(ScannerEvent::Small(SmallRecord {
                line: std::mem::take(&mut self.cur),
                seq,
            }));
        }

        // Phase 2: the line crossed the resident cap. Keep buffering until its
        // key columns are known, capped by the writer's head window.
        if crate::key::key_needs_whole_line(&self.spec) {
            return Err(Error::KeyTooLarge {
                seq,
                needed: self.small_limit + 1,
                budget: self.head_limit as u64,
            });
        }
        loop {
            if prefix_has_key(&self.spec, &self.cur) {
                break;
            }
            match self.next_input_byte()? {
                Some(b'\n') => {
                    nl = true;
                    break;
                }
                Some(b) => {
                    if self.cur.len() >= self.head_limit {
                        return Err(Error::KeyTooLarge {
                            seq,
                            needed: self.cur.len() + 1,
                            budget: self.head_limit as u64,
                        });
                    }
                    self.cur.push(b);
                }
                None => break, // line ends without newline; key is known
            }
        }

        // A column that runs to the line end is complete once the newline (or
        // EOF) is reached; a CR immediately before that newline is part of the
        // CRLF terminator, not the value.
        if nl && self.cur.last() == Some(&b'\r') {
            self.cur.pop();
        }
        let keys = self
            .spec
            .extract(&self.cur)
            .into_iter()
            .map(|k| k.to_vec())
            .collect();
        let prefix = std::mem::take(&mut self.cur);
        self.giant_active = true;
        self.giant_immediate_end = nl;
        self.seq += 1;
        Ok(ScannerEvent::Giant { seq, keys, prefix })
    }
}

/// Streaming source for one giant record. When the line ended while its prefix
/// was being resolved, the first pull returns 0 bytes + end-of-record. Bytes
/// are read one at a time so CRLF CRs can be withheld and dropped if a newline
/// follows.
impl GiantDrain for LineScanner {
    fn pull(&mut self, buf: &mut [u8]) -> Result<Pull> {
        debug_assert!(self.giant_active);
        if self.giant_immediate_end {
            self.giant_immediate_end = false;
            self.giant_active = false;
            return Ok(Pull {
                bytes: 0,
                end_of_record: true,
            });
        }
        if buf.is_empty() {
            return Ok(Pull {
                bytes: 0,
                end_of_record: false,
            });
        }
        let mut n = 0usize;
        let mut ended = false;
        let mut pushed: Option<u8> = self.look.take();

        while n < buf.len() {
            let raw = match pushed.take() {
                Some(b) => Some(b),
                None => self.next_input_byte()?,
            };
            let Some(b) = raw else {
                // EOF ends the record; flush a withheld CR (bare CR at EOL).
                if self.pending_cr {
                    buf[n] = b'\r';
                    n += 1;
                    self.pending_cr = false;
                }
                ended = true;
                break;
            };
            if b == b'\n' {
                // Withheld CR belongs to a CRLF terminator: drop it.
                self.pending_cr = false;
                ended = true;
                break;
            }
            if self.pending_cr {
                buf[n] = b'\r';
                n += 1;
                self.pending_cr = false;
                // The current byte still needs processing; re-arm it.
                pushed = Some(b);
                continue;
            }
            if b == b'\r' {
                self.pending_cr = true;
                continue;
            }
            buf[n] = b;
            n += 1;
        }
        if ended {
            self.giant_active = false;
        }
        Ok(Pull {
            bytes: n,
            end_of_record: ended,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scan_small(data: &[u8], limit: usize) -> Vec<(Vec<u8>, u64)> {
        let owned = data.to_vec();
        let mut s = LineScanner::new(
            Box::new(std::io::Cursor::new(owned)),
            KeySpec::default(),
            limit,
            8 * 1024,
        );
        let mut out = Vec::new();
        loop {
            match s.next().unwrap() {
                ScannerEvent::End => break,
                ScannerEvent::Small(r) => out.push((r.line, r.seq)),
                ScannerEvent::Giant { .. } => panic!("unexpected giant"),
            }
        }
        out
    }

    fn drain_giant(s: &mut LineScanner) -> Vec<u8> {
        let mut out = Vec::new();
        let mut buf = [0u8; 7]; // awkward size stresses boundaries
        loop {
            let p = s.pull(&mut buf).unwrap();
            out.extend_from_slice(&buf[..p.bytes]);
            if p.end_of_record {
                break;
            }
        }
        out
    }

    #[test]
    fn simple_lines() {
        let rows = scan_small(b"alpha\nbeta\r\ngamma\ntail", 1024);
        assert_eq!(rows.len(), 4);
        assert_eq!(rows[0], (b"alpha".to_vec(), 0));
        assert_eq!(rows[1], (b"beta".to_vec(), 1));
        assert_eq!(rows[2], (b"gamma".to_vec(), 2));
        assert_eq!(rows[3], (b"tail".to_vec(), 3));
    }

    #[test]
    fn empty_and_blank() {
        assert!(scan_small(b"", 1024).is_empty());
        assert_eq!(scan_small(b"\n", 1024).len(), 1);
        assert_eq!(scan_small(b"a\n\n", 1024).len(), 2);
    }

    #[test]
    fn whole_line_giant_errors() {
        let owned = vec![0u8; 100];
        let mut s = LineScanner::new(
            Box::new(std::io::Cursor::new(owned)),
            KeySpec::default(),
            16,
            8 * 1024,
        );
        assert!(matches!(s.next().unwrap_err(), Error::KeyTooLarge { .. }));
    }

    #[test]
    fn giant_streaming_with_columns() {
        // key = column 2; line = "aa,BBB...,cc" where col2 is long.
        let mut body = b"aa,".to_vec();
        body.extend(std::iter::repeat(b'B').take(50));
        body.push(b',');
        body.extend_from_slice(b"cc\nnext,row\n");
        let spec = KeySpec::parse("2", b',').unwrap();
        let owned = body.clone();
        let mut s = LineScanner::new(Box::new(std::io::Cursor::new(owned)), spec, 16, 8 * 1024);
        match s.next().unwrap() {
            ScannerEvent::Giant { seq, keys, prefix } => {
                assert_eq!(seq, 0);
                assert_eq!(keys.len(), 1);
                // Key (column 2) fully captured from the bounded prefix.
                assert!(keys[0].iter().all(|&b| b == b'B'));
                assert_eq!(keys[0].len(), 50);
                let rest = drain_giant(&mut s);
                let all = [prefix, rest].concat();
                // First record is everything before its '\n'.
                let expected = &body[..body.len() - b"next,row\n".len() - 1];
                assert_eq!(&all, expected);
            }
            _ => panic!("expected giant"),
        }
        // Scanning resumes at the next line.
        match s.next().unwrap() {
            ScannerEvent::Small(r) => assert_eq!(r.line, b"next,row"),
            _ => panic!("expected small"),
        }
    }

    #[test]
    fn giant_crlf_and_eof() {
        // Giant line terminated by CRLF then a short line.
        let mut body = b"x,".to_vec();
        body.extend(std::iter::repeat(b'Z').take(40));
        body.extend_from_slice(b"\r\nn\n");
        let spec = KeySpec::parse("2", b',').unwrap();
        let mut s = LineScanner::new(
            Box::new(std::io::Cursor::new(body.clone())),
            spec,
            8,
            8 * 1024,
        );
        match s.next().unwrap() {
            ScannerEvent::Giant { prefix, .. } => {
                let rest = drain_giant(&mut s);
                let all = [prefix, rest].concat();
                assert!(!all.contains(&b'\r'), "CR should be stripped: {all:?}");
                assert_eq!(all.len(), 2 + 40);
            }
            _ => panic!("expected giant"),
        }
        match s.next().unwrap() {
            ScannerEvent::Small(r) => assert_eq!(r.line, b"n"),
            _ => panic!("expected small"),
        }
    }

    #[test]
    fn giant_whole_line_fits_head_but_over_small_limit() {
        // A line over small_limit but the whole key (col1) fits the head.
        let body = b"abcdefghijklmnop,rest\nx\n".to_vec();
        let spec = KeySpec::parse("1", b',').unwrap();
        let mut s = LineScanner::new(
            Box::new(std::io::Cursor::new(body.clone())),
            spec,
            8,
            8 * 1024,
        );
        match s.next().unwrap() {
            ScannerEvent::Giant { keys, prefix, .. } => {
                assert_eq!(keys[0], b"abcdefghijklmnop");
                let rest = drain_giant(&mut s);
                let all = [prefix, rest].concat();
                assert_eq!(&all, b"abcdefghijklmnop,rest");
            }
            _ => panic!("expected giant"),
        }
        match s.next().unwrap() {
            ScannerEvent::Small(r) => assert_eq!(r.line, b"x"),
            _ => panic!("expected small"),
        }
    }
}
