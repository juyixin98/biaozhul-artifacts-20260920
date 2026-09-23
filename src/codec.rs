//! Block codec.
//!
//! # Payload encoding
//!
//! Timestamps (all integers i64):
//!   ts[0]            : raw, 8 bytes LE
//!   ts[1]            : delta d0 = ts[1]-ts[0]          -> zigzag varint
//!   ts[i] (i >= 2)   : delta-of-delta dod = d(i-1)-d(i-2) -> zigzag varint
//!
//! Values:
//!   v[0]             : raw, 8 bytes LE
//!   v[i] (i >= 1)    : delta dv = v[i]-v[i-1]          -> zigzag varint
//!
//! # Overflow handling (explicit)
//!
//! All deltas are computed in i128. A delta that does not fit in i64 — or
//! that equals i64::MIN (whose zigzag image, u64::MAX, is reserved) — is
//! encoded as the ESCAPE marker (varint u64::MAX) followed by the *absolute*
//! value (the raw timestamp or raw value) as 8 bytes LE. Decoding mirrors
//! this and recomputes the running delta from the absolute value. Decoding
//! also rejects any reconstruction that would leave the i64 range (corrupt
//! data), returning None instead of wrapping.

pub const ESCAPE: u64 = u64::MAX;

pub const FILE_MAGIC: &[u8; 4] = b"TSB1";
pub const FILE_VERSION: u16 = 1;
pub const FILE_HEADER_LEN: usize = 8;
pub const BLOCK_HEADER_LEN: usize = 48;

/// Metadata of one encoded block; also the in-memory index entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockMeta {
    pub count: u32,
    pub ts_start: i64,
    pub ts_end: i64,
    pub val_min: i64,
    pub val_max: i64,
    /// Byte offset of the block header within the data file.
    pub offset: u64,
    /// Total byte length of the block (header + payloads).
    pub len: u64,
}

// ---------------------------------------------------------------- varint

pub fn zigzag_encode(n: i64) -> u64 {
    ((n << 1) ^ (n >> 63)) as u64
}

pub fn zigzag_decode(z: u64) -> i64 {
    ((z >> 1) as i64) ^ -((z & 1) as i64)
}

pub fn write_varint(buf: &mut Vec<u8>, mut v: u64) {
    loop {
        let b = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            buf.push(b);
            return;
        }
        buf.push(b | 0x80);
    }
}

pub fn read_varint(buf: &[u8], pos: &mut usize) -> Option<u64> {
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    loop {
        let b = *buf.get(*pos)?;
        *pos += 1;
        if shift == 63 {
            // 10th byte: only one payload bit remains in a u64.
            if b > 1 {
                return None;
            }
            result |= (b as u64) << shift;
            return Some(result);
        }
        result |= ((b & 0x7f) as u64) << shift;
        if b & 0x80 == 0 {
            return Some(result);
        }
        shift += 7;
    }
}

// ---------------------------------------------------------------- crc32

/// CRC-32 (IEEE 802.3, polynomial 0xEDB88320), bitwise.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc: u32 = 0xFFFF_FFFF;
    for &byte in data {
        crc ^= byte as u32;
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
        }
    }
    !crc
}

// ---------------------------------------------------------------- streams

fn write_delta_or_escape(out: &mut Vec<u8>, delta: i128, absolute: i64) {
    if delta > i64::MIN as i128 && delta <= i64::MAX as i128 {
        // Note: i64::MIN is excluded on purpose (zigzag(i64::MIN) == ESCAPE).
        write_varint(out, zigzag_encode(delta as i64));
    } else {
        write_varint(out, ESCAPE);
        out.extend_from_slice(&absolute.to_le_bytes());
    }
}

/// Reads one timestamp item. On ESCAPE, reads the absolute timestamp, pushes
/// it and returns the delta recomputed from the previous timestamp.
/// Otherwise decodes a delta-of-delta step, reconstructs and pushes the
/// timestamp, and returns the new running delta. All arithmetic in i128;
/// leaving the i64 range => None (corrupt stream).
fn read_ts_item(
    buf: &[u8],
    pos: &mut usize,
    out: &mut Vec<i64>,
    prev_delta: i128,
) -> Option<i128> {
    let code = read_varint(buf, pos)?;
    let prev = *out.last()?;
    if code == ESCAPE {
        let raw = buf.get(*pos..*pos + 8)?;
        *pos += 8;
        let v = i64::from_le_bytes(raw.try_into().ok()?);
        out.push(v);
        Some(v as i128 - prev as i128)
    } else {
        let dod = zigzag_decode(code) as i128;
        let delta = prev_delta + dod;
        let v = prev as i128 + delta;
        if v < i64::MIN as i128 || v > i64::MAX as i128 {
            return None;
        }
        out.push(v as i64);
        Some(delta)
    }
}

/// Reads one value item. On ESCAPE, reads the absolute value; otherwise the
/// code is the plain delta from the previous value.
fn read_val_item(buf: &[u8], pos: &mut usize, out: &mut Vec<i64>) -> Option<()> {
    let code = read_varint(buf, pos)?;
    let prev = *out.last()?;
    if code == ESCAPE {
        let raw = buf.get(*pos..*pos + 8)?;
        *pos += 8;
        out.push(i64::from_le_bytes(raw.try_into().ok()?));
    } else {
        let v = prev as i128 + zigzag_decode(code) as i128;
        if v < i64::MIN as i128 || v > i64::MAX as i128 {
            return None;
        }
        out.push(v as i64);
    }
    Some(())
}

pub fn encode_timestamps(ts: &[i64]) -> Vec<u8> {
    let mut out = Vec::new();
    if ts.is_empty() {
        return out;
    }
    out.extend_from_slice(&ts[0].to_le_bytes());
    if ts.len() == 1 {
        return out;
    }
    let mut prev_delta: i128 = ts[1] as i128 - ts[0] as i128;
    write_delta_or_escape(&mut out, prev_delta, ts[1]);
    for i in 2..ts.len() {
        let delta = ts[i] as i128 - ts[i - 1] as i128;
        let dod = delta - prev_delta;
        write_delta_or_escape(&mut out, dod, ts[i]);
        prev_delta = delta;
    }
    out
}

pub fn decode_timestamps(buf: &[u8], count: usize) -> Option<Vec<i64>> {
    let (mut out, mut pos) = decode_first(buf, count)?;
    if count >= 2 {
        // second timestamp: the stored step IS the first delta (prev_delta = 0)
        let mut prev_delta = read_ts_item(buf, &mut pos, &mut out, 0)?;
        while out.len() < count {
            prev_delta = read_ts_item(buf, &mut pos, &mut out, prev_delta)?;
        }
    }
    finish(buf, out, pos)
}

pub fn encode_values(vals: &[i64]) -> Vec<u8> {
    let mut out = Vec::new();
    if vals.is_empty() {
        return out;
    }
    out.extend_from_slice(&vals[0].to_le_bytes());
    for i in 1..vals.len() {
        let delta = vals[i] as i128 - vals[i - 1] as i128;
        write_delta_or_escape(&mut out, delta, vals[i]);
    }
    out
}

pub fn decode_values(buf: &[u8], count: usize) -> Option<Vec<i64>> {
    let (mut out, mut pos) = decode_first(buf, count)?;
    while out.len() < count {
        read_val_item(buf, &mut pos, &mut out)?;
    }
    finish(buf, out, pos)
}

fn decode_first(buf: &[u8], count: usize) -> Option<(Vec<i64>, usize)> {
    if count == 0 {
        if buf.is_empty() {
            return Some((Vec::new(), 0));
        }
        return None;
    }
    let first_raw = buf.get(0..8)?;
    let mut out = Vec::with_capacity(count);
    out.push(i64::from_le_bytes(first_raw.try_into().ok()?));
    Some((out, 8))
}

fn finish(buf: &[u8], out: Vec<i64>, pos: usize) -> Option<Vec<i64>> {
    if pos != buf.len() {
        return None; // trailing garbage => corrupt
    }
    Some(out)
}

// ---------------------------------------------------------------- blocks

/// Encodes a full block (header + payloads) for `points`, which must be
/// non-empty and sorted by strictly increasing timestamp (caller ensures).
pub fn encode_block(points: &[(i64, i64)]) -> Vec<u8> {
    debug_assert!(!points.is_empty());
    let ts: Vec<i64> = points.iter().map(|p| p.0).collect();
    let vals: Vec<i64> = points.iter().map(|p| p.1).collect();
    let ts_payload = encode_timestamps(&ts);
    let val_payload = encode_values(&vals);

    let mut crc_input = Vec::with_capacity(ts_payload.len() + val_payload.len());
    crc_input.extend_from_slice(&ts_payload);
    crc_input.extend_from_slice(&val_payload);
    let crc = crc32(&crc_input);

    let mut out = Vec::with_capacity(BLOCK_HEADER_LEN + ts_payload.len() + val_payload.len());
    out.extend_from_slice(&(points.len() as u32).to_le_bytes());
    out.extend_from_slice(&ts[0].to_le_bytes());
    out.extend_from_slice(&ts[ts.len() - 1].to_le_bytes());
    out.extend_from_slice(&vals.iter().min().unwrap().to_le_bytes());
    out.extend_from_slice(&vals.iter().max().unwrap().to_le_bytes());
    out.extend_from_slice(&(ts_payload.len() as u32).to_le_bytes());
    out.extend_from_slice(&(val_payload.len() as u32).to_le_bytes());
    out.extend_from_slice(&crc.to_le_bytes());
    debug_assert_eq!(out.len(), BLOCK_HEADER_LEN);
    out.extend_from_slice(&ts_payload);
    out.extend_from_slice(&val_payload);
    out
}

pub struct BlockHeader {
    pub count: u32,
    pub ts_start: i64,
    pub ts_end: i64,
    pub val_min: i64,
    pub val_max: i64,
    pub ts_len: u32,
    pub val_len: u32,
    pub crc32: u32,
}

pub fn parse_block_header(buf: &[u8]) -> Option<BlockHeader> {
    if buf.len() < BLOCK_HEADER_LEN {
        return None;
    }
    Some(BlockHeader {
        count: u32::from_le_bytes(buf[0..4].try_into().ok()?),
        ts_start: i64::from_le_bytes(buf[4..12].try_into().ok()?),
        ts_end: i64::from_le_bytes(buf[12..20].try_into().ok()?),
        val_min: i64::from_le_bytes(buf[20..28].try_into().ok()?),
        val_max: i64::from_le_bytes(buf[28..36].try_into().ok()?),
        ts_len: u32::from_le_bytes(buf[36..40].try_into().ok()?),
        val_len: u32::from_le_bytes(buf[40..44].try_into().ok()?),
        crc32: u32::from_le_bytes(buf[44..48].try_into().ok()?),
    })
}

/// Decodes a complete block (header + payloads). Verifies CRC and that the
/// decoded point count matches the header. Returns None on any corruption.
pub fn decode_block(block: &[u8]) -> Option<Vec<(i64, i64)>> {
    let h = parse_block_header(block)?;
    let total = BLOCK_HEADER_LEN + h.ts_len as usize + h.val_len as usize;
    if block.len() != total {
        return None;
    }
    let payload = &block[BLOCK_HEADER_LEN..];
    let mut crc_input = Vec::with_capacity(payload.len());
    crc_input.extend_from_slice(payload);
    if crc32(&crc_input) != h.crc32 {
        return None;
    }
    let ts = decode_timestamps(&payload[..h.ts_len as usize], h.count as usize)?;
    let vals = decode_values(&payload[h.ts_len as usize..], h.count as usize)?;
    if ts.len() != vals.len() {
        return None;
    }
    Some(ts.into_iter().zip(vals).collect())
}

pub fn file_header() -> Vec<u8> {
    let mut v = Vec::with_capacity(FILE_HEADER_LEN);
    v.extend_from_slice(FILE_MAGIC);
    v.extend_from_slice(&FILE_VERSION.to_le_bytes());
    v.extend_from_slice(&0u16.to_le_bytes()); // reserved
    v
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(points: &[(i64, i64)]) {
        let block = encode_block(points);
        let decoded = decode_block(&block).expect("decode");
        assert_eq!(decoded, points);
    }

    #[test]
    fn roundtrip_regular() {
        let pts: Vec<(i64, i64)> = (0..200).map(|i| (1_700_000_000 + i, i * 3 - 100)).collect();
        roundtrip(&pts);
    }

    #[test]
    fn roundtrip_single_point() {
        roundtrip(&[(42, -7)]);
    }

    #[test]
    fn roundtrip_extremes() {
        let pts = vec![
            (0, i64::MIN),
            (1, i64::MAX),
            (2, 0),
            (i64::MAX - 1, -1),
            (i64::MAX, i64::MIN),
        ];
        roundtrip(&pts);
    }

    #[test]
    fn roundtrip_timestamp_jump_overflow() {
        // delta 0 -> i64::MAX overflows i64 delta range => escape path
        let pts = vec![(0, 0), (i64::MAX, 1), (i64::MAX - 1, 2)];
        // not sorted strictly? i64::MAX-1 < i64::MAX, so reorder:
        let pts = vec![(pts[0].0, pts[0].1), (pts[2].0, pts[2].1), (pts[1].0, pts[1].1)];
        roundtrip(&pts);
    }

    #[test]
    fn roundtrip_min_delta_escape() {
        // delta == i64::MIN must take the escape path (zigzag collision)
        let pts = vec![(i64::MAX, 0), (i64::MAX - 1, i64::MIN), (i64::MAX - 2, i64::MAX)];
        roundtrip(&pts);
    }

    #[test]
    fn corrupt_crc_rejected() {
        let pts: Vec<(i64, i64)> = (0..10).map(|i| (i, i * i)).collect();
        let mut block = encode_block(&pts);
        let last = block.len() - 1;
        block[last] ^= 0xFF;
        assert!(decode_block(&block).is_none());
    }

    #[test]
    fn truncated_block_rejected() {
        let pts: Vec<(i64, i64)> = (0..10).map(|i| (i, i)).collect();
        let block = encode_block(&pts);
        assert!(decode_block(&block[..block.len() - 3]).is_none());
    }

    #[test]
    fn crc32_known_vector() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }
}
