//! Encoder: turns a slice of `i64` values into a bpb stream.

use crate::bitio::BitWriter;
use crate::error::Error;
use crate::format::*;
use crate::zigzag_encode;

/// Encoder options.
#[derive(Clone, Copy, Debug)]
pub struct EncodeOptions {
    /// Maximum number of values per block, 1..=MAX_BLOCK_VALUES.
    pub block_size: u32,
}

impl Default for EncodeOptions {
    fn default() -> Self {
        Self {
            block_size: DEFAULT_BLOCK_SIZE,
        }
    }
}

/// Cost model: one escape entry costs 12 bytes = 96 bits.
const ESCAPE_COST_BITS: u128 = 96;

struct BlockEnc {
    count: u32,
    base: i64,
    bit_width: u8,
    escapes: Vec<(u32, i64)>,
    packed: Vec<u8>,
}

/// Pick the bit width minimizing `count * width + escapes * 96` bits.
/// Ties resolve to the smaller width.
fn best_width(zigzags: &[Option<u64>], forced_escapes: u64) -> u8 {
    let count = zigzags.len() as u128;
    let mut best_w = 0u8;
    let mut best_cost = u128::MAX;
    for w in 0u32..=64 {
        let threshold = 1u128 << w; // zigzag values >= threshold need escaping
        let mut escapes = forced_escapes as u128;
        for z in zigzags.iter().flatten() {
            if u128::from(*z) >= threshold {
                escapes += 1;
            }
        }
        let cost = count * u128::from(w) + escapes * ESCAPE_COST_BITS;
        if cost < best_cost {
            best_cost = cost;
            best_w = w as u8;
        }
    }
    best_w
}

fn build_block(vals: &[i64]) -> BlockEnc {
    let count = vals.len() as u32;
    let base = vals[0];

    // Deltas are computed in i128: a delta outside the i64 range cannot be
    // zigzag-encoded into 64 bits and becomes a forced escape.
    let mut zigzags: Vec<Option<u64>> = Vec::with_capacity(vals.len());
    let mut forced_escapes = 0u64;
    for &v in vals {
        let delta = i128::from(v) - i128::from(base);
        if delta >= i128::from(i64::MIN) && delta <= i128::from(i64::MAX) {
            zigzags.push(Some(zigzag_encode(delta as i64)));
        } else {
            zigzags.push(None);
            forced_escapes += 1;
        }
    }

    let bit_width = best_width(&zigzags, forced_escapes);
    let threshold = 1u128 << bit_width;

    let mut writer = BitWriter::new();
    let mut escapes: Vec<(u32, i64)> = Vec::new();
    for (i, z) in zigzags.iter().enumerate() {
        let escape = match z {
            None => true,
            Some(z) => u128::from(*z) >= threshold,
        };
        if escape {
            escapes.push((i as u32, vals[i]));
            writer.push(0, u32::from(bit_width)); // slot content is ignored on decode
        } else {
            writer.push(z.unwrap(), u32::from(bit_width));
        }
    }

    BlockEnc {
        count,
        base,
        bit_width,
        escapes,
        packed: writer.finish(),
    }
}

/// Encode `values` into a complete bpb stream (header + blocks + index).
pub fn encode(values: &[i64], opts: &EncodeOptions) -> Result<Vec<u8>, Error> {
    if opts.block_size == 0 || opts.block_size > MAX_BLOCK_VALUES {
        return Err(Error::InvalidData(format!(
            "block_size must be in 1..={MAX_BLOCK_VALUES}, got {}",
            opts.block_size
        )));
    }

    let mut blocks = Vec::new();
    for chunk in values.chunks(opts.block_size as usize) {
        blocks.push(build_block(chunk));
    }

    // Serialize block bodies, remembering each block's absolute offset.
    let mut bodies = Vec::new();
    let mut index_entries = Vec::with_capacity(blocks.len());
    let mut offset = HEADER_LEN as u64;
    let mut first_value = 0u64;
    for b in &blocks {
        index_entries.push((offset, first_value, b.count));
        let mut body = Vec::with_capacity(
            BLOCK_HEADER_LEN + b.packed.len() + b.escapes.len() * ESCAPE_ENTRY_LEN,
        );
        body.extend_from_slice(&b.count.to_le_bytes());
        body.extend_from_slice(&b.base.to_le_bytes());
        body.push(b.bit_width);
        body.extend_from_slice(&(b.escapes.len() as u32).to_le_bytes());
        body.extend_from_slice(&b.packed);
        for &(idx, val) in &b.escapes {
            body.extend_from_slice(&idx.to_le_bytes());
            body.extend_from_slice(&val.to_le_bytes());
        }
        offset += body.len() as u64;
        first_value += u64::from(b.count);
        bodies.extend_from_slice(&body);
    }

    let index_offset = HEADER_LEN as u64 + bodies.len() as u64;

    let mut out = Vec::with_capacity(index_offset as usize + index_entries.len() * INDEX_ENTRY_LEN);
    out.extend_from_slice(MAGIC);
    out.push(VERSION);
    out.push(0); // flags
    out.extend_from_slice(&0u16.to_le_bytes()); // reserved
    out.extend_from_slice(&(blocks.len() as u32).to_le_bytes());
    out.extend_from_slice(&(values.len() as u64).to_le_bytes());
    out.extend_from_slice(&index_offset.to_le_bytes());
    out.extend_from_slice(&bodies);
    for (off, first, count) in index_entries {
        out.extend_from_slice(&off.to_le_bytes());
        out.extend_from_slice(&first.to_le_bytes());
        out.extend_from_slice(&count.to_le_bytes());
    }
    Ok(out)
}
