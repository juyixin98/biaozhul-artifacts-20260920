//! Segment dictionary merging with cross-segment id remapping.
//!
//! Multiple `CDCT` streams (each possibly holding many segments with
//! independent local dictionaries) are fused into one output stream. A
//! single global dictionary is built in first-seen order; every input row id
//! is translated through a per-segment `local_id -> global_id` table, so the
//! merged output decodes to exactly the concatenation of the input rows.
//!
//! The merge is streaming: at any moment it holds only the global dictionary
//! (bounded by `max_dict_bytes`/`max_dict_entries`), one input segment's
//! dictionary, and one output segment's id buffer (bounded by `max_rows`).
//!
//! Dictionary *order* carries no meaning: ids always refer to the dictionary
//! shipped in the same segment, so any permutation of a segment's dictionary
//! (with matching id rewrite) decodes identically. The merger exploits this
//! — output segments may carry different snapshots of the growing global
//! dictionary.

use crate::error::{Error, Limits, Result};
use crate::segment;
use std::collections::HashMap;
use std::io::{Read, Write};

/// Statistics produced by a merge.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct MergeStats {
    pub input_streams: u64,
    pub input_segments: u64,
    pub output_segments: u64,
    pub rows: u64,
    pub global_dict_entries: u64,
}

/// Streaming merger. See module docs for the algorithm.
pub struct Merger {
    limits: Limits,
    global_dict: Vec<String>,
    global_index: HashMap<String, u32>,
    global_dict_bytes: u64,
    /// Ids of the output segment currently being accumulated (global ids).
    out_ids: Vec<u32>,
    out_segment_rows: u64,
    stats: MergeStats,
}

impl Merger {
    /// `out_segment_rows` controls how many rows go into each output segment
    /// (`0` = a single output segment, still bounded by `max_rows`).
    pub fn new(limits: Limits, out_segment_rows: u64) -> Self {
        Merger {
            limits,
            global_dict: Vec::new(),
            global_index: HashMap::new(),
            global_dict_bytes: 0,
            out_ids: Vec::new(),
            out_segment_rows,
            stats: MergeStats::default(),
        }
    }

    /// Intern `s` into the global dictionary, returning its global id.
    fn intern_global(&mut self, s: &str) -> Result<u32> {
        if let Some(&id) = self.global_index.get(s) {
            return Ok(id);
        }
        if self.global_dict.len() as u64 >= self.limits.max_dict_entries {
            return Err(Error::limit(format!(
                "merged dictionary exceeds {} entries",
                self.limits.max_dict_entries
            )));
        }
        let new_total = self.global_dict_bytes + s.len() as u64;
        if new_total > self.limits.max_dict_bytes {
            return Err(Error::limit(format!(
                "merged dictionary exceeds {} bytes",
                self.limits.max_dict_bytes
            )));
        }
        self.global_dict_bytes = new_total;
        let id = (self.global_dict.len() + 1) as u32;
        self.global_dict.push(s.to_string());
        self.global_index.insert(s.to_string(), id);
        Ok(id)
    }

    fn push_out_id<W: Write>(&mut self, w: &mut W, id: u32) -> Result<()> {
        if self.out_ids.len() as u64 >= self.limits.max_rows {
            return Err(Error::limit(format!(
                "output segment exceeds {} rows",
                self.limits.max_rows
            )));
        }
        if self.stats.rows >= self.limits.max_rows {
            return Err(Error::limit(format!(
                "merged stream exceeds {} rows in total",
                self.limits.max_rows
            )));
        }
        self.out_ids.push(id);
        self.stats.rows += 1;
        if self.out_segment_rows > 0 && self.out_ids.len() as u64 >= self.out_segment_rows {
            self.flush(w)?;
        }
        Ok(())
    }

    /// Write the pending output segment (with the current global dictionary
    /// snapshot) if any rows are buffered.
    pub fn flush<W: Write>(&mut self, w: &mut W) -> Result<()> {
        if self.out_ids.is_empty() {
            return Ok(());
        }
        segment::write_segment_header(
            w,
            &segment::SegmentHeader {
                dict_entries: self.global_dict.len() as u64,
                rows: self.out_ids.len() as u64,
            },
        )?;
        for entry in &self.global_dict {
            segment::write_dict_entry(w, entry)?;
        }
        for &id in &self.out_ids {
            segment::write_id(w, id)?;
        }
        self.out_ids.clear();
        self.stats.output_segments += 1;
        Ok(())
    }

    /// Merge one complete input stream into the output writer `w`.
    ///
    /// The file header of the output is *not* written here; call
    /// [`Merger::begin`] once before the first input and [`Merger::finish`]
    /// after the last one.
    pub fn add_stream<R: Read, W: Write>(&mut self, r: &mut R, w: &mut W) -> Result<()> {
        segment::read_file_header(r)?;
        self.stats.input_streams += 1;
        loop {
            let header = match segment::read_segment_header(r)? {
                None => break,
                Some(h) => h,
            };
            if self.stats.input_segments >= self.limits.max_segments {
                return Err(Error::limit(format!(
                    "more than {} input segments",
                    self.limits.max_segments
                )));
            }
            self.stats.input_segments += 1;
            let local_dict = segment::read_dictionary(r, header.dict_entries, &self.limits)?;
            // Build the local -> global id translation table for this segment.
            // Index 0 is NULL and maps to NULL; entry i (id i+1) maps to the
            // global id of local_dict[i].
            let mut remap = Vec::with_capacity(local_dict.len() + 1);
            remap.push(segment::NULL_ID);
            for entry in &local_dict {
                let gid = self.intern_global(entry)?;
                remap.push(gid);
            }
            for _ in 0..header.rows {
                let local_id = segment::read_id(r, header.dict_entries)?;
                let gid = remap[local_id as usize];
                self.push_out_id(w, gid)?;
            }
        }
        Ok(())
    }

    /// Write the output file header. Call once before the first `add_stream`.
    pub fn begin<W: Write>(&self, w: &mut W) -> Result<()> {
        segment::write_file_header(w)
    }

    /// Flush pending rows and return merge statistics.
    pub fn finish<W: Write>(mut self, w: &mut W) -> Result<MergeStats> {
        self.flush(w)?;
        self.stats.global_dict_entries = self.global_dict.len() as u64;
        Ok(self.stats)
    }
}

/// Convenience: merge several in-memory streams into one byte vector.
pub fn merge_to_vec(
    inputs: &[&[u8]],
    limits: &Limits,
    out_segment_rows: u64,
) -> Result<(Vec<u8>, MergeStats)> {
    let mut merger = Merger::new(limits.clone(), out_segment_rows);
    let mut out = Vec::new();
    merger.begin(&mut out)?;
    for input in inputs {
        let mut slice = *input;
        merger.add_stream(&mut slice, &mut out)?;
    }
    let stats = merger.finish(&mut out)?;
    Ok((out, stats))
}

/// Re-encode a single stream so that every segment's dictionary is replaced
/// by the global dictionary of the whole stream. Exposed mainly for tests of
/// order-independence; equivalent to merging one input.
pub fn normalize_to_vec(input: &[u8], limits: &Limits) -> Result<Vec<u8>> {
    let (out, _) = merge_to_vec(&[input], limits, 0)?;
    Ok(out)
}
