//! Stream-level container: fixed header, block section, random-access index.
//!
//! Layout (all integers little-endian, see FORMAT.md for the full spec):
//!
//! ```text
//! offset  size  field
//! 0       4     magic         "BPB1"
//! 4       2     version       u16 = 1
//! 6       2     flags         u16, bit 0 clear = little-endian (only 0 valid)
//! 8       4     block_size    u32, max values per block
//! 12      4     block_count   u32
//! 16      8     total_values  u64
//! 24      8     index_offset  u64, absolute offset of the index section
//! 32      ..    block section (block_count blocks, see block.rs)
//! ..      ..    index section: block_count entries of
//!               (block_offset u64, first_value_index u64)
//! ```
//!
//! The index lets a reader locate and decode a single block without
//! touching the others.

use crate::block::{decode_block, encode_block};
use crate::error::{Error, Result};
use crate::limits::{Limits, ABSOLUTE_MAX_BLOCK_SIZE};

pub const MAGIC: &[u8; 4] = b"BPB1";
pub const VERSION: u16 = 1;
pub const STREAM_HEADER_LEN: usize = 32;
pub const INDEX_ENTRY_LEN: usize = 16;

/// Parsed stream header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StreamHeader {
    pub block_size: u32,
    pub block_count: u32,
    pub total_values: u64,
    pub index_offset: u64,
}

/// One index entry: where a block starts and which global value index it
/// begins at.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IndexEntry {
    pub block_offset: u64,
    pub first_value_index: u64,
}

/// Encode `values` into a complete stream with blocks of at most
/// `block_size` values.
pub fn encode_stream(values: &[i64], block_size: u32) -> Result<Vec<u8>> {
    if block_size == 0 || block_size > ABSOLUTE_MAX_BLOCK_SIZE {
        return Err(Error::InvalidBlockSize(block_size));
    }
    let block_count = values.len().div_ceil(block_size as usize);
    if block_count > u32::MAX as usize {
        return Err(Error::LimitExceeded("block count"));
    }

    let mut out = vec![0u8; STREAM_HEADER_LEN];
    let mut index: Vec<IndexEntry> = Vec::with_capacity(block_count);
    let mut first_value_index = 0u64;
    for chunk in values.chunks(block_size as usize) {
        index.push(IndexEntry {
            block_offset: out.len() as u64,
            first_value_index,
        });
        out.extend_from_slice(&encode_block(chunk)?);
        first_value_index += chunk.len() as u64;
    }
    let index_offset = out.len() as u64;
    for entry in &index {
        out.extend_from_slice(&entry.block_offset.to_le_bytes());
        out.extend_from_slice(&entry.first_value_index.to_le_bytes());
    }

    let header = StreamHeader {
        block_size,
        block_count: index.len() as u32,
        total_values: values.len() as u64,
        index_offset,
    };
    out[..STREAM_HEADER_LEN].copy_from_slice(&serialize_header(&header));
    Ok(out)
}

fn serialize_header(h: &StreamHeader) -> [u8; STREAM_HEADER_LEN] {
    let mut buf = [0u8; STREAM_HEADER_LEN];
    buf[0..4].copy_from_slice(MAGIC);
    buf[4..6].copy_from_slice(&VERSION.to_le_bytes());
    buf[6..8].copy_from_slice(&0u16.to_le_bytes()); // flags: little-endian
    buf[8..12].copy_from_slice(&h.block_size.to_le_bytes());
    buf[12..16].copy_from_slice(&h.block_count.to_le_bytes());
    buf[16..24].copy_from_slice(&h.total_values.to_le_bytes());
    buf[24..32].copy_from_slice(&h.index_offset.to_le_bytes());
    buf
}

/// Parse and fully validate the stream header and index. Every structural
/// invariant is checked here, before any block is touched or any output
/// buffer is allocated.
pub fn parse_stream(data: &[u8], limits: &Limits) -> Result<(StreamHeader, Vec<IndexEntry>)> {
    if data.len() > limits.max_input_bytes {
        return Err(Error::LimitExceeded("input bytes"));
    }
    if data.len() < STREAM_HEADER_LEN {
        return Err(Error::UnexpectedEof);
    }
    if &data[0..4] != MAGIC {
        return Err(Error::InvalidMagic);
    }
    let version = u16::from_le_bytes(data[4..6].try_into().expect("len checked"));
    if version != VERSION {
        return Err(Error::UnsupportedVersion(version));
    }
    let flags = u16::from_le_bytes(data[6..8].try_into().expect("len checked"));
    if flags != 0 {
        return Err(Error::UnsupportedFlags(flags));
    }
    let header = StreamHeader {
        block_size: u32::from_le_bytes(data[8..12].try_into().expect("len checked")),
        block_count: u32::from_le_bytes(data[12..16].try_into().expect("len checked")),
        total_values: u64::from_le_bytes(data[16..24].try_into().expect("len checked")),
        index_offset: u64::from_le_bytes(data[24..32].try_into().expect("len checked")),
    };

    if header.block_size == 0 || header.block_size > limits.max_block_size {
        return Err(Error::InvalidBlockSize(header.block_size));
    }
    if header.total_values > limits.max_total_values {
        return Err(Error::LimitExceeded("total values"));
    }
    // block_count must be exactly ceil(total_values / block_size).
    let expected_blocks = if header.total_values == 0 {
        0
    } else {
        (header.total_values - 1) / u64::from(header.block_size) + 1
    };
    if u64::from(header.block_count) != expected_blocks {
        return Err(Error::Corrupt("block count inconsistent with totals"));
    }

    let index_offset = usize::try_from(header.index_offset)
        .map_err(|_| Error::Corrupt("index offset overflows address space"))?;
    if index_offset < STREAM_HEADER_LEN || index_offset > data.len() {
        return Err(Error::Corrupt("index offset out of range"));
    }
    let index_bytes = (header.block_count as usize)
        .checked_mul(INDEX_ENTRY_LEN)
        .ok_or(Error::Corrupt("index size overflow"))?;
    let index_end = index_offset
        .checked_add(index_bytes)
        .ok_or(Error::Corrupt("index size overflow"))?;
    if index_end > data.len() {
        return Err(Error::UnexpectedEof);
    }
    if index_end != data.len() {
        return Err(Error::TrailingBytes);
    }

    let mut index = Vec::with_capacity(header.block_count as usize);
    let mut prev_offset: Option<u64> = None;
    let mut prev_first: Option<u64> = None;
    for chunk in data[index_offset..index_end].chunks_exact(INDEX_ENTRY_LEN) {
        let entry = IndexEntry {
            block_offset: u64::from_le_bytes(chunk[0..8].try_into().expect("chunk len")),
            first_value_index: u64::from_le_bytes(chunk[8..16].try_into().expect("chunk len")),
        };
        if entry.block_offset < STREAM_HEADER_LEN as u64
            || entry.block_offset >= header.index_offset
        {
            return Err(Error::Corrupt("block offset out of range"));
        }
        if prev_offset.is_some_and(|p| entry.block_offset <= p) {
            return Err(Error::Corrupt("block offsets not increasing"));
        }
        if entry.first_value_index >= header.total_values && header.total_values > 0 {
            return Err(Error::Corrupt("first value index out of range"));
        }
        if prev_first.is_some_and(|p| entry.first_value_index <= p) {
            return Err(Error::Corrupt("first value indices not increasing"));
        }
        prev_offset = Some(entry.block_offset);
        prev_first = Some(entry.first_value_index);
        index.push(entry);
    }
    if !index.is_empty() && index[0].first_value_index != 0 {
        return Err(Error::Corrupt("first block must start at value 0"));
    }
    Ok((header, index))
}

/// Byte slice of block `i`, bounded by the next block's offset or the start
/// of the index section.
fn block_slice<'a>(data: &'a [u8], header: &StreamHeader, index: &[IndexEntry], i: usize) -> &'a [u8] {
    let start = index[i].block_offset as usize;
    let end = if i + 1 < index.len() {
        index[i + 1].block_offset as usize
    } else {
        header.index_offset as usize
    };
    &data[start..end]
}

/// Decode the entire stream. Output length is capped by
/// `limits.max_output_values`.
pub fn decode_stream(data: &[u8], limits: &Limits) -> Result<Vec<i64>> {
    let (header, index) = parse_stream(data, limits)?;
    if header.total_values > limits.max_output_values {
        return Err(Error::LimitExceeded("output values"));
    }
    let mut values = Vec::with_capacity(header.total_values as usize);
    let per_block_cap = limits.max_block_size.min(header.block_size);
    for (i, entry) in index.iter().enumerate() {
        if entry.first_value_index != values.len() as u64 {
            return Err(Error::Corrupt("index does not match decoded length"));
        }
        let (_, block_values) = decode_block(block_slice(data, &header, &index, i), per_block_cap)?;
        values.extend_from_slice(&block_values);
    }
    if values.len() as u64 != header.total_values {
        return Err(Error::Corrupt("decoded length does not match header"));
    }
    Ok(values)
}

/// Decode a single block by number, using the index to seek directly.
pub fn decode_block_at(data: &[u8], block_number: u32, limits: &Limits) -> Result<Vec<i64>> {
    let (header, index) = parse_stream(data, limits)?;
    let i = block_number as usize;
    if i >= index.len() {
        return Err(Error::Corrupt("block number out of range"));
    }
    let (_, values) = decode_block(
        block_slice(data, &header, &index, i),
        limits.max_block_size.min(header.block_size),
    )?;
    Ok(values)
}

/// Structural information about a stream, returned by `inspect`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StreamInfo {
    pub header: StreamHeader,
    pub index: Vec<IndexEntry>,
}

/// Parse header and index without decoding any values.
pub fn inspect(data: &[u8], limits: &Limits) -> Result<StreamInfo> {
    let (header, index) = parse_stream(data, limits)?;
    Ok(StreamInfo { header, index })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_stream_roundtrips() {
        let data = encode_stream(&[], 128).unwrap();
        assert_eq!(data.len(), STREAM_HEADER_LEN);
        let values = decode_stream(&data, &Limits::unrestricted()).unwrap();
        assert!(values.is_empty());
    }

    #[test]
    fn multi_block_index_seeks() {
        let values: Vec<i64> = (0..1000).map(|i| i * i - 500).collect();
        let data = encode_stream(&values, 7).unwrap();
        let info = inspect(&data, &Limits::unrestricted()).unwrap();
        assert_eq!(info.header.block_count, 143); // ceil(1000/7)
        for (b, entry) in info.index.iter().enumerate() {
            let block = decode_block_at(&data, b as u32, &Limits::unrestricted()).unwrap();
            let start = entry.first_value_index as usize;
            let end = (start + 7).min(values.len());
            assert_eq!(block, &values[start..end]);
        }
    }
}
