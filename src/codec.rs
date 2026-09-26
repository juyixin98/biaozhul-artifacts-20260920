//! Streaming binary codec.
//!
//! The format is fully specified in `docs/format.md`. Encoding writes to any
//! [`Write`]; decoding reads from any [`Read`] one chunk at a time, so neither
//! side needs the whole message resident at once.
//!
//! Decoding is bounded on three axes:
//! * [`DecodeLimits::max_bytes`] — total bytes that may be read;
//! * [`DecodeLimits::max_values`] — total distinct values that may be
//!   produced;
//! * structural caps built into the format (at most 65 536 chunks, at most
//!   4 096 values per array container, exactly 8 KiB per bitmap container).

use std::io::{Read, Write};

use crate::container::{Container, ARRAY_MAX, BITMAP_WORDS};
use crate::error::{Error, Result};
use crate::set::IntSet;

/// Magic preamble of every encoded stream: ASCII `RBST`.
pub const MAGIC: [u8; 4] = *b"RBST";
/// Format version produced and accepted by this implementation.
pub const VERSION: u8 = 1;

const KIND_ARRAY: u8 = 0;
const KIND_BITMAP: u8 = 1;
/// Number of possible high keys (16-bit key space).
const MAX_CHUNKS: u64 = 1 << 16;
/// Largest legal cardinality of a single container.
const MAX_CONTAINER_CARD: u64 = 1 << 16;

/// Budgets imposed on a decode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DecodeLimits {
    /// Maximum number of input bytes the decoder will read in total
    /// (including the fixed header).
    pub max_bytes: u64,
    /// Maximum number of distinct values that may be decoded.
    pub max_values: u64,
}

impl DecodeLimits {
    /// Construct limits; both budgets must be non-zero.
    pub fn new(max_bytes: u64, max_values: u64) -> Result<Self> {
        if max_bytes == 0 || max_values == 0 {
            return Err(Error::LengthLimitExceeded {
                declared: 0,
                limit: 0,
            });
        }
        Ok(Self {
            max_bytes,
            max_values,
        })
    }
}

/// Reader wrapper that counts every consumed byte against a budget.
struct CountedReader<R> {
    inner: R,
    read: u64,
    budget: u64,
}

impl<R: Read> CountedReader<R> {
    fn new(inner: R, budget: u64) -> Self {
        Self {
            inner,
            read: 0,
            budget,
        }
    }

    /// Read exactly `buf.len()` bytes. A clean EOF is reported with the byte
    /// count the stream would have had to contain.
    fn read_exact_counted(&mut self, buf: &mut [u8]) -> Result<()> {
        let need = buf.len() as u64;
        if need > self.budget {
            return Err(Error::ByteLimitExceeded);
        }
        self.inner.read_exact(buf).map_err(|e| {
            if e.kind() == std::io::ErrorKind::UnexpectedEof {
                Error::UnexpectedEof {
                    needed: self.read + need,
                    available: self.read,
                }
            } else {
                Error::Io(e.to_string())
            }
        })?;
        self.read += need;
        self.budget -= need;
        Ok(())
    }

    fn read_u8(&mut self) -> Result<u8> {
        let mut b = [0u8; 1];
        self.read_exact_counted(&mut b)?;
        Ok(b[0])
    }

    fn read_u16(&mut self) -> Result<u16> {
        let mut b = [0u8; 2];
        self.read_exact_counted(&mut b)?;
        Ok(u16::from_le_bytes(b))
    }

    fn read_u32(&mut self) -> Result<u32> {
        let mut b = [0u8; 4];
        self.read_exact_counted(&mut b)?;
        Ok(u32::from_le_bytes(b))
    }

    fn read_u64(&mut self) -> Result<u64> {
        let mut b = [0u8; 8];
        self.read_exact_counted(&mut b)?;
        Ok(u64::from_le_bytes(b))
    }
}

fn io_err(e: std::io::Error) -> Error {
    Error::Io(e.to_string())
}

/// Encode a set to a writer in the documented binary format.
///
/// Returns the number of bytes written.
pub fn encode_set<W: Write>(set: &IntSet, writer: &mut W) -> Result<u64> {
    let chunk_count = set.chunk_count();
    let total = set.len();
    // A u32 universe holds at most 2^32 distinct values; the key space has
    // at most 2^16 non-empty chunks.
    if chunk_count > (1usize << 16) {
        return Err(Error::Malformed(0, "more than 2^16 chunks"));
    }
    if total > 1u64 << 32 {
        return Err(Error::Malformed(0, "set larger than the u32 universe"));
    }

    writer.write_all(&MAGIC).map_err(io_err)?;
    writer.write_all(&[VERSION, 0]).map_err(io_err)?;
    writer
        .write_all(&(chunk_count as u32).to_le_bytes())
        .map_err(io_err)?;
    writer.write_all(&total.to_le_bytes()).map_err(io_err)?;
    let mut written = 18u64;

    for (&hi, c) in set.iter_chunks() {
        writer.write_all(&hi.to_le_bytes()).map_err(io_err)?;
        written += 2;
        match c {
            Container::Array(a) => {
                writer.write_all(&[KIND_ARRAY]).map_err(io_err)?;
                let card = a.len() as u16;
                writer.write_all(&card.to_le_bytes()).map_err(io_err)?;
                for &v in a {
                    writer.write_all(&v.to_le_bytes()).map_err(io_err)?;
                }
                written += 3 + 2 * a.len() as u64;
            }
            Container::Bitmap(b) => {
                writer.write_all(&[KIND_BITMAP]).map_err(io_err)?;
                let pop: u32 = b.iter().map(|w| w.count_ones()).sum();
                writer.write_all(&pop.to_le_bytes()).map_err(io_err)?;
                for word in b.iter() {
                    writer.write_all(&word.to_le_bytes()).map_err(io_err)?;
                }
                written += 5 + (BITMAP_WORDS as u64) * 8;
            }
        }
    }
    writer.flush().map_err(io_err)?;
    Ok(written)
}

/// Convenience: encode to a fresh byte vector.
pub fn encode_to_vec(set: &IntSet) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    encode_set(set, &mut buf)?;
    Ok(buf)
}

/// Decode a set from a byte slice under the given limits.
pub fn decode_from_slice(bytes: &[u8], limits: DecodeLimits) -> Result<IntSet> {
    decode_set(bytes, limits)
}

/// Streaming decode: consume one [`Read`] and build the bounded set.
pub fn decode_set<R: Read>(reader: R, limits: DecodeLimits) -> Result<IntSet> {
    let mut r = CountedReader::new(reader, limits.max_bytes);

    // --- 18-byte header ---------------------------------------------------
    let mut magic = [0u8; 4];
    r.read_exact_counted(&mut magic)?;
    if magic != MAGIC {
        return Err(Error::BadMagic);
    }
    let version = r.read_u8()?;
    if version != VERSION {
        return Err(Error::BadMagic);
    }
    let flags = r.read_u8()?;
    if flags != 0 {
        return Err(Error::Malformed(6, "unsupported flag bits set"));
    }
    let chunk_count = r.read_u32()? as u64;
    if chunk_count > MAX_CHUNKS {
        return Err(Error::Malformed(
            7,
            "chunk count exceeds the 16-bit key space",
        ));
    }
    let total = r.read_u64()?;
    if total > limits.max_values {
        return Err(Error::LengthLimitExceeded {
            declared: total,
            limit: limits.max_values,
        });
    }

    let mut set = IntSet::new();
    let mut values_seen: u64 = 0;
    let mut prev_key: Option<u16> = None;

    for _ in 0..chunk_count {
        let key = r.read_u16()?;
        if let Some(prev) = prev_key {
            if key <= prev {
                return Err(Error::Malformed(
                    r.read - 2,
                    "chunk keys must be strictly ascending and unique",
                ));
            }
        }
        prev_key = Some(key);

        let kind_at = r.read;
        let kind = r.read_u8()?;
        match kind {
            KIND_ARRAY => {
                let card = r.read_u16()? as u64;
                if card == 0 {
                    return Err(Error::Malformed(
                        kind_at,
                        "empty containers must not be stored",
                    ));
                }
                if card > ARRAY_MAX as u64 {
                    return Err(Error::Malformed(
                        kind_at,
                        "array container declares more than 4096 values",
                    ));
                }
                if values_seen
                    .checked_add(card)
                    .is_none_or(|s| s > limits.max_values)
                {
                    return Err(Error::ValueLimitExceeded);
                }
                let mut lows = Vec::with_capacity(card as usize);
                let mut last: Option<u16> = None;
                for _ in 0..card {
                    let v_at = r.read;
                    let v = r.read_u16()?;
                    if let Some(prev) = last {
                        if v <= prev {
                            return Err(Error::Malformed(
                                v_at,
                                "array values must be strictly ascending and unique",
                            ));
                        }
                    }
                    last = Some(v);
                    lows.push(v);
                }
                values_seen += card;
                set.push_group(key, lows);
            }
            KIND_BITMAP => {
                let card = r.read_u32()? as u64;
                if card == 0 {
                    return Err(Error::Malformed(
                        kind_at,
                        "empty containers must not be stored",
                    ));
                }
                if card > MAX_CONTAINER_CARD {
                    return Err(Error::Malformed(
                        kind_at,
                        "bitmap cardinality cannot exceed 65536",
                    ));
                }
                if values_seen
                    .checked_add(card)
                    .is_none_or(|s| s > limits.max_values)
                {
                    return Err(Error::ValueLimitExceeded);
                }
                let mut words = Box::new([0u64; BITMAP_WORDS]);
                for w in words.iter_mut() {
                    *w = r.read_u64()?;
                }
                let actual: u64 = words.iter().map(|x| x.count_ones() as u64).sum();
                if actual != card {
                    return Err(Error::Malformed(
                        r.read,
                        "bitmap payload does not match declared cardinality",
                    ));
                }
                values_seen += actual;
                set.push_bitmap_group(key, words);
            }
            _ => {
                return Err(Error::Malformed(
                    kind_at,
                    "unknown container kind (only 0=array, 1=bitmap)",
                ));
            }
        }
    }

    if values_seen != total {
        return Err(Error::Malformed(
            r.read,
            "sum of container cardinalities does not match header total",
        ));
    }
    Ok(set)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::container::BITMAP_WORDS;

    fn limits() -> DecodeLimits {
        DecodeLimits {
            max_bytes: 64 * 1024 * 1024,
            max_values: 1 << 33,
        }
    }

    #[test]
    fn round_trip_small_array() {
        let s = IntSet::from_values(&[1, 2, 0x0001_0005, u32::MAX, 2]);
        let buf = encode_to_vec(&s).unwrap();
        let back = decode_from_slice(&buf, limits()).unwrap();
        assert_eq!(s, back);
        assert_eq!(back.len(), 4);
    }

    #[test]
    fn round_trip_forces_bitmap_over_crossover() {
        // 4097 values in one chunk -> bitmap on the wire.
        let vals: Vec<u32> = (0..(ARRAY_MAX as u32 + 1)).collect();
        let s = IntSet::from_values(&vals);
        let buf = encode_to_vec(&s).unwrap();
        // header 18 + key 2 + kind/card 5 + 8192
        assert_eq!(buf.len(), 18 + 2 + 5 + BITMAP_WORDS * 8);
        let back = decode_from_slice(&buf, limits()).unwrap();
        assert_eq!(s, back);
    }

    #[test]
    fn full_universe_round_trips() {
        // One bitmap chunk holding every possible low value (card 65536,
        // which does not fit in u16).
        let s = IntSet::from_values(&(0..=u16::MAX as u32).collect::<Vec<_>>());
        let buf = encode_to_vec(&s).unwrap();
        let back = decode_from_slice(&buf, limits()).unwrap();
        assert_eq!(s, back);
        assert_eq!(back.len(), 65_536);
    }

    #[test]
    fn rejects_truncated_stream() {
        let s = IntSet::from_values(&[1, 2, 3]);
        let buf = encode_to_vec(&s).unwrap();
        let cut = buf.len() - 1;
        let err = decode_from_slice(&buf[..cut], limits()).unwrap_err();
        assert!(matches!(err, Error::UnexpectedEof { .. }), "got {err:?}");
    }

    #[test]
    fn rejects_bad_magic_and_version() {
        let mut buf = encode_to_vec(&IntSet::from_values(&[1])).unwrap();
        buf[0] = b'X';
        assert!(matches!(
            decode_from_slice(&buf, limits()),
            Err(Error::BadMagic)
        ));
        buf[0] = b'R';
        buf[4] = 99;
        assert!(matches!(
            decode_from_slice(&buf, limits()),
            Err(Error::BadMagic)
        ));
    }

    #[test]
    fn byte_limit_stops_decode() {
        let s = IntSet::from_values(&(0..10_000u32).collect::<Vec<_>>());
        let buf = encode_to_vec(&s).unwrap();
        let tight = DecodeLimits {
            max_bytes: 20,
            max_values: 1 << 33,
        };
        assert!(matches!(
            decode_from_slice(&buf, tight),
            Err(Error::ByteLimitExceeded)
        ));
    }

    #[test]
    fn value_limit_stops_decode() {
        let s = IntSet::from_values(&(0..100u32).collect::<Vec<_>>());
        let buf = encode_to_vec(&s).unwrap();
        let tight = DecodeLimits {
            max_bytes: 1 << 30,
            max_values: 50,
        };
        assert!(matches!(
            decode_from_slice(&buf, tight),
            Err(Error::LengthLimitExceeded { .. })
        ));
    }

    #[test]
    fn malicious_array_length_truncated() {
        // Claim 4096 array entries but provide none: must hit EOF, never
        // allocate 8 KiB then over-read.
        let mut buf = Vec::new();
        buf.extend_from_slice(&MAGIC);
        buf.extend_from_slice(&[VERSION, 0]);
        buf.extend_from_slice(&1u32.to_le_bytes());
        buf.extend_from_slice(&4096u64.to_le_bytes());
        buf.extend_from_slice(&0u16.to_le_bytes());
        buf.push(KIND_ARRAY);
        buf.extend_from_slice(&4096u16.to_le_bytes());
        let err = decode_from_slice(&buf, limits()).unwrap_err();
        assert!(matches!(err, Error::UnexpectedEof { .. }), "got {err:?}");
    }

    #[test]
    fn malicious_array_length_over_cap() {
        let mut buf = Vec::new();
        buf.extend_from_slice(&MAGIC);
        buf.extend_from_slice(&[VERSION, 0]);
        buf.extend_from_slice(&1u32.to_le_bytes());
        buf.extend_from_slice(&5000u64.to_le_bytes());
        buf.extend_from_slice(&0u16.to_le_bytes());
        buf.push(KIND_ARRAY);
        buf.extend_from_slice(&5000u16.to_le_bytes());
        let err = decode_from_slice(&buf, limits()).unwrap_err();
        assert!(matches!(
            err,
            Error::Malformed(_, "array container declares more than 4096 values")
        ));
    }

    #[test]
    fn rejects_non_ascending_array_values() {
        let s = IntSet::from_values(&[1, 2]);
        let mut buf = encode_to_vec(&s).unwrap();
        // Chunk starts at offset 18: key(2) kind(1) card(2) vals...
        buf[18 + 5] = 9;
        buf[18 + 7] = 5;
        assert!(matches!(
            decode_from_slice(&buf, limits()),
            Err(Error::Malformed(_, msg)) if msg.contains("strictly ascending")
        ));
    }

    #[test]
    fn rejects_duplicate_chunk_key() {
        // Two chunks both carrying key 7.
        let mut buf = Vec::new();
        buf.extend_from_slice(&MAGIC);
        buf.extend_from_slice(&[VERSION, 0]);
        buf.extend_from_slice(&2u32.to_le_bytes());
        buf.extend_from_slice(&2u64.to_le_bytes());
        for _ in 0..2 {
            buf.extend_from_slice(&7u16.to_le_bytes());
            buf.push(KIND_ARRAY);
            buf.extend_from_slice(&1u16.to_le_bytes());
            buf.extend_from_slice(&1u16.to_le_bytes());
        }
        assert!(matches!(
            decode_from_slice(&buf, limits()),
            Err(Error::Malformed(_, msg)) if msg.contains("strictly ascending")
        ));
    }

    #[test]
    fn bitmap_cardinality_lie_rejected() {
        let s = IntSet::from_values(&(0..5000u32).collect::<Vec<_>>());
        let mut buf = encode_to_vec(&s).unwrap();
        // Header 18 + key 2 + kind 1 -> bitmap card u32 at bytes 21..25.
        buf[21] = buf[21].wrapping_add(1);
        assert!(matches!(
            decode_from_slice(&buf, limits()),
            Err(Error::Malformed(
                _,
                "bitmap payload does not match declared cardinality"
            ))
        ));
    }

    #[test]
    fn header_total_lie_rejected() {
        let s = IntSet::from_values(&[1, 2, 3]);
        let mut buf = encode_to_vec(&s).unwrap();
        buf[10..18].copy_from_slice(&99u64.to_le_bytes());
        assert!(matches!(
            decode_from_slice(&buf, limits()),
            Err(Error::Malformed(_, msg)) if msg.contains("does not match header total")
        ));
    }
}
