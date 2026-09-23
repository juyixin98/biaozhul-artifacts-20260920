//! On-disk format constants, block handles and the footer.
//!
//! # File layout (little-endian throughout)
//!
//! ```text
//! +-------------------+
//! | data block 0      |  <payload bytes> + 5-byte trailer
//! +-------------------+
//! | data block 1      |
//! +-------------------+
//! | ...               |
//! +-------------------+
//! | data block n-1    |
//! +-------------------+
//! | metaindex block   |  always present; an empty block in this version
//! +-------------------+
//! | index block       |  one entry per data block
//! +-------------------+
//! | footer            |  fixed 48 bytes
//! +-------------------+
//! ```
//!
//! # Block trailer (5 bytes, immediately after each block payload)
//!
//! ```text
//! +----------------------+----------------------------+
//! | type        u8       | checksum         u32 LE    |
//! +----------------------+----------------------------+
//! ```
//!
//! `type` is [`BLOCK_TYPE_DATA`] (`1`) for every block in this version; the
//! checksum is the *masked* CRC32C over `type || payload`.
//!
//! # Block payload
//!
//! Entries are concatenated; the payload ends with the restart array:
//!
//! ```text
//! +---------------------------------------------------+
//! | entry 0 | entry 1 | ... | restart[0] ... restart[k-1] | num_restarts u32 |
//! +---------------------------------------------------+
//! ```
//!
//! Each entry is:
//!
//! ```text
//! shared_len varint32 | unshared_len varint32 | value_len varint32
//! | key_delta [unshared_len] | value [value_len]
//! ```
//!
//! Every `restart_interval` entries (and always for entry 0) `shared_len` is
//! zero and the key delta is the full key; the offset of such an entry is
//! appended to the restart array. Sparse per-block keys (index block) are built
//! with `restart_interval == 1`.
//!
//! # Block handle (varint)
//!
//! ```text
//! offset varint64 | size varint64
//! ```
//! where `size` is the payload size (the 5-byte trailer is not included).
//!
//! # Footer (fixed 48 bytes)
//!
//! ```text
//! +-----------------------------------------------------------+
//! | metaindex_handle varint64+varint64                        |
//! | index_handle     varint64+varint64                        |
//! | zero padding                                              |
//! | magic            u64 LE  = 0x2025c1c3b050878e            |
//! +-----------------------------------------------------------+
//! ```
//!
//! The two handles plus padding occupy exactly 40 bytes, followed by the
//! 8-byte magic (mirrors the LevelDB footer framing).

use crate::coding;
use crate::error::{Error, Result};

/// Trailer block type: raw (uncompressed) data.
pub const BLOCK_TYPE_DATA: u8 = 1;

/// Bytes appended to every block payload.
pub const BLOCK_TRAILER_LEN: usize = 5;

/// Fixed footer size.
pub const FOOTER_LEN: usize = 48;

/// Bytes available to the two varint handles inside the footer.
pub const FOOTER_HANDLE_SPACE: usize = 40;

/// File magic, little-endian on disk: `PSST\x01\x00!%`.
pub const TABLE_MAGIC: u64 = 0x2025_c1c3_b050_878e;

/// Maximum encoded block payload size accepted by the reader (4 MiB).
pub const MAX_BLOCK_SIZE: usize = 4 * 1024 * 1024;

/// A reference to a block inside the table file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlockHandle {
    pub offset: u64,
    /// Payload length, excluding the trailer.
    pub size: u64,
}

impl BlockHandle {
    pub fn new(offset: u64, size: u64) -> Self {
        BlockHandle { offset, size }
    }

    pub fn encode_to(&self, out: &mut Vec<u8>) {
        coding::put_varint64(out, self.offset);
        coding::put_varint64(out, self.size);
    }

    pub fn decode(buf: &[u8]) -> Result<(BlockHandle, usize)> {
        let (offset, n1) = coding::decode_varint64(buf, 0)?;
        let (size, n2) = coding::decode_varint64(buf, n1)?;
        Ok((BlockHandle { offset, size }, n1 + n2))
    }

    /// Offset of the first byte past the block (payload + trailer).
    pub fn end_exclusive(&self) -> Result<u64> {
        self.offset
            .checked_add(self.size)
            .and_then(|v| v.checked_add(BLOCK_TRAILER_LEN as u64))
            .ok_or_else(|| Error::corruption("block handle overflow"))
    }
}

/// The fixed-size footer locating the metaindex and index blocks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Footer {
    pub metaindex: BlockHandle,
    pub index: BlockHandle,
}

impl Footer {
    pub fn new(metaindex: BlockHandle, index: BlockHandle) -> Self {
        Footer { metaindex, index }
    }

    pub fn encode_to(&self, out: &mut Vec<u8>) -> Result<()> {
        let start = out.len();
        self.metaindex.encode_to(out);
        self.index.encode_to(out);
        if out.len() - start > FOOTER_HANDLE_SPACE {
            return Err(Error::invalid_argument("handles do not fit in footer"));
        }
        out.resize(start + FOOTER_HANDLE_SPACE, 0);
        coding::put_u64_le(out, TABLE_MAGIC);
        debug_assert_eq!(out.len() - start, FOOTER_LEN);
        Ok(())
    }

    pub fn decode(buf: &[u8]) -> Result<Footer> {
        if buf.len() < FOOTER_LEN {
            return Err(Error::corruption("footer shorter than 48 bytes"));
        }
        if coding::decode_u64_le(&buf[FOOTER_HANDLE_SPACE..]) != TABLE_MAGIC {
            return Err(Error::corruption("bad table magic in footer"));
        }
        let (metaindex, n1) = BlockHandle::decode(buf)?;
        let (index, _n2) = BlockHandle::decode(&buf[n1..])?;
        Ok(Footer { metaindex, index })
    }
}

/// Append a block payload plus its 5-byte trailer. Returns the emitted
/// [`BlockHandle`].
pub fn write_block(out: &mut Vec<u8>, block_type: u8, payload: &[u8]) -> BlockHandle {
    let offset = out.len() as u64;
    let size = payload.len() as u64;
    out.extend_from_slice(payload);
    let mut crc_input = Vec::with_capacity(1 + payload.len());
    crc_input.push(block_type);
    crc_input.extend_from_slice(payload);
    out.push(block_type);
    coding::put_u32_le(out, coding::mask_crc(coding::crc32c(&crc_input)));
    BlockHandle::new(offset, size)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn footer_roundtrip() {
        let f = Footer::new(BlockHandle::new(0, 4096), BlockHandle::new(12_345, 99));
        let mut buf = Vec::new();
        f.encode_to(&mut buf).unwrap();
        assert_eq!(buf.len(), FOOTER_LEN);
        assert_eq!(Footer::decode(&buf).unwrap(), f);
    }

    #[test]
    fn footer_rejects_bad_magic() {
        let f = Footer::new(BlockHandle::new(0, 1), BlockHandle::new(6, 1));
        let mut buf = Vec::new();
        f.encode_to(&mut buf).unwrap();
        buf[FOOTER_LEN - 1] ^= 0xff;
        assert!(Footer::decode(&buf).is_err());
    }

    #[test]
    fn handle_varint_roundtrip() {
        let h = BlockHandle::new(1 << 40, (1 << 20) + 7);
        let mut buf = Vec::new();
        h.encode_to(&mut buf);
        let (got, n) = BlockHandle::decode(&buf).unwrap();
        assert_eq!(got, h);
        assert_eq!(n, buf.len());
    }
}
