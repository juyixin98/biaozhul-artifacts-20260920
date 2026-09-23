//! Read-only blocks: in-block prefix compression, restart points and the
//! sparse per-block cursor used by both data and index blocks.
//!
//! See the [`crate::format`] module documentation for the byte-level layout.

use crate::coding;
use crate::error::{Error, Result};

// ---------------------------------------------------------------------------
// Write side
// ---------------------------------------------------------------------------

/// Builds one data or index block in memory.
pub struct BlockBuilder {
    buf: Vec<u8>,
    restarts: Vec<u32>,
    last_key: Vec<u8>,
    /// Number of entries already emitted.
    num_entries: u32,
    restart_interval: u32,
}

impl BlockBuilder {
    /// `restart_interval` is the number of entries between restart points;
    /// every entry at an index divisible by it stores a full (uncompressed)
    /// key. Must be at least 1; index blocks use 1.
    pub fn new(restart_interval: u32) -> Result<BlockBuilder> {
        if restart_interval == 0 {
            return Err(Error::invalid_argument("restart_interval must be >= 1"));
        }
        Ok(BlockBuilder {
            buf: Vec::new(),
            restarts: Vec::new(),
            last_key: Vec::new(),
            num_entries: 0,
            restart_interval,
        })
    }

    pub fn reset(&mut self) {
        self.buf.clear();
        self.restarts.clear();
        self.last_key.clear();
        self.num_entries = 0;
    }

    pub fn is_empty(&self) -> bool {
        self.restarts.is_empty()
    }

    /// Current encoded size including the restart array that will be appended
    /// and a conservative allowance for per-entry varint headers (each header
    /// has three varints; 15 bytes is the worst case when lengths are large).
    pub fn estimated_size(&self) -> usize {
        self.buf.len() + self.restarts.len() * 4 + 4
    }

    /// Append a key/value pair. Keys MUST be strictly increasing; the builder
    /// itself does not check (the table layer does).
    pub fn add(&mut self, key: &[u8], value: &[u8]) {
        let restart = self.num_entries.is_multiple_of(self.restart_interval);
        let shared = if restart {
            self.restarts.push(self.buf.len() as u32);
            0usize
        } else {
            common_prefix(&self.last_key, key)
        };

        // Strictly increasing keys guarantee shared < key.len() on a
        // compressed entry (identical keys are rejected upstream); restart
        // entries simply ignore the computed shared length.
        debug_assert!(restart || shared < key.len());

        let non_shared = key.len() - shared;
        coding::put_varint32(&mut self.buf, shared as u32);
        coding::put_varint32(&mut self.buf, non_shared as u32);
        coding::put_varint32(&mut self.buf, value.len() as u32);
        self.buf.extend_from_slice(&key[shared..]);
        self.buf.extend_from_slice(value);

        self.last_key.truncate(shared);
        self.last_key.extend_from_slice(&key[shared..]);
        self.num_entries += 1;
    }

    /// Append the restart array and return the finished payload. Call
    /// [`BlockBuilder::reset`] before reusing the builder afterwards.
    pub fn finish(&mut self) -> &[u8] {
        for r in &self.restarts {
            coding::put_u32_le(&mut self.buf, *r);
        }
        coding::put_u32_le(&mut self.buf, self.restarts.len() as u32);
        &self.buf
    }
}

/// Length of the longest common prefix of `a` and `b`.
pub fn common_prefix(a: &[u8], b: &[u8]) -> usize {
    let mut i = 0;
    let max = a.len().min(b.len());
    while i < max && a[i] == b[i] {
        i += 1;
    }
    i
}

// ---------------------------------------------------------------------------
// Read side: parsed block
// ---------------------------------------------------------------------------

/// A parsed, immutable block payload (trailer already stripped and verified).
#[derive(Clone)]
pub struct Block {
    data: Vec<u8>,
    restart_offset: usize,
    num_restarts: u32,
}

/// Sanity ceiling on restart points in a single block (16M).
const MAX_RESTARTS: u32 = 1 << 24;

impl Block {
    /// Parse a payload and structurally validate the restart array.
    pub fn parse(data: Vec<u8>) -> Result<Block> {
        if data.len() < 4 {
            return Err(Error::corruption(format!(
                "block payload too short for restart count: {} bytes",
                data.len()
            )));
        }
        let num_restarts = coding::decode_u32_le(&data[data.len() - 4..]);
        let array_bytes = (num_restarts as u64)
            .checked_mul(4)
            .ok_or_else(|| Error::corruption("restart count overflow"))?
            as usize;
        if array_bytes + 4 > data.len() {
            return Err(Error::corruption("restart array overruns block"));
        }
        let restart_offset = data.len() - 4 - array_bytes;

        // An empty block is exactly the 4-byte restart count of zero.
        if num_restarts == 0 {
            if data.len() != 4 {
                return Err(Error::corruption(
                    "zero restart points but payload carries extra bytes",
                ));
            }
            return Ok(Block {
                data,
                restart_offset: 0,
                num_restarts: 0,
            });
        }
        if num_restarts > MAX_RESTARTS {
            return Err(Error::corruption("restart count absurdly large"));
        }

        let mut prev: Option<u32> = None;
        for i in 0..num_restarts as usize {
            let off = coding::decode_u32_le(&data[restart_offset + i * 4..]);
            if (off as usize) >= restart_offset {
                return Err(Error::corruption(format!(
                    "restart[{i}] offset {off} points at/after restart array"
                )));
            }
            match prev {
                None => {
                    if off != 0 {
                        return Err(Error::corruption("first restart point must be at offset 0"));
                    }
                }
                Some(p) if off <= p => {
                    return Err(Error::corruption(
                        "restart offsets must be strictly increasing",
                    ));
                }
                Some(_) => {}
            }
            prev = Some(off);
        }

        Ok(Block {
            data,
            restart_offset,
            num_restarts,
        })
    }

    pub fn num_restarts(&self) -> usize {
        self.num_restarts as usize
    }

    pub fn restart_offset(&self) -> usize {
        self.restart_offset
    }

    pub fn payload(&self) -> &[u8] {
        &self.data
    }

    /// Offset recorded by restart point `i`.
    pub fn restart_point(&self, i: usize) -> usize {
        coding::decode_u32_le(&self.data[self.restart_offset + i * 4..]) as usize
    }

    /// Decode the entry header at absolute payload offset `pos`.
    pub fn entry_at(&self, pos: usize) -> Result<EntryHeader> {
        EntryHeader::decode(&self.data, pos, self.restart_offset)
    }

    /// Cursor positioned at the first entry (invalid for empty blocks).
    pub fn first(&self) -> Result<Cursor<'_>> {
        let mut c = Cursor::new(self, 0);
        if self.num_restarts != 0 {
            c.next()?;
        }
        Ok(c)
    }

    /// Position a cursor at the first entry whose key is `>= target`.
    /// The returned cursor is invalid iff every key is smaller (or empty).
    pub fn seek(&self, target: &[u8]) -> Result<Cursor<'_>> {
        if self.num_restarts == 0 {
            return Ok(Cursor::new(self, self.restart_offset));
        }
        // Binary search across restart points: find the last restart whose full
        // key is strictly less than `target`.
        let mut lo: i64 = 0;
        let mut hi: i64 = self.num_restarts as i64 - 1;
        while lo < hi {
            let mid = (lo + hi + 1) / 2;
            let key = self.restart_key(mid as usize)?;
            if key < target {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }

        let mut cur = Cursor::new(self, self.restart_point(lo as usize));
        while cur.next()? {
            if cur.key() >= target {
                return Ok(cur);
            }
        }
        Ok(cur)
    }

    /// Full key bytes of restart entry `i` (restart entries store shared=0).
    fn restart_key(&self, i: usize) -> Result<&[u8]> {
        let off = self.restart_point(i);
        let h = self.entry_at(off)?;
        if h.shared != 0 {
            return Err(Error::corruption(
                "restart entry has non-zero shared length",
            ));
        }
        Ok(&self.data[h.key_delta_offset..h.key_delta_offset + h.non_shared as usize])
    }
}

/// Decoded header/locations of one block entry.
#[derive(Debug)]
pub struct EntryHeader {
    pub shared: u32,
    pub non_shared: u32,
    pub value_len: u32,
    pub key_delta_offset: usize,
    pub value_offset: usize,
    pub next_offset: usize,
}

impl EntryHeader {
    fn decode(data: &[u8], pos: usize, end: usize) -> Result<EntryHeader> {
        let (shared, n1) = coding::decode_varint32(data, pos)?;
        let (non_shared, n2) = coding::decode_varint32(data, pos + n1)?;
        let (value_len, n3) = coding::decode_varint32(data, pos + n1 + n2)?;
        let key_delta_offset = pos + n1 + n2 + n3;
        let value_offset = key_delta_offset
            .checked_add(non_shared as usize)
            .ok_or_else(|| Error::corruption("entry key length overflow"))?;
        let next_offset = value_offset
            .checked_add(value_len as usize)
            .ok_or_else(|| Error::corruption("entry value length overflow"))?;
        if next_offset > end {
            return Err(Error::corruption("entry overruns into restart array"));
        }
        Ok(EntryHeader {
            shared,
            non_shared,
            value_len,
            key_delta_offset,
            value_offset,
            next_offset,
        })
    }
}

// ---------------------------------------------------------------------------
// Cursor
// ---------------------------------------------------------------------------

/// Forward cursor over a [`Block`]. The reconstructed key is owned here; values
/// borrow the block bytes.
pub struct Cursor<'a> {
    block: &'a Block,
    /// Offset of the next entry to decode.
    pos: usize,
    /// Offset of the current entry.
    entry_offset: usize,
    valid: bool,
    key: Vec<u8>,
}

impl<'a> Cursor<'a> {
    fn new(block: &'a Block, start: usize) -> Cursor<'a> {
        Cursor {
            block,
            pos: start,
            entry_offset: start,
            valid: false,
            key: Vec::new(),
        }
    }

    /// Restart a scan at restart point `index` (clears the prefix state).
    #[allow(dead_code)]
    fn restart_at(&mut self, index: usize) {
        self.key.clear();
        self.pos = self.block.restart_point(index);
        self.entry_offset = self.pos;
        self.valid = false;
    }

    /// Advance to the next entry; returns `false` at end of block.
    ///
    /// Deliberately not `Iterator::next`: advancing is fallible (re-decoding
    /// bytes can surface corruption), which the std trait cannot express.
    #[allow(clippy::should_implement_trait)]
    pub fn next(&mut self) -> Result<bool> {
        if self.pos >= self.block.restart_offset {
            self.valid = false;
            return Ok(false);
        }
        self.entry_offset = self.pos;
        let h = self.block.entry_at(self.pos)?;
        if h.shared as usize > self.key.len() {
            return Err(Error::corruption(format!(
                "entry at {} shares {} bytes but reconstructed key is {} bytes",
                self.pos,
                h.shared,
                self.key.len()
            )));
        }
        self.key.truncate(h.shared as usize);
        self.key.extend_from_slice(
            &self.block.data[h.key_delta_offset..h.key_delta_offset + h.non_shared as usize],
        );
        self.pos = h.next_offset;
        self.valid = true;
        Ok(true)
    }

    pub fn valid(&self) -> bool {
        self.valid
    }

    pub fn key(&self) -> &[u8] {
        &self.key
    }

    /// Value bytes of the current entry (borrowed from the block).
    pub fn value(&self) -> Result<&'a [u8]> {
        let h = self.block.entry_at(self.entry_offset)?;
        Ok(&self.block.data[h.value_offset..h.value_offset + h.value_len as usize])
    }

    /// Payload offset of the current entry (crate-internal scan bookkeeping).
    pub(crate) fn current_entry_offset(&self) -> usize {
        self.entry_offset
    }
}

// ---------------------------------------------------------------------------
// Deep structural validation (used by Table::validate and corruption tests)
// ---------------------------------------------------------------------------

/// Entry statistics gathered during a full block walk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockStats {
    pub entries: usize,
    pub restarts: usize,
}

/// Walk every entry of `block`, reconstruct all keys and enforce ordering and
/// restart discipline. A well-formed non-empty block has `entries >= restarts`.
pub fn validate_entries(block: &Block) -> Result<BlockStats> {
    let mut cur = Cursor::new(block, 0);
    let mut prev_key: Option<Vec<u8>> = None;
    let mut entries = 0usize;
    let mut restart_index = 0usize;

    // Replay positions, tracking where restart points should land.
    let mut pos = 0usize;
    while pos < block.restart_offset() {
        let h = block.entry_at(pos)?;

        // Whether this entry is a restart point depends on its position.
        let should_be_restart = if restart_index < block.num_restarts() {
            block.restart_point(restart_index) == pos
        } else {
            false
        };
        if should_be_restart && h.shared != 0 {
            return Err(Error::corruption(format!(
                "entry at {pos} is recorded as a restart point but shares {} bytes",
                h.shared
            )));
        }
        if should_be_restart {
            restart_index += 1;
        } else if h.shared == 0 {
            // A non-restart entry may still legitimately share 0 bytes (e.g.
            // short keys with no common prefix); that is allowed.
        }

        // Reconstruct via cursor machinery by advancing it.
        let advanced = cur.next()?;
        debug_assert!(advanced);
        if let Some(prev) = &prev_key {
            if prev.as_slice() >= cur.key() {
                return Err(Error::corruption(format!(
                    "block keys not strictly increasing at entry {entries}"
                )));
            }
        }
        prev_key = Some(cur.key().to_vec());
        entries += 1;
        pos = h.next_offset;
    }

    if restart_index != block.num_restarts() {
        return Err(Error::corruption(format!(
            "restart points consumed ({restart_index}) != recorded ({})",
            block.num_restarts()
        )));
    }
    if entries < block.num_restarts() {
        return Err(Error::corruption("fewer entries than restart points"));
    }
    Ok(BlockStats {
        entries,
        restarts: block.num_restarts(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build_entries(interval: u32, kv: &[(&[u8], &[u8])]) -> Block {
        let mut b = BlockBuilder::new(interval).unwrap();
        for (k, v) in kv {
            b.add(k, v);
        }
        let payload = b.finish().to_vec();
        Block::parse(payload).unwrap()
    }

    #[test]
    fn roundtrip_basic() {
        let keys: Vec<Vec<u8>> = vec![b"apple".to_vec(), b"apricot".to_vec(), b"banana".to_vec()];
        let block = build_entries(
            2,
            &[(b"apple", b"1"), (b"apricot", b"22"), (b"banana", b"333")],
        );
        let mut cur = block.first().unwrap();
        for (i, k) in keys.iter().enumerate() {
            assert!(cur.valid());
            assert_eq!(cur.key(), k);
            assert_eq!(cur.value().unwrap().len(), i + 1);
            cur.next().unwrap();
        }
        assert!(!cur.valid());
    }

    #[test]
    fn seek_across_restart_points() {
        let block = build_entries(
            2,
            &[
                (b"a0", b""),
                (b"a1", b""),
                (b"a2", b""),
                (b"a3", b""),
                (b"a4", b""),
            ],
        );
        for target in [b"a0".as_ref(), b"a2", b"a3", b"a4"] {
            let cur = block.seek(target).unwrap();
            assert!(cur.valid(), "seek {target:?} invalid");
            assert_eq!(cur.key(), target);
        }
        let cur = block.seek(b"a5").unwrap();
        assert!(!cur.valid());
        let cur = block.seek(b"").unwrap();
        assert_eq!(cur.key(), b"a0");
    }

    #[test]
    fn empty_key_is_legal() {
        let block = build_entries(1, &[(b"", b"v0"), (b"x", b"v1")]);
        let cur = block.seek(b"").unwrap();
        assert!(cur.valid());
        assert_eq!(cur.key(), b"");
        assert_eq!(cur.value().unwrap(), b"v0");
    }

    #[test]
    fn common_prefix_compresses() {
        let mut b = BlockBuilder::new(16).unwrap();
        b.add(b"aaaaaaaaaa01", b"v");
        b.add(b"aaaaaaaaaa02", b"v");
        let raw = b.finish();
        // Keys are 12 bytes each but only 3 suffix bytes should repeat.
        assert!(raw.len() < 12 + 12 + 16);
        let block = Block::parse(raw.to_vec()).unwrap();
        let stats = validate_entries(&block).unwrap();
        assert_eq!(stats.entries, 2);
    }

    #[test]
    fn rejects_bad_restart_offset() {
        let mut b = BlockBuilder::new(16).unwrap();
        b.add(b"k1", b"v1");
        b.add(b"k2", b"v2");
        let mut raw = b.finish().to_vec();
        let n = raw.len();
        // Corrupt restart[0]: must stay 0.
        raw[n - 8] = 0x01;
        let err = match Block::parse(raw) {
            Err(e) => e,
            Ok(_) => panic!("corrupt restart[0] was accepted"),
        };
        assert!(matches!(err, Error::Corruption(_)));
    }

    #[test]
    fn rejects_ordering_violation() {
        // Hand-build two entries out of order (builder normally prevents it).
        let mut b = BlockBuilder::new(16).unwrap();
        b.add(b"b", b"");
        b.add(b"a", b"");
        let raw = b.finish().to_vec();
        let block = Block::parse(raw).unwrap();
        assert!(validate_entries(&block).is_err());
    }
}
