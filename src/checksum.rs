//! Weak rolling checksum (rsync convention) and strong cryptographic hash.
//!
//! # Weak checksum
//!
//! We use the rsync-style Adler-like 32-bit checksum. For a block `X[0..n]`
//! (bytes interpreted as integers in 0..=255):
//!
//! ```text
//! a =   sum X[i]        (mod 2^16)
//! b =   sum (n-i) X[i]  (mod 2^16)
//! weak = a | (b << 16)
//! ```
//!
//! Note: rsync intentionally omits the `+1` bias that classic zlib Adler-32
//! applies to both accumulators. With this convention, when the rolling window
//! shifts one byte (dropping `X0`, appending `Y`, window length `n`):
//!
//! ```text
//! a' = a - X0 + Y
//! b' = b - n*X0 + a'
//! ```
//!
//! The weak checksum is a 16+16-bit fingerprint only: it is cheap and can be
//! rolled, but collisions are easy to construct. It is therefore used *only*
//! to locate candidate blocks. Content equality is decided exclusively by the
//! strong BLAKE3 hash ([`strong`]); see `delta.rs`.

/// Minimum legal block size in bytes.
pub const MIN_BLOCK_SIZE: usize = 16;
/// Maximum legal block size in bytes.
pub const MAX_BLOCK_SIZE: usize = 64 * 1024;

/// Compute both accumulators of the weak checksum over a full window.
/// Returns `(a, b)` as used by rsync (no Adler `+1` bias).
#[inline]
pub fn weak_parts(data: &[u8]) -> (u32, u32) {
    let n = data.len() as u32;
    let mut a: u32 = 0;
    let mut b: u32 = 0;
    for (i, &x) in data.iter().enumerate() {
        a = a.wrapping_add(x as u32);
        b = b.wrapping_add((n - i as u32).wrapping_mul(x as u32));
    }
    (a & 0xffff, b & 0xffff)
}

/// Weak checksum over a complete block (non-rolling).
#[inline]
pub fn weak(data: &[u8]) -> u32 {
    let (a, b) = weak_parts(data);
    a | (b << 16)
}

/// Advance the weak checksum accumulators by one byte.
///
/// * `parts`   - `(a, b)` for the old window,
/// * `removed` - byte leaving the window,
/// * `added`   - byte entering the window,
/// * `n`       - window length in bytes.
///
/// Returns `(a', b')`. All arithmetic is modular exactly as in rsync; because
/// modular arithmetic identifies multiples of the modulus, the `mod 2^16`
/// reduction each step is equivalent to reducing only when packing the result.
#[inline]
pub fn roll(parts: (u32, u32), removed: u8, added: u8, n: usize) -> (u32, u32) {
    let (a, b) = parts;
    let n = n as u32;
    // Work without the 16-bit mask; `a2` is needed unmasked in the b update.
    let a2 = a.wrapping_sub(removed as u32).wrapping_add(added as u32);
    let b2 = b.wrapping_sub(n.wrapping_mul(removed as u32)).wrapping_add(a2);
    (a2 & 0xffff, b2 & 0xffff)
}

/// Pack `(a, b)` accumulators into the 32-bit weak checksum.
#[inline]
pub fn pack_parts(parts: (u32, u32)) -> u32 {
    (parts.0 & 0xffff) | ((parts.1 & 0xffff) << 16)
}

/// Strong cryptographic hash of a block. BLAKE3 is fast and collision
/// resistant; this value is the sole authority for "the content matches".
pub fn strong(data: &[u8]) -> [u8; 32] {
    *blake3::hash(data).as_bytes()
}

/// Strong hash of the whole reconstructed artifact, used for end-to-end
/// verification of patch application.
pub fn strong_whole(data: &[u8]) -> [u8; 32] {
    strong(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roll_matches_full_computation_every_offset() {
        // At every offset the rolled accumulators must equal the checksum of
        // the window computed from scratch.
        let data = b"the quick brown fox jumps over the lazy dog. 0123456789";
        for n in [1usize, 3, 7, 16, 31] {
            if data.len() < n + 5 {
                continue;
            }
            let mut parts = weak_parts(&data[0..n]);
            for start in 0..data.len() - n {
                // recompute-from-scratch check
                assert_eq!(
                    parts,
                    weak_parts(&data[start..start + n]),
                    "n={n} start={start}"
                );
                parts = roll(parts, data[start], data[start + n], n);
            }
            // final window check
            let last = data.len() - n;
            assert_eq!(parts, weak_parts(&data[last..last + n]));
        }
    }

    #[test]
    fn weak_is_order_sensitive() {
        // Same byte multiset, different order -> generally different checksum.
        assert_ne!(weak(b"abcdefgh"), weak(b"hgfedcba"));
    }

    #[test]
    fn empty_and_single() {
        assert_eq!(weak_parts(b""), (0, 0));
        let (a, b) = weak_parts(b"Z");
        assert_eq!(a, b);
        assert_eq!(a, b'Z' as u32);
    }
}
