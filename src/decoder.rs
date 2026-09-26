//! Streaming dictionary decoder.
//!
//! Segments are read one at a time; only one segment's dictionary is held at
//! a time. Two consumer styles are supported:
//!
//! * [`decode_to_vec`] / [`Decoder::next_row`] for materialized or row-by-row
//!   access;
//! * [`decode_for_each`] for callbacks that never materialize the whole
//!   column, keeping peak memory at one dictionary.
//!
//! All limits are enforced while reading: an oversized declared dictionary
//! is rejected before its entries are allocated, and the running total of
//! emitted string bytes is capped by `max_decoded_string_bytes`.

use crate::error::{Error, Limits, Result};
use crate::segment;
use std::io::Read;

/// Statistics produced after decoding a complete stream.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct DecodeStats {
    pub rows: u64,
    pub segments: u64,
}

/// Row-by-row streaming decoder over any [`Read`].
pub struct Decoder<R: Read> {
    r: R,
    limits: Limits,
    dict: Vec<String>,
    /// Remaining ids to yield in the current segment.
    remaining: u64,
    segments: u64,
    rows: u64,
    /// Set once clean EOF after a complete segment has been observed.
    done: bool,
}

impl<R: Read> Decoder<R> {
    pub fn new(r: R, limits: Limits) -> Result<Self> {
        let mut d = Decoder {
            r,
            limits,
            dict: Vec::new(),
            remaining: 0,
            segments: 0,
            rows: 0,
            done: false,
        };
        segment::read_file_header(&mut d.r)?;
        Ok(d)
    }

    fn load_next_segment(&mut self) -> Result<bool> {
        let header = match segment::read_segment_header(&mut self.r)? {
            None => {
                self.done = true;
                return Ok(false);
            }
            Some(h) => h,
        };
        if self.segments >= self.limits.max_segments {
            return Err(Error::limit(format!(
                "more than {} segments",
                self.limits.max_segments
            )));
        }
        if self.rows.saturating_add(header.rows) > self.limits.max_rows {
            return Err(Error::limit(format!(
                "total row count would exceed {}",
                self.limits.max_rows
            )));
        }
        self.dict = segment::read_dictionary(&mut self.r, header.dict_entries, &self.limits)?;
        self.remaining = header.rows;
        self.segments += 1;
        Ok(true)
    }

    /// Read the next row id, advancing across segment boundaries.
    /// Returns `Ok(None)` at clean end of stream.
    fn read_one_id(&mut self) -> Result<Option<u32>> {
        while self.remaining == 0 {
            if !self.load_next_segment()? {
                return Ok(None);
            }
        }
        let id = segment::read_id(&mut self.r, self.dict.len() as u64)?;
        self.remaining -= 1;
        self.rows += 1;
        Ok(Some(id))
    }

    /// Return the next row, or `None` at clean end of stream.
    ///
    /// Strings are returned by reference into the current segment
    /// dictionary; clone the value if it must outlive subsequent calls.
    pub fn next_row(&mut self) -> Result<Option<Option<&str>>> {
        if self.done {
            return Ok(None);
        }
        match self.read_one_id()? {
            None => Ok(None),
            Some(id) if id == segment::NULL_ID => Ok(Some(None)),
            Some(id) => Ok(Some(Some(self.dict[(id - 1) as usize].as_str()))),
        }
    }

    /// Finish reading and return statistics. Must be called after `next_row`
    /// returns `None` (verifies there is no trailing/partial data).
    pub fn finish(self) -> DecodeStats {
        DecodeStats {
            rows: self.rows,
            segments: self.segments,
        }
    }
}

/// Decode an entire stream, invoking `f(None)` for NULL rows and
/// `f(Some(s))` for string rows. Output length is bounded by
/// `limits.max_decoded_string_bytes` counted across all emitted strings.
pub fn decode_for_each<R, F>(r: R, limits: &Limits, mut f: F) -> Result<DecodeStats>
where
    R: Read,
    F: FnMut(Option<&str>) -> Result<()>,
{
    let mut dec = Decoder::new(r, limits.clone())?;
    let mut emitted_bytes: u64 = 0;
    loop {
        match dec.next_row()? {
            None => break,
            Some(None) => f(None)?,
            Some(Some(s)) => {
                emitted_bytes += s.len() as u64;
                if emitted_bytes > limits.max_decoded_string_bytes {
                    return Err(Error::limit(format!(
                        "decoded output exceeds {} string bytes",
                        limits.max_decoded_string_bytes
                    )));
                }
                f(Some(s))?
            }
        }
    }
    Ok(dec.finish())
}

/// Decode an entire stream into a vector of rows.
pub fn decode_to_vec<R: Read>(r: R, limits: &Limits) -> Result<(Vec<Option<String>>, DecodeStats)> {
    let mut out: Vec<Option<String>> = Vec::new();
    let stats = decode_for_each(r, limits, |row| {
        out.push(row.map(|s| s.to_string()));
        Ok(())
    })?;
    Ok((out, stats))
}
