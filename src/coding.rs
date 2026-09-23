//! Integer block encoding (Gorilla-lite).
//!
//! Each block holds an ordered run of `(timestamp: i64, value: i64)` points.
//!
//! Payload layout (bit stream, MSB-first, see [`crate::bitio`]):
//! ```text
//! count: u32 LE (byte aligned, 4 bytes)
//! first_ts: 64 bits
//! first_value: 64 bits
//! then, for each subsequent point:
//!   timestamps:
//!     tag 0 -> dod = cur_delta - prev_delta (signed), stored zigzag-varint.
//!              On the first point after a reset, the stored value is the
//!              raw cur_delta instead. Either way it must fit in i64.
//!     tag 1 -> raw 64-bit timestamp; resets the DOD state (overflow escape).
//!   values:
//!     tag 0 -> (value - prev_value), stored zigzag-varint when it fits i64
//!     tag 1 -> raw 64-bit value; resets the first-difference state
//! ```
//!
//! # Overflow semantics
//!
//! All predictor arithmetic is done in `i128`. Whenever a delta-of-delta
//! (a reset delta, or a value difference) does not fit in `i64`, the
//! encoder cannot represent it in the compact form, so it emits the escape
//! tag `1`, stores the raw signed value in 64 bits, and resets the
//! corresponding predictor. Note that the running delta itself is kept as
//! `i128`: a DOD can fit in i64 even when the absolute delta does not
//! (e.g. timestamps `i64::MIN, -1, i64::MAX` give DOD 1 while the raw delta
//! is 2^63). The decoder performs exactly the same i128 reconstruction, so
//! every legal sequence of i64 points round-trips.
//!
//! This module enforces no ordering itself (the storage layer does); points
//! are encoded in the order given.

use crate::bitio::{BitReader, BitWriter, ReadError};

/// One timestamped integer sample.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Point {
    pub ts: i64,
    pub value: i64,
}

impl Point {
    pub fn new(ts: i64, value: i64) -> Self {
        Self { ts, value }
    }
}

/// Map a signed integer to an unsigned one preserving magnitude ordering
/// (small absolute values become small unsigned values).
/// Computed in i128 so that extreme inputs such as i64::MIN cannot overflow.
pub fn zigzag_encode(v: i64) -> u64 {
    (((v as i128) << 1) ^ ((v as i128) >> 63)) as u64
}

pub fn zigzag_decode(v: u64) -> i64 {
    ((v >> 1) as i64) ^ -((v & 1) as i64)
}

/// Encode a block of points into the on-disk payload bytes
/// (4-byte little-endian count followed by the bit stream).
pub fn encode_block(points: &[Point]) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 + points.len() * 2);
    out.extend_from_slice(&(points.len() as u32).to_le_bytes());

    let mut w = BitWriter::with_capacity(points.len() * 2);
    if let Some(first) = points.first() {
        w.write_u64(first.ts as u64);
        w.write_u64(first.value as u64);

        // Timestamp predictor. The running delta is i128 because a DOD may
        // be small even when the raw delta exceeds i64.
        let mut prev_ts: i64 = first.ts;
        let mut prev_delta: Option<i128> = None;
        // Value predictor.
        let mut prev_value: i64 = first.value;

        for p in &points[1..] {
            // ---- timestamp ----
            let cur_delta = (p.ts as i128) - (prev_ts as i128);
            match prev_delta {
                Some(delta) => {
                    let dod = cur_delta - delta;
                    match i64::try_from(dod) {
                        Ok(dod64) => {
                            w.write_bit(false); // tag 0: delta-of-delta
                            w.write_varint_u64(zigzag_encode(dod64));
                            prev_delta = Some(cur_delta);
                        }
                        Err(_) => {
                            w.write_bit(true); // tag 1: raw escape
                            w.write_u64(p.ts as u64);
                            prev_delta = None;
                        }
                    }
                }
                None => {
                    // First point after a reset: store a fresh raw delta.
                    match i64::try_from(cur_delta) {
                        Ok(d) => {
                            w.write_bit(false); // tag 0: initial delta
                            w.write_varint_u64(zigzag_encode(d));
                            prev_delta = Some(cur_delta);
                        }
                        Err(_) => {
                            w.write_bit(true); // tag 1: raw escape
                            w.write_u64(p.ts as u64);
                            // Stay reset: even the single delta overflows.
                        }
                    }
                }
            }
            prev_ts = p.ts;

            // ---- value ----
            let diff = (p.value as i128) - (prev_value as i128);
            match i64::try_from(diff) {
                Ok(d) => {
                    w.write_bit(false);
                    w.write_varint_u64(zigzag_encode(d));
                }
                Err(_) => {
                    w.write_bit(true);
                    w.write_u64(p.value as u64);
                }
            }
            prev_value = p.value;
        }
    }
    out.extend_from_slice(&w.finish());
    out
}

/// Decode a block payload produced by [`encode_block`].
pub fn decode_block(payload: &[u8]) -> Result<Vec<Point>, CodecError> {
    if payload.len() < 4 {
        return Err(CodecError::Truncated);
    }
    let count = u32::from_le_bytes([payload[0], payload[1], payload[2], payload[3]]) as usize;
    let mut r = BitReader::new(&payload[4..]);
    if count == 0 {
        return Ok(Vec::new());
    }

    let first_ts = r.read_u64()? as i64;
    let first_value = r.read_u64()? as i64;
    let mut points = Vec::with_capacity(count);
    points.push(Point::new(first_ts, first_value));

    let mut prev_ts = first_ts;
    let mut prev_delta: Option<i128> = None;
    let mut prev_value = first_value;

    for _ in 1..count {
        // ---- timestamp ----
        let ts = if !r.read_bit()? {
            let d_signed = zigzag_decode(r.read_varint_u64()?) as i128;
            let cur_delta = match prev_delta {
                Some(delta) => delta + d_signed, // delta-of-delta
                None => d_signed,                // initial delta after reset
            };
            let ts = (prev_ts as i128) + cur_delta;
            prev_delta = Some(cur_delta);
            i64::try_from(ts).map_err(|_| CodecError::Overflow)?
        } else {
            let raw = r.read_u64()? as i64;
            prev_delta = None;
            raw
        };
        prev_ts = ts;

        // ---- value ----
        let value = if !r.read_bit()? {
            let d = zigzag_decode(r.read_varint_u64()?) as i128;
            let v = (prev_value as i128) + d;
            i64::try_from(v).map_err(|_| CodecError::Overflow)?
        } else {
            r.read_u64()? as i64
        };
        prev_value = value;

        points.push(Point::new(ts, value));
    }

    Ok(points)
}

#[derive(Debug, PartialEq, Eq)]
pub enum CodecError {
    Truncated,
    UnexpectedEof,
    /// A reconstructed value did not fit in `i64` (corrupt stream).
    Overflow,
}

impl From<ReadError> for CodecError {
    fn from(_: ReadError) -> Self {
        CodecError::UnexpectedEof
    }
}

impl std::fmt::Display for CodecError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CodecError::Truncated => write!(f, "block payload truncated"),
            CodecError::UnexpectedEof => write!(f, "unexpected end of block bit stream"),
            CodecError::Overflow => write!(f, "reconstructed integer overflows i64"),
        }
    }
}

impl std::error::Error for CodecError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(points: &[Point]) -> Vec<Point> {
        let enc = encode_block(points);
        decode_block(&enc).unwrap()
    }

    #[test]
    fn empty_block() {
        assert!(roundtrip(&[]).is_empty());
    }

    #[test]
    fn single_point() {
        let p = vec![Point::new(42, -7)];
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn constant_interval_constant_value() {
        let p: Vec<_> = (0..1000).map(|i| Point::new(1000 + i * 10, 5)).collect();
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn negative_values() {
        let p: Vec<_> = (0..100).map(|i| Point::new(i, -i * 3 - 1)).collect();
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn duplicate_timestamps() {
        let p = vec![Point::new(1, 10), Point::new(1, 11), Point::new(1, -99)];
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn extreme_integers() {
        // i64::MIN / i64::MAX timestamps and values, including jumps whose
        // delta or delta-of-delta overflows i64 and must hit the raw escape.
        let p = vec![
            Point::new(i64::MIN, i64::MAX),
            Point::new(i64::MAX, i64::MIN),
            Point::new(0, 0),
            Point::new(-1, i64::MAX),
            Point::new(i64::MAX - 1, i64::MIN + 1),
        ];
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn small_dod_but_huge_raw_delta() {
        // Deltas are i64::MAX then 2^63 (== i64::MAX+1); the DOD is 1, which
        // fits even though the raw delta itself does not.
        let p = vec![
            Point::new(i64::MIN, 1),
            Point::new(-1, 2),
            Point::new(i64::MAX, 3),
        ];
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn value_diff_overflow() {
        let p = vec![Point::new(0, i64::MAX), Point::new(1, i64::MIN)];
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn timestamp_delta_overflow_pair() {
        // The very first delta (MIN -> MAX) does not fit i64.
        let p = vec![Point::new(i64::MIN, 1), Point::new(i64::MAX, 2)];
        assert_eq!(roundtrip(&p), p);
    }

    #[test]
    fn truncated_payload_errors() {
        assert_eq!(decode_block(&[1, 2, 3]).unwrap_err(), CodecError::Truncated);
        let enc = encode_block(&[Point::new(1, 2), Point::new(3, 4)]);
        let cut = enc.len() - 1;
        assert!(decode_block(&enc[..cut]).is_err());
    }
}
