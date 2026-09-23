//! Domain-separated SHA-256 hashing.
//!
//! Every preimage is tagged so leaf preimages, internal-node preimages and the
//! empty-tree marker can never collide with one another:
//!
//! * leaf   : `SHA256(0x00 ‖ u32be(|key|) ‖ key ‖ u32be(|value|) ‖ value)`
//! * branch : `SHA256(0x01 ‖ left(32) ‖ right(32))`
//! * empty  : `SHA256(0x02)`
//!
//! Length-prefixing the variable-length `key`/`value` fields rules out
//! ambiguity at concatenation boundaries.

use sha2::{Digest, Sha256};

/// Length of every hash in bytes (SHA-256).
pub const HASH_LEN: usize = 32;

/// Domain tag prepended to leaf preimages.
pub const TAG_LEAF: u8 = 0x00;
/// Domain tag prepended to internal-node preimages.
pub const TAG_BRANCH: u8 = 0x01;
/// Domain tag hashed alone to mark the empty tree.
pub const TAG_EMPTY: u8 = 0x02;

/// A 32-byte SHA-256 digest.
pub type Hash = [u8; HASH_LEN];

#[inline]
fn put_u32be(out: &mut Vec<u8>, n: usize) {
    let n = u32::try_from(n).expect("length must fit in u32");
    out.extend_from_slice(&n.to_be_bytes());
}

/// Hash a leaf `(key, value)` pair, including the leaf domain tag.
pub fn hash_leaf(key: &[u8], value: &[u8]) -> Hash {
    let mut pre = Vec::with_capacity(1 + 4 + key.len() + 4 + value.len());
    pre.push(TAG_LEAF);
    put_u32be(&mut pre, key.len());
    pre.extend_from_slice(key);
    put_u32be(&mut pre, value.len());
    pre.extend_from_slice(value);
    let h = Sha256::digest(&pre);
    let mut out = [0u8; HASH_LEN];
    out.copy_from_slice(&h);
    out
}

/// Hash an internal node `H(left ‖ right)`, including the branch domain tag.
///
/// `left` and `right` are ordered by key-space position: `left` always covers
/// keys strictly smaller than every key covered by `right`.
pub fn hash_branch(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update([TAG_BRANCH]);
    h.update(left);
    h.update(right);
    let mut out = [0u8; HASH_LEN];
    out.copy_from_slice(h.finalize().as_slice());
    out
}

/// Root hash of the empty tree (`SHA256(TAG_EMPTY)`).
pub fn empty_root() -> Hash {
    let mut out = [0u8; HASH_LEN];
    out.copy_from_slice(Sha256::digest([TAG_EMPTY]).as_slice());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tags_are_separated_and_stable() {
        // Known vectors computed independently once and pinned so an
        // accidental change to the encoding is caught.
        let l = hash_leaf(b"a", b"");
        let b = hash_branch(&l, &l);
        let e = empty_root();
        assert_eq!(hex::encode(e), "dbc1b4c900ffe48d575b5da5c638040125f65db0fe3e24494b76ea986457d986");
        assert_ne!(l, b);
        assert_ne!(l, e);
        assert_ne!(b, e);

        // Length-prefix sanity: different splits of the same raw bytes differ.
        assert_ne!(hash_leaf(b"ab", b"c"), hash_leaf(b"a", b"bc"));
        // Empty value is a real value and differs from any non-empty one.
        assert_ne!(hash_leaf(b"k", b""), hash_leaf(b"k", b"\0"));
    }
}
