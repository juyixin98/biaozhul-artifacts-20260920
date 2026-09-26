//! Decoder and stream inspector.
//!
//! All length fields are validated before any allocation or bit reading, so a
//! corrupt or hostile stream cannot trigger out-of-bounds allocation, shifts
//! by 64+ bits, or panics.

use crate::bitio::BitReader;
use crate::error::Error;
use crate::format::*;
use crate::zigzag_decode;

/// Resource limits enforced while decoding.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Maximum number of blocks in a stream.
    pub max_blocks: u32,
    /// Maximum total value count declared in the header.
    pub max_total_values: u64,
    /// Maximum values per block (never above MAX_BLOCK_VALUES).
    pub max_block_values: u32,
    /// Maximum values a single decode call may produce.
    pub max_output_values: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_blocks: 1 << 20,
            max_total_values: 1 << 24,
            max_block_values: MAX_BLOCK_VALUES,
            max_output_values: 1 << 24,
        }
    }
}

/// One entry of the stream index.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct IndexEntry {
    /// Absolute offset of the block in the stream.
    pub offset: u64,
    /// Cumulative index of the block's first value.
    pub first_value: u64,
    /// Number of values in the block.
    pub count: u32,
}

/// Validated block header plus the block's total byte length.
#[derive(Clone, Copy, Debug)]
pub struct BlockMeta {
    pub count: u32,
    pub base: i64,
    pub bit_width: u8,
    pub escape_count: u32,
    /// Total byte length of the block including header, packed data, escapes.
    pub total_len: usize,
}

/// Result of [`inspect`]: header, index, and per-block metadata.
#[derive(Clone, Debug)]
pub struct StreamInfo {
    pub version: u8,
    pub block_count: u32,
    pub total_values: u64,
    pub index_offset: u64,
    pub index: Vec<IndexEntry>,
    pub blocks: Vec<BlockMeta>,
}

struct Header {
    block_count: u32,
    total_values: u64,
    index_offset: u64,
}

fn take<'a>(data: &'a [u8], pos: &mut usize, n: usize) -> Result<&'a [u8], Error> {
    let end = pos.checked_add(n).ok_or(Error::Truncated)?;
    if end > data.len() {
        return Err(Error::Truncated);
    }
    let s = &data[*pos..end];
    *pos = end;
    Ok(s)
}

fn read_u8(data: &[u8], pos: &mut usize) -> Result<u8, Error> {
    Ok(take(data, pos, 1)?[0])
}

fn read_u32(data: &[u8], pos: &mut usize) -> Result<u32, Error> {
    Ok(u32::from_le_bytes(take(data, pos, 4)?.try_into().unwrap()))
}

fn read_u64(data: &[u8], pos: &mut usize) -> Result<u64, Error> {
    Ok(u64::from_le_bytes(take(data, pos, 8)?.try_into().unwrap()))
}

fn invalid(msg: impl Into<String>) -> Error {
    Error::InvalidData(msg.into())
}

fn parse_header(data: &[u8], limits: &Limits) -> Result<Header, Error> {
    let mut pos = 0usize;
    if take(data, &mut pos, 4)? != MAGIC {
        return Err(Error::BadMagic);
    }
    let version = read_u8(data, &mut pos)?;
    if version != VERSION {
        return Err(Error::UnsupportedVersion(version));
    }
    let flags = read_u8(data, &mut pos)?;
    let reserved = u16::from_le_bytes(take(data, &mut pos, 2)?.try_into().unwrap());
    if flags != 0 || reserved != 0 {
        return Err(Error::InvalidFlags);
    }
    let block_count = read_u32(data, &mut pos)?;
    let total_values = read_u64(data, &mut pos)?;
    let index_offset = read_u64(data, &mut pos)?;

    if block_count > limits.max_blocks {
        return Err(Error::LimitExceeded("block count"));
    }
    if total_values > limits.max_total_values {
        return Err(Error::LimitExceeded("total values"));
    }
    if block_count == 0 && total_values != 0 {
        return Err(invalid("zero blocks but nonzero total_values"));
    }
    Ok(Header {
        block_count,
        total_values,
        index_offset,
    })
}

fn parse_index(data: &[u8], header: &Header) -> Result<Vec<IndexEntry>, Error> {
    let index_offset = usize::try_from(header.index_offset)
        .map_err(|_| invalid("index offset does not fit usize"))?;
    if index_offset < HEADER_LEN {
        return Err(invalid("index offset inside header"));
    }
    let index_bytes = (header.block_count as usize)
        .checked_mul(INDEX_ENTRY_LEN)
        .ok_or_else(|| invalid("index size overflow"))?;
    let index_end = index_offset
        .checked_add(index_bytes)
        .ok_or_else(|| invalid("index end overflow"))?;
    if index_end > data.len() {
        return Err(Error::Truncated);
    }

    let mut entries = Vec::with_capacity(header.block_count as usize);
    let mut pos = index_offset;
    let mut running = 0u64;
    for _ in 0..header.block_count {
        let offset = read_u64(data, &mut pos)?;
        let first_value = read_u64(data, &mut pos)?;
        let count = read_u32(data, &mut pos)?;
        if offset < HEADER_LEN as u64 || offset >= header.index_offset {
            return Err(invalid("block offset outside block region"));
        }
        if first_value != running {
            return Err(invalid("index first_value not cumulative"));
        }
        running = running
            .checked_add(u64::from(count))
            .ok_or_else(|| invalid("value count overflow"))?;
        entries.push(IndexEntry {
            offset,
            first_value,
            count,
        });
    }
    if running != header.total_values {
        return Err(invalid("index counts do not sum to total_values"));
    }
    Ok(entries)
}

/// Parse and validate a block header at `offset`, including bounds checks for
/// the packed section and the escape table. Does not decode values.
fn parse_block_meta(data: &[u8], offset: usize, limits: &Limits) -> Result<BlockMeta, Error> {
    let mut pos = offset;
    let count = read_u32(data, &mut pos)?;
    if count == 0 || count > limits.max_block_values {
        return Err(invalid("block count out of range"));
    }
    let base = i64::from_le_bytes(take(data, &mut pos, 8)?.try_into().unwrap());
    let bit_width = read_u8(data, &mut pos)?;
    if bit_width > 64 {
        return Err(invalid("bit width exceeds 64"));
    }
    let escape_count = read_u32(data, &mut pos)?;
    if escape_count > count {
        return Err(invalid("escape count exceeds block count"));
    }
    // count <= 256 and bit_width <= 64, so this cannot overflow.
    let packed_len = (count as usize * bit_width as usize).div_ceil(8);
    let escapes_len = escape_count as usize * ESCAPE_ENTRY_LEN;
    let total_len = BLOCK_HEADER_LEN + packed_len + escapes_len;
    let end = offset
        .checked_add(total_len)
        .ok_or_else(|| invalid("block length overflow"))?;
    if end > data.len() {
        return Err(Error::Truncated);
    }

    // Validate escape entries: indices must be < count and strictly increasing.
    let mut epos = offset + BLOCK_HEADER_LEN + packed_len;
    let mut prev: Option<u32> = None;
    for _ in 0..escape_count {
        let idx = read_u32(data, &mut epos)?;
        let _value = read_u64(data, &mut epos)?;
        if idx >= count {
            return Err(invalid("escape index out of range"));
        }
        if prev.is_some_and(|p| idx <= p) {
            return Err(invalid("escape indices not strictly increasing"));
        }
        prev = Some(idx);
    }

    Ok(BlockMeta {
        count,
        base,
        bit_width,
        escape_count,
        total_len,
    })
}

fn decode_block_at(data: &[u8], offset: usize, limits: &Limits) -> Result<(Vec<i64>, BlockMeta), Error> {
    let meta = parse_block_meta(data, offset, limits)?;
    let packed_start = offset + BLOCK_HEADER_LEN;
    let packed_len = (meta.count as usize * meta.bit_width as usize).div_ceil(8);
    let packed = &data[packed_start..packed_start + packed_len];
    let escapes_start = packed_start + packed_len;

    let mut out = Vec::with_capacity(meta.count as usize);
    let mut reader = BitReader::new(packed);
    // Escape entries are consumed lazily, one at a time, in index order.
    let mut epos = escapes_start;
    let mut next_escape: Option<(u32, i64)> = None;
    let mut escapes_remaining = meta.escape_count;
    for i in 0..meta.count {
        // Every slot occupies bit_width bits in the packed stream, including
        // escape slots (their content is a placeholder and is discarded).
        let z = reader.read(u32::from(meta.bit_width))?;
        if next_escape.is_none() && escapes_remaining > 0 {
            let idx = u32::from_le_bytes(take(data, &mut epos, 4)?.try_into().unwrap());
            let val = i64::from_le_bytes(take(data, &mut epos, 8)?.try_into().unwrap());
            next_escape = Some((idx, val));
            escapes_remaining -= 1;
        }
        if let Some((idx, val)) = next_escape {
            if idx == i {
                out.push(val);
                next_escape = None;
                continue;
            }
        }
        let delta = zigzag_decode(z);
        let value = meta
            .base
            .checked_add(delta)
            .ok_or_else(|| invalid("base + delta overflows i64"))?;
        out.push(value);
    }
    Ok((out, meta))
}

fn parse_stream(data: &[u8], limits: &Limits) -> Result<(Header, Vec<IndexEntry>), Error> {
    let header = parse_header(data, limits)?;
    let index = parse_index(data, &header)?;
    Ok((header, index))
}

/// Validate a stream and return its header, index, and per-block metadata
/// without decoding any values.
pub fn inspect(data: &[u8], limits: &Limits) -> Result<StreamInfo, Error> {
    let (header, index) = parse_stream(data, limits)?;
    let mut blocks = Vec::with_capacity(index.len());
    for entry in &index {
        let meta = parse_block_meta(data, entry.offset as usize, limits)?;
        if meta.count != entry.count {
            return Err(invalid("block count does not match index"));
        }
        blocks.push(meta);
    }
    Ok(StreamInfo {
        version: VERSION,
        block_count: header.block_count,
        total_values: header.total_values,
        index_offset: header.index_offset,
        index,
        blocks,
    })
}

/// Decode the entire stream into a vector of values.
pub fn decode_all(data: &[u8], limits: &Limits) -> Result<Vec<i64>, Error> {
    let (header, index) = parse_stream(data, limits)?;
    if header.total_values > limits.max_output_values {
        return Err(Error::LimitExceeded("output values"));
    }
    let mut out = Vec::with_capacity(header.total_values as usize);
    for entry in &index {
        let (values, meta) = decode_block_at(data, entry.offset as usize, limits)?;
        if meta.count != entry.count {
            return Err(invalid("block count does not match index"));
        }
        out.extend_from_slice(&values);
    }
    Ok(out)
}

/// Decode a single block located through the stream index.
pub fn decode_block(data: &[u8], block_index: u32, limits: &Limits) -> Result<Vec<i64>, Error> {
    let (header, index) = parse_stream(data, limits)?;
    let entry = index
        .get(block_index as usize)
        .ok_or_else(|| invalid("block index out of range"))?;
    if u64::from(entry.count) > limits.max_output_values {
        return Err(Error::LimitExceeded("output values"));
    }
    let _ = header;
    let (values, _) = decode_block_at(data, entry.offset as usize, limits)?;
    Ok(values)
}
