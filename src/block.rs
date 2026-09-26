//! Block-level encoding: base value + bit-packed zigzag deltas + escapes.
//!
//! Layout of one block (all integers little-endian):
//!
//! ```text
//! offset  size  field
//! 0       8     base            i64, first value of the block
//! 8       1     bit_width       u8, 0..=64
//! 9       4     value_count     u32, >= 1
//! 13      4     exception_count u32, <= value_count
//! 17      4     packed_len      u32, bytes of the packed region
//! 21      3     reserved        must be zero
//! 24      ..    packed region   (value_count - exception_count) values,
//!                               bit_width bits each, LSB-first
//! ..      ..    exception table exception_count entries of
//!                               (index u32, value i64), index strictly
//!                               increasing and < value_count
//! ```
//!
//! Each non-exception slot stores `zigzag(value - base)` in `bit_width`
//! bits. Slots whose zigzag image does not fit are omitted from the packed
//! region and carried verbatim in the exception table; the decoder
//! recognizes them purely by index, so no in-band marker is needed and
//! bit width 0 (all deltas zero) works without special cases.

use crate::bitio::{BitReader, BitWriter, MAX_BIT_WIDTH};
use crate::error::{Error, Result};
use crate::zigzag::{bit_len, zigzag_decode, zigzag_encode};

pub const BLOCK_HEADER_LEN: usize = 24;
pub const EXCEPTION_ENTRY_LEN: usize = 12;

/// Decoded view of a block header.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BlockHeader {
    pub base: i64,
    pub bit_width: u8,
    pub value_count: u32,
    pub exception_count: u32,
    pub packed_len: u32,
}

/// Fraction of values that may become exceptions when picking the width:
/// the width is the 7/8-quantile of the per-value bit lengths.
const WIDTH_QUANTILE_NUM: usize = 7;
const WIDTH_QUANTILE_DEN: usize = 8;

/// Encode a non-empty slice of values into one block.
pub fn encode_block(values: &[i64]) -> Result<Vec<u8>> {
    if values.is_empty() {
        return Err(Error::Corrupt("cannot encode an empty block"));
    }
    if values.len() > u32::MAX as usize {
        return Err(Error::LimitExceeded("block value count"));
    }
    let base = values[0];
    let zz: Vec<u128> = values
        .iter()
        .map(|&v| zigzag_encode(v as i128 - base as i128))
        .collect();

    // Pick the width as the 7/8-quantile of bit lengths (capped at 64);
    // larger outliers escape into the exception table.
    let mut lens: Vec<u32> = zz.iter().map(|&z| bit_len(z)).collect();
    lens.sort_unstable();
    let width = lens[values.len() * WIDTH_QUANTILE_NUM / WIDTH_QUANTILE_DEN]
        .min(MAX_BIT_WIDTH);

    let mut writer = BitWriter::new();
    let mut exceptions: Vec<(u32, i64)> = Vec::new();
    for (i, (&z, &v)) in zz.iter().zip(values).enumerate() {
        if bit_len(z) > width {
            exceptions.push((i as u32, v));
        } else {
            // bit_len(z) <= width <= 64, so z fits in a u64.
            writer.write_bits(z as u64, width);
        }
    }

    let packed = writer.into_bytes();
    let mut out = Vec::with_capacity(
        BLOCK_HEADER_LEN + packed.len() + exceptions.len() * EXCEPTION_ENTRY_LEN,
    );
    out.extend_from_slice(&base.to_le_bytes());
    out.push(width as u8);
    out.extend_from_slice(&(values.len() as u32).to_le_bytes());
    out.extend_from_slice(&(exceptions.len() as u32).to_le_bytes());
    out.extend_from_slice(&(packed.len() as u32).to_le_bytes());
    out.extend_from_slice(&[0u8; 3]); // reserved
    out.extend_from_slice(&packed);
    for (idx, val) in exceptions {
        out.extend_from_slice(&idx.to_le_bytes());
        out.extend_from_slice(&val.to_le_bytes());
    }
    Ok(out)
}

/// Decode exactly one block from `data`, which must contain the whole block
/// and nothing else. `max_values` caps the output allocation and must be
/// checked by the caller against its resource limits.
pub fn decode_block(data: &[u8], max_values: u32) -> Result<(BlockHeader, Vec<i64>)> {
    if data.len() < BLOCK_HEADER_LEN {
        return Err(Error::UnexpectedEof);
    }
    let base = i64::from_le_bytes(data[0..8].try_into().expect("slice len checked"));
    let width = u32::from(data[8]);
    if width > MAX_BIT_WIDTH {
        return Err(Error::InvalidBitWidth(width));
    }
    let value_count = u32::from_le_bytes(data[9..13].try_into().expect("slice len checked"));
    let exception_count = u32::from_le_bytes(data[13..17].try_into().expect("slice len checked"));
    let packed_len = u32::from_le_bytes(data[17..21].try_into().expect("slice len checked"));
    if data[21..24] != [0, 0, 0] {
        return Err(Error::Corrupt("reserved header bytes must be zero"));
    }
    if value_count == 0 {
        return Err(Error::Corrupt("block declares zero values"));
    }
    if value_count > max_values {
        return Err(Error::LimitExceeded("block value count"));
    }
    if exception_count > value_count {
        return Err(Error::Corrupt("exception count exceeds value count"));
    }

    // The packed length is fully determined by the counts and the width;
    // verify it with 64-bit arithmetic before trusting it for slicing.
    let non_exceptions = u64::from(value_count - exception_count);
    let expected_packed = (non_exceptions * u64::from(width) + 7) / 8;
    if u64::from(packed_len) != expected_packed {
        return Err(Error::Corrupt("packed length mismatch"));
    }
    let expected_total = BLOCK_HEADER_LEN as u64
        + u64::from(packed_len)
        + u64::from(exception_count) * EXCEPTION_ENTRY_LEN as u64;
    if data.len() as u64 != expected_total {
        return Err(Error::Corrupt("block length mismatch"));
    }

    let header = BlockHeader {
        base,
        bit_width: width as u8,
        value_count,
        exception_count,
        packed_len,
    };

    let packed_start = BLOCK_HEADER_LEN;
    let exc_start = packed_start + packed_len as usize;
    let exceptions = parse_exceptions(&data[exc_start..], exception_count, value_count)?;

    let mut values = Vec::with_capacity(value_count as usize);
    let mut reader = BitReader::new(&data[packed_start..exc_start]);
    let mut next_exc = exceptions.iter().copied();
    let mut current_exc = next_exc.next();
    for i in 0..value_count {
        if let Some((idx, val)) = current_exc {
            if idx == i {
                values.push(val);
                current_exc = next_exc.next();
                continue;
            }
        }
        let zz = u128::from(reader.read_bits(width)?);
        let v = base as i128 + zigzag_decode(zz);
        let v = i64::try_from(v).map_err(|_| Error::Corrupt("decoded value out of i64 range"))?;
        values.push(v);
    }
    debug_assert!(current_exc.is_none());
    Ok((header, values))
}

/// Parse and validate the exception table: indices strictly increasing and
/// below `value_count`, so the decode loop consumes every entry exactly once.
fn parse_exceptions(
    region: &[u8],
    exception_count: u32,
    value_count: u32,
) -> Result<Vec<(u32, i64)>> {
    let mut exceptions = Vec::with_capacity(exception_count as usize);
    let mut prev: Option<u32> = None;
    for chunk in region.chunks_exact(EXCEPTION_ENTRY_LEN) {
        let idx = u32::from_le_bytes(chunk[0..4].try_into().expect("chunk len"));
        let val = i64::from_le_bytes(chunk[4..12].try_into().expect("chunk len"));
        if idx >= value_count {
            return Err(Error::Corrupt("exception index out of range"));
        }
        if prev.is_some_and(|p| idx <= p) {
            return Err(Error::Corrupt("exception indices not increasing"));
        }
        prev = Some(idx);
        exceptions.push((idx, val));
    }
    if exceptions.len() != exception_count as usize {
        return Err(Error::Corrupt("exception table truncated"));
    }
    Ok(exceptions)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(values: &[i64]) -> BlockHeader {
        let block = encode_block(values).unwrap();
        let (header, decoded) = decode_block(&block, u32::MAX).unwrap();
        assert_eq!(decoded, values);
        header
    }

    #[test]
    fn constant_block_uses_width_zero() {
        let header = roundtrip(&[-42; 100]);
        assert_eq!(header.bit_width, 0);
        assert_eq!(header.packed_len, 0);
        assert_eq!(header.exception_count, 0);
    }

    #[test]
    fn negative_deltas() {
        roundtrip(&[0, -1, -2, 3, -5, 8, -13, 21]);
    }

    #[test]
    fn extreme_outliers_escape() {
        let mut values = vec![7i64; 64];
        values[10] = i64::MIN;
        values[20] = i64::MAX;
        values[30] = i64::MIN / 2;
        let header = roundtrip(&values);
        assert!(header.exception_count >= 3);
        assert!(header.bit_width < 64);
    }

    #[test]
    fn full_range_deltas_still_roundtrip() {
        // delta spans the entire i128-extended range: exception at width 64.
        roundtrip(&[i64::MIN, i64::MAX, 0, i64::MAX, i64::MIN]);
    }

    #[test]
    fn every_bit_width() {
        for w in 0..=64u32 {
            // zigzag(v) = 2v for v >= 0 and 1 for v = -1; pick a delta whose
            // zigzag image has bit length exactly w, used by most values so
            // the quantile picks width w.
            let delta: i64 = match w {
                0 => 0,
                1 => -1,
                _ => 1i64 << (w - 2),
            };
            let values: Vec<i64> = (0..16).map(|i| if i == 0 { 0 } else { delta }).collect();
            let header = roundtrip(&values);
            assert_eq!(u32::from(header.bit_width), w, "forced width {w}");
        }
    }
}
