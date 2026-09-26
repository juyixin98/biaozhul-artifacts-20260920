//! Streaming dictionary encoder.
//!
//! Rows are pushed one at a time; each distinct non-NULL string is interned
//! into the current segment's dictionary (first-seen order) and the row is
//! recorded as a 32-bit id (`0` = NULL, `n` = dictionary entry `n - 1`).
//! When a segment fills up (`segment_rows` reached) it is flushed to the
//! underlying writer and a fresh segment begins, so peak memory stays bounded
//! by one segment's dictionary plus its id vector — both capped by
//! [`Limits`].

use crate::error::{Error, Limits, Result};
use crate::segment;
use std::collections::HashMap;
use std::io::Write;

/// Accumulates rows for one segment and serializes it on demand.
#[derive(Default)]
pub struct SegmentEncoder {
    dict: Vec<String>,
    index: HashMap<String, u32>,
    ids: Vec<u32>,
    dict_bytes: u64,
}

impl SegmentEncoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Add one row. `None` encodes NULL.
    pub fn push(&mut self, value: Option<&str>, limits: &Limits) -> Result<()> {
        if self.ids.len() as u64 >= limits.max_rows {
            return Err(Error::limit(format!(
                "segment row count exceeds {}",
                limits.max_rows
            )));
        }
        match value {
            None => self.ids.push(segment::NULL_ID),
            Some(s) => {
                let id = match self.index.get(s) {
                    Some(&id) => id,
                    None => {
                        if s.len() as u64 > limits.max_string_bytes {
                            return Err(Error::limit(format!(
                                "string of {} bytes exceeds max_string_bytes {}",
                                s.len(),
                                limits.max_string_bytes
                            )));
                        }
                        if self.dict.len() as u64 >= limits.max_dict_entries {
                            return Err(Error::limit(format!(
                                "dictionary exceeds {} entries",
                                limits.max_dict_entries
                            )));
                        }
                        let new_total = self.dict_bytes + s.len() as u64;
                        if new_total > limits.max_dict_bytes {
                            return Err(Error::limit(format!(
                                "dictionary exceeds {} bytes",
                                limits.max_dict_bytes
                            )));
                        }
                        self.dict_bytes = new_total;
                        let id = (self.dict.len() + 1) as u32;
                        self.index.insert(s.to_string(), id);
                        self.dict.push(s.to_string());
                        id
                    }
                };
                self.ids.push(id);
            }
        }
        Ok(())
    }

    pub fn rows(&self) -> u64 {
        self.ids.len() as u64
    }

    pub fn dict_entries(&self) -> u64 {
        self.dict.len() as u64
    }

    pub fn is_empty(&self) -> bool {
        self.ids.is_empty()
    }

    /// Serialize the segment (header + dictionary + ids) to `w`.
    pub fn write_to<W: Write + ?Sized>(&self, w: &mut W) -> Result<()> {
        segment::write_segment_header(
            w,
            &segment::SegmentHeader {
                dict_entries: self.dict_entries(),
                rows: self.rows(),
            },
        )?;
        for entry in &self.dict {
            segment::write_dict_entry(w, entry)?;
        }
        for &id in &self.ids {
            segment::write_id(w, id)?;
        }
        Ok(())
    }

    /// Reset for the next segment, releasing per-segment state.
    pub fn clear(&mut self) {
        self.dict.clear();
        self.index.clear();
        self.ids.clear();
        self.dict_bytes = 0;
    }
}

/// Summary returned by [`Encoder::finish`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EncodeStats {
    pub rows: u64,
    pub segments: u64,
}

/// Streaming encoder writing a complete `CDCT` file to `W`.
pub struct Encoder<W: Write> {
    w: W,
    limits: Limits,
    segment_rows: u64,
    seg: SegmentEncoder,
    header_written: bool,
    rows: u64,
    segments: u64,
}

impl<W: Write> Encoder<W> {
    /// Create an encoder emitting a new segment every `segment_rows` rows.
    /// `segment_rows == 0` means "one segment for the whole stream".
    pub fn new(w: W, limits: Limits, segment_rows: u64) -> Self {
        Self {
            w,
            limits,
            segment_rows,
            seg: SegmentEncoder::new(),
            header_written: false,
            rows: 0,
            segments: 0,
        }
    }

    fn ensure_header(&mut self) -> Result<()> {
        if !self.header_written {
            segment::write_file_header(&mut self.w)?;
            self.header_written = true;
        }
        Ok(())
    }

    /// Push one row (`None` = NULL).
    pub fn push_row(&mut self, value: Option<&str>) -> Result<()> {
        self.ensure_header()?;
        if self.rows >= self.limits.max_rows {
            return Err(Error::limit(format!(
                "stream exceeds {} rows in total",
                self.limits.max_rows
            )));
        }
        if self.segment_rows > 0 && self.seg.rows() >= self.segment_rows {
            self.flush_segment()?;
        }
        self.seg.push(value, &self.limits)?;
        self.rows += 1;
        Ok(())
    }

    /// Flush the current partial segment, if any.
    pub fn flush_segment(&mut self) -> Result<()> {
        self.ensure_header()?;
        if !self.seg.is_empty() {
            if self.segments >= self.limits.max_segments {
                return Err(Error::limit(format!(
                    "more than {} segments",
                    self.limits.max_segments
                )));
            }
            self.seg.write_to(&mut self.w)?;
            self.segments += 1;
            self.seg.clear();
        }
        Ok(())
    }

    /// Flush any pending rows and return statistics.
    pub fn finish(mut self) -> Result<EncodeStats> {
        self.flush_segment()?;
        Ok(EncodeStats {
            rows: self.rows,
            segments: self.segments,
        })
    }
}

/// Convenience: encode a full in-memory column into a byte vector.
pub fn encode_to_vec(rows: &[Option<&str>], limits: &Limits, segment_rows: u64) -> Result<Vec<u8>> {
    let mut enc = Encoder::new(Vec::new(), limits.clone(), segment_rows);
    enc.ensure_header()?;
    for row in rows {
        enc.push_row(*row)?;
    }
    enc.flush_segment()?;
    Ok(enc.into_inner())
}

impl<W: Write> Encoder<W> {
    /// Consume the encoder, returning the underlying writer.
    pub fn into_inner(self) -> W {
        self.w
    }
}
