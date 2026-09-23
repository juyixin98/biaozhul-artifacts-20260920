//! On-disk formats. Everything little-endian; all checksums are CRC-32
//! (see [`crate::crc32`]).
//!
//! # File layout
//!
//! A repository is a directory containing three files:
//!
//! | file       | role                                                        |
//! |------------|-------------------------------------------------------------|
//! | `data.log` | append-only record log                                     |
//! | `sb.a`     | superblock slot A, exactly [`SB_SIZE`] bytes when present  |
//! | `sb.b`     | superblock slot B, exactly [`SB_SIZE`] bytes when present  |
//!
//! # Superblock (4096 bytes, one fixed page)
//!
//! ```text
//! offset  size  field
//! 0       4     magic = b"DSB1"
//! 4       4     format version (u32, currently 1)
//! 8       8     generation (u64, starts at 1, +1 per commit)
//! 16      8     data_len: logical high-water mark of data.log (u64)
//! 24      8     root_off: offset of the head record in data.log (u64)
//! 32      4     slot: 0 (sb.a) or 1 (sb.b)
//! 36      4036  reserved, must be zero
//! 4072    4     CRC-32 over bytes [0, 4072) (u32)
//! 4076    20    trailing reserved, must be zero
//! ```
//!
//! A half-page (torn) write therefore leaves the CRC wrong, or the tail of
//! the page as zero/garbage: the block is rejected wholesale.
//!
//! # Data record
//!
//! ```text
//! offset  size  field
//! 0       4     magic = b"RECD"
//! 4       4     payload length L (u32)
//! 8       4     prev_off: absolute offset of the previous record,
//! |             | NIL_PREV (u32::MAX) if this is the first record (u32)
//! 12      8     generation of the commit that appended it (u64)
//! 20      8     record_off: absolute offset of this record in data.log (u64)
//! 28      8     reserved, must be zero
//! 36      4     CRC-32 over bytes [0, 36) (u32)
//! 40      L     payload
//! ```
//!
//! Record size is `40 + L` bytes. Records are packed contiguously starting at
//! offset 0, so `data_len` is exactly the end offset of the last valid
//! record. A separate NIL sentinel (rather than 0) is used for the first
//! record's back-pointer because 0 is a valid record offset.
//!
//! # Payload
//!
//! ```text
//! 0       1     op: 1 = put, 2 = delete
//! 1       4     key length K (u32)
//! 5       4     value length V (u32, delete: 0)
//! 9       K     key bytes (UTF-8)
//! 9+K     V     value bytes (put only)
//! ```
//!
//! Fixed header is therefore 9 bytes; a put payload is `9 + K + V` bytes.

use crate::crc32;

/// Fixed superblock page size.
pub const SB_SIZE: usize = 4096;
pub const SB_MAGIC: [u8; 4] = *b"DSB1";
pub const SB_VERSION: u32 = 1;
const SB_CRC_OFFSET: usize = SB_SIZE - 24; // 4072: CRC covers [0, 4072)

pub const DATA_FILE: &str = "data.log";
pub const SB_FILE: [&str; 2] = ["sb.a", "sb.b"];

pub const REC_MAGIC: [u8; 4] = *b"RECD";
pub const REC_HEADER_LEN: usize = 40;

/// Back-pointer sentinel: this record is the first (oldest) in the chain.
pub const NIL_PREV: u32 = u32::MAX;

pub const OP_PUT: u8 = 1;
pub const OP_DELETE: u8 = 2;

/// Errors detected while decoding/validating on-disk bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FormatError {
    TooShort,
    BadMagic,
    BadVersion,
    CrcMismatch,
    BadSlot,
    NonZeroReserved,
    OutOfBounds,
    BadPayload,
}

impl std::fmt::Display for FormatError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        std::fmt::Debug::fmt(self, f)
    }
}
impl std::error::Error for FormatError {}

/// Parsed superblock.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Superblock {
    pub generation: u64,
    pub data_len: u64,
    pub root_off: u64,
    pub slot: u32,
}

impl Superblock {
    pub fn encode(&self) -> [u8; SB_SIZE] {
        let mut buf = [0u8; SB_SIZE];
        buf[0..4].copy_from_slice(&SB_MAGIC);
        buf[4..8].copy_from_slice(&SB_VERSION.to_le_bytes());
        buf[8..16].copy_from_slice(&self.generation.to_le_bytes());
        buf[16..24].copy_from_slice(&self.data_len.to_le_bytes());
        buf[24..32].copy_from_slice(&self.root_off.to_le_bytes());
        buf[32..36].copy_from_slice(&self.slot.to_le_bytes());
        let crc = crc32::checksum(&buf[..SB_CRC_OFFSET]);
        buf[SB_CRC_OFFSET..SB_CRC_OFFSET + 4].copy_from_slice(&crc.to_le_bytes());
        buf
    }

    /// Validate raw page bytes. `file_len` is the actual length of the data
    /// file, used to reject out-of-bounds pointers.
    pub fn decode(buf: &[u8], file_len: u64) -> Result<Superblock, FormatError> {
        if buf.len() != SB_SIZE {
            return Err(FormatError::TooShort);
        }
        if buf[0..4] != SB_MAGIC {
            return Err(FormatError::BadMagic);
        }
        let version = u32::from_le_bytes(buf[4..8].try_into().unwrap());
        if version != SB_VERSION {
            return Err(FormatError::BadVersion);
        }
        let stored_crc = u32::from_le_bytes(buf[SB_CRC_OFFSET..SB_CRC_OFFSET + 4].try_into().unwrap());
        if crc32::checksum(&buf[..SB_CRC_OFFSET]) != stored_crc {
            return Err(FormatError::CrcMismatch);
        }
        // Reserved regions must be zero: catches garbage written where no
        // superblock ever existed.
        if buf[36..SB_CRC_OFFSET].iter().any(|&b| b != 0)
            || buf[SB_CRC_OFFSET + 4..].iter().any(|&b| b != 0)
        {
            return Err(FormatError::NonZeroReserved);
        }
        let sb = Superblock {
            generation: u64::from_le_bytes(buf[8..16].try_into().unwrap()),
            data_len: u64::from_le_bytes(buf[16..24].try_into().unwrap()),
            root_off: u64::from_le_bytes(buf[24..32].try_into().unwrap()),
            slot: u32::from_le_bytes(buf[32..36].try_into().unwrap()),
        };
        if sb.slot > 1 {
            return Err(FormatError::BadSlot);
        }
        if sb.generation == 0 {
            return Err(FormatError::BadVersion);
        }
        // data_len must describe a prefix of the actual file, and the root
        // pointer must lie inside that prefix.
        if sb.data_len > file_len {
            return Err(FormatError::OutOfBounds);
        }
        if sb.root_off >= sb.data_len {
            return Err(FormatError::OutOfBounds);
        }
        Ok(sb)
    }
}

/// Parsed record header (payload bytes are borrowed from the data file).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordHeader {
    pub payload_len: u32,
    pub prev_off: u32,
    pub generation: u64,
    pub record_off: u64,
}

impl RecordHeader {
    pub fn total_len(payload_len: u32) -> u64 {
        REC_HEADER_LEN as u64 + payload_len as u64
    }

    /// Parse the record at absolute offset `offset` within a data buffer of
    /// logical length `data_len`. Every cross-record and self-consistency
    /// check that does not require following the chain lives here.
    pub fn parse_at(
        buf: &[u8],
        data_len: u64,
        offset: u64,
    ) -> Result<(RecordHeader, &[u8]), FormatError> {
        let off = offset as usize;
        if off + REC_HEADER_LEN > data_len as usize {
            return Err(FormatError::OutOfBounds);
        }
        let h = &buf[off..off + REC_HEADER_LEN];
        if h[0..4] != REC_MAGIC {
            return Err(FormatError::BadMagic);
        }
        let stored_crc = u32::from_le_bytes(h[36..40].try_into().unwrap());
        if crc32::checksum(&h[..36]) != stored_crc {
            return Err(FormatError::CrcMismatch);
        }
        if h[28..36].iter().any(|&b| b != 0) {
            return Err(FormatError::NonZeroReserved);
        }
        let payload_len = u32::from_le_bytes(h[4..8].try_into().unwrap());
        let prev_off = u32::from_le_bytes(h[8..12].try_into().unwrap());
        let generation = u64::from_le_bytes(h[12..20].try_into().unwrap());
        let record_off = u64::from_le_bytes(h[20..28].try_into().unwrap());
        let end = (off as u64)
            .checked_add(Self::total_len(payload_len))
            .ok_or(FormatError::OutOfBounds)?;
        if end > data_len {
            return Err(FormatError::OutOfBounds);
        }
        if record_off != offset {
            return Err(FormatError::OutOfBounds);
        }
        if generation == 0 {
            return Err(FormatError::BadVersion);
        }
        // Back-pointer must strictly precede this record (the chain points
        // backwards), unless this record carries the NIL sentinel. A pointer
        // to offset 0 is normal for the second record, which links to the
        // first (packed) record.
        if prev_off != NIL_PREV && prev_off as u64 >= offset {
            return Err(FormatError::OutOfBounds);
        }
        let payload = &buf[off + REC_HEADER_LEN..end as usize];
        Ok((
            RecordHeader {
                payload_len,
                prev_off,
                generation,
                record_off,
            },
            payload,
        ))
    }

    pub fn encode(prev_off: u32, generation: u64, record_off: u64, payload: &[u8]) -> Vec<u8> {
        let mut buf = Vec::with_capacity(REC_HEADER_LEN + payload.len());
        buf.extend_from_slice(&REC_MAGIC);
        buf.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        buf.extend_from_slice(&prev_off.to_le_bytes());
        buf.extend_from_slice(&generation.to_le_bytes());
        buf.extend_from_slice(&record_off.to_le_bytes());
        buf.extend_from_slice(&0u64.to_le_bytes()); // reserved
        let crc = crc32::checksum(&buf[..36]);
        buf.extend_from_slice(&crc.to_le_bytes());
        buf.extend_from_slice(payload);
        buf
    }
}

/// Decoded record payload.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    Put(Vec<u8>, Vec<u8>),
    Delete(Vec<u8>),
}

impl Op {
    pub fn encode_put(key: &[u8], value: &[u8]) -> Vec<u8> {
        let mut p = Vec::with_capacity(9 + key.len() + value.len());
        p.push(OP_PUT);
        p.extend_from_slice(&(key.len() as u32).to_le_bytes());
        p.extend_from_slice(&(value.len() as u32).to_le_bytes());
        p.extend_from_slice(key);
        p.extend_from_slice(value);
        p
    }

    pub fn encode_delete(key: &[u8]) -> Vec<u8> {
        let mut p = Vec::with_capacity(9 + key.len());
        p.push(OP_DELETE);
        p.extend_from_slice(&(key.len() as u32).to_le_bytes());
        p.extend_from_slice(&0u32.to_le_bytes());
        p.extend_from_slice(key);
        p
    }

    pub fn decode(payload: &[u8]) -> Result<Op, FormatError> {
        let need = |n: usize| {
            if payload.len() >= n {
                Ok(())
            } else {
                Err(FormatError::BadPayload)
            }
        };
        need(9)?;
        let op = payload[0];
        let klen = u32::from_le_bytes(payload[1..5].try_into().unwrap()) as usize;
        let vlen = u32::from_le_bytes(payload[5..9].try_into().unwrap()) as usize;
        match op {
            OP_PUT => {
                if 9usize
                    .checked_add(klen)
                    .and_then(|x| x.checked_add(vlen))
                    != Some(payload.len())
                {
                    return Err(FormatError::BadPayload);
                }
                let key = payload[9..9 + klen].to_vec();
                let value = payload[9 + klen..9 + klen + vlen].to_vec();
                Ok(Op::Put(key, value))
            }
            OP_DELETE => {
                if vlen != 0 || 9usize.checked_add(klen) != Some(payload.len()) {
                    return Err(FormatError::BadPayload);
                }
                Ok(Op::Delete(payload[9..9 + klen].to_vec()))
            }
            _ => Err(FormatError::BadPayload),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn superblock_roundtrip() {
        let sb = Superblock {
            generation: 7,
            data_len: 1234,
            root_off: 1200,
            slot: 1,
        };
        let page = sb.encode();
        assert_eq!(page.len(), SB_SIZE);
        let decoded = Superblock::decode(&page, 1234).unwrap();
        assert_eq!(decoded, sb);
        // trailing file space beyond data_len is fine
        assert!(Superblock::decode(&page, 5000).is_ok());
        // data_len past end of file: rejected
        assert_eq!(
            Superblock::decode(&page, 1233),
            Err(FormatError::OutOfBounds)
        );
    }

    #[test]
    fn superblock_detects_torn_page() {
        let sb = Superblock {
            generation: 3,
            data_len: 80,
            root_off: 40,
            slot: 0,
        };
        let mut page = sb.encode();
        // simulate a half-page write: first half contains the new page,
        // second half is zeroed.
        for b in &mut page[SB_SIZE / 2..] {
            *b = 0;
        }
        assert_eq!(
            Superblock::decode(&page, 80),
            Err(FormatError::CrcMismatch)
        );
        // single-bit corruption anywhere is detected
        let mut page = sb.encode();
        page[100] ^= 0x01;
        assert!(Superblock::decode(&page, 80).is_err());
    }

    #[test]
    fn record_roundtrip() {
        let payload = Op::encode_put(b"k", b"v");
        let rec = RecordHeader::encode(NIL_PREV, 1, 0, &payload);
        assert_eq!(rec.len(), 40 + payload.len());
        let (h, p) = RecordHeader::parse_at(&rec, rec.len() as u64, 0).unwrap();
        assert_eq!(h.generation, 1);
        assert_eq!(h.record_off, 0);
        assert_eq!(Op::decode(p).unwrap(), Op::Put(b"k".to_vec(), b"v".to_vec()));
    }
}
