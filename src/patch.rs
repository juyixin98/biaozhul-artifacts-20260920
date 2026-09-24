//! Patch wire format, application and verification.
//!
//! ```text
//! magic        4 bytes  b"ADLP"
//! version      u16      = 1
//! block_len    u32      signature block size (informational)
//! target_len   u64      expected reconstructed length
//! basis_len    u64      length the patch was built against
//! basis_hash   32 bytes BLAKE3 of the basis (wrong-basis detection)
//! target_hash  32 bytes BLAKE3 of the expected result
//! ops          repeated, terminated by tag END (0x00):
//!   LITERAL 0x01  length u32  bytes[length]
//!   REF     0x02  index  u32  length u32
//! ```
//!
//! Applying a patch produces the target; the implementation then verifies
//! BOTH the declared length and the strong target hash.  A wrong basis is
//! detected up front via `basis_hash`.

use crate::delta::{Delta, Op};
use crate::error::{Error, Result};
use crate::signature::strong_hash;

pub const MAGIC: &[u8; 4] = b"ADLP";
pub const VERSION: u16 = 1;
const HEADER_LEN: usize = 4 + 2 + 4 + 8 + 8 + 32 + 32;

const TAG_END: u8 = 0;
const TAG_LITERAL: u8 = 1;
const TAG_REF: u8 = 2;

/// Decoded patch (header fields plus the op stream).
#[derive(Debug, Clone)]
pub struct Patch {
    pub block_len: u32,
    pub target_len: u64,
    pub basis_len: u64,
    pub basis_hash: [u8; 32],
    pub target_hash: [u8; 32],
    pub ops: Vec<Op>,
}

impl Patch {
    /// Build a patch from a delta; caller supplies the basis identity.
    pub fn new(delta: &Delta, block_len: u32, basis_len: u64, basis_hash: [u8; 32]) -> Patch {
        Patch {
            block_len,
            target_len: delta.target_len,
            basis_len,
            basis_hash,
            target_hash: delta.target_hash,
            ops: delta.ops.clone(),
        }
    }

    /// Serialize to the compact binary wire format.
    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&self.block_len.to_le_bytes());
        out.extend_from_slice(&self.target_len.to_le_bytes());
        out.extend_from_slice(&self.basis_len.to_le_bytes());
        out.extend_from_slice(&self.basis_hash);
        out.extend_from_slice(&self.target_hash);
        for op in &self.ops {
            match op {
                Op::Literal(bytes) => {
                    out.push(TAG_LITERAL);
                    let len = u32::try_from(bytes.len())
                        .expect("literal length must fit in u32");
                    out.extend_from_slice(&len.to_le_bytes());
                    out.extend_from_slice(bytes);
                }
                Op::Ref { index, length } => {
                    out.push(TAG_REF);
                    out.extend_from_slice(&index.to_le_bytes());
                    out.extend_from_slice(&length.to_le_bytes());
                }
            }
        }
        out.push(TAG_END);
        out
    }

    /// Parse and strictly validate a patch byte stream.
    pub fn decode(buf: &[u8]) -> Result<Patch> {
        if buf.len() < HEADER_LEN || &buf[..4] != MAGIC {
            return Err(Error::Malformed("bad patch magic"));
        }
        let version = u16::from_le_bytes([buf[4], buf[5]]);
        if version != VERSION {
            return Err(Error::Malformed("unsupported patch version"));
        }
        let rd = |o: usize, n: usize| &buf[o..o + n];
        let block_len = u32::from_le_bytes(rd(6, 4).try_into().unwrap());
        if block_len == 0 || block_len > crate::signature::MAX_BLOCK_SIZE {
            return Err(Error::Malformed("patch block size out of range"));
        }
        let target_len = u64::from_le_bytes(rd(10, 8).try_into().unwrap());
        let basis_len = u64::from_le_bytes(rd(18, 8).try_into().unwrap());
        let mut basis_hash = [0u8; 32];
        basis_hash.copy_from_slice(rd(26, 32));
        let mut target_hash = [0u8; 32];
        target_hash.copy_from_slice(rd(58, 32));

        let mut pos = HEADER_LEN;
        let mut ops = Vec::new();
        let mut planned: u64 = 0;
        loop {
            if pos >= buf.len() {
                return Err(Error::Malformed("patch op stream truncated (no END tag)"));
            }
            match buf[pos] {
                TAG_END => {
                    pos += 1;
                    break;
                }
                TAG_LITERAL => {
                    pos += 1;
                    let len = read_u32(buf, &mut pos)? as u64;
                    if (pos as u64) + len > buf.len() as u64 {
                        return Err(Error::Malformed("literal payload truncated"));
                    }
                    if !adds_up(&planned, len, target_len) {
                        return Err(Error::Malformed("literal would exceed declared target length"));
                    }
                    planned += len;
                    ops.push(Op::Literal(buf[pos..pos + len as usize].to_vec()));
                    pos += len as usize;
                }
                TAG_REF => {
                    pos += 1;
                    let index = read_u32(buf, &mut pos)?;
                    let length = read_u32(buf, &mut pos)? as u64;
                    if length == 0 || length > block_len as u64 {
                        return Err(Error::Malformed("ref length out of range"));
                    }
                    if !adds_up(&planned, length, target_len) {
                        return Err(Error::Malformed("ref would exceed declared target length"));
                    }
                    // Index must be a plausible basis block.
                    let basis_blocks = basis_len.div_ceil(block_len as u64);
                    if index as u64 >= basis_blocks {
                        return Err(Error::Malformed("ref index beyond basis blocks"));
                    }
                    planned += length;
                    ops.push(Op::Ref { index, length: length as u32 });
                }
                other => {
                    let _ = other;
                    return Err(Error::Malformed("unknown op tag"));
                }
            }
        }
        if pos != buf.len() {
            return Err(Error::Malformed("trailing bytes after END tag"));
        }
        if planned != target_len {
            return Err(Error::Malformed("ops do not reconstruct declared target length"));
        }
        Ok(Patch { block_len, target_len, basis_len, basis_hash, target_hash, ops })
    }
}

fn read_u32(buf: &[u8], pos: &mut usize) -> Result<u32> {
    if *pos + 4 > buf.len() {
        return Err(Error::Malformed("integer field truncated"));
    }
    let v = u32::from_le_bytes(buf[*pos..*pos + 4].try_into().unwrap());
    *pos += 4;
    Ok(v)
}

/// Apply a validated patch against `basis`, returning the reconstructed
/// artifact. Verifies, in order:
///
/// 1. `basis_hash` matches the supplied basis (wrong old content rejected);
/// 2. every REF is in bounds with the declared length;
/// 3. reconstructed length equals `target_len`;
/// 4. BLAKE3 of the result equals `target_hash` (strong end-to-end check).
pub fn apply_patch(patch: &Patch, basis: &[u8]) -> Result<Vec<u8>> {
    if strong_hash(basis) != patch.basis_hash || basis.len() as u64 != patch.basis_len {
        return Err(Error::BasisMismatch);
    }
    let mut out = Vec::with_capacity(patch.target_len as usize);
    for op in &patch.ops {
        match op {
            Op::Literal(bytes) => out.extend_from_slice(bytes),
            Op::Ref { index, length } => {
                let start = *index as u64 * patch.block_len as u64;
                let end = start
                    .checked_add(*length as u64)
                    .ok_or(Error::OutOfBounds("ref offset overflow"))?;
                if end > basis.len() as u64 {
                    return Err(Error::OutOfBounds("ref reads past end of basis"));
                }
                out.extend_from_slice(&basis[start as usize..end as usize]);
            }
        }
    }
    if out.len() as u64 != patch.target_len {
        return Err(Error::LengthMismatch { expected: patch.target_len, actual: out.len() as u64 });
    }
    if strong_hash(&out) != patch.target_hash {
        return Err(Error::HashMismatch);
    }
    Ok(out)
}

/// `acc + add` does not overflow and stays within `limit`.
fn adds_up(acc: &u64, add: u64, limit: u64) -> bool {
    acc.checked_add(add).is_some_and(|v| v <= limit)
}
