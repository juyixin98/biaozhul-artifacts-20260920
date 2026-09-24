//! Weak rolling checksum — rsync-style Adler-32 variant.
//!
//! This is deliberately a *weak* checksum: collisions are easy to construct,
//! so it is ONLY used to locate candidate block positions.  Every match must
//! be confirmed with a strong cryptographic digest (see [`crate::strong_hash`]).
//!
//! For a block `x[0..S]` the invariants are
//!
//! ```text
//! a =  (Σ x[i])           mod M
//! b =  (Σ (S - i) * x[i])  mod M
//! ```
//!
//! which can be rolled forward in O(1) when the window slides one byte:
//!
//! ```text
//! a' = a - x[0] + x[S]
//! b' = b - S*x[0] + a'
//! ```
//! all mod `M`.

/// Largest prime < 2^16, the classic rsync modulus.
pub const MOD: u32 = 65521;

/// Rolling weak checksum state.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Rollsum {
    a: u32,
    b: u32,
}

impl Rollsum {
    #[inline]
    pub fn new() -> Self {
        Self { a: 0, b: 0 }
    }

    /// Append one byte to the window (used to prime the first window).
    #[inline]
    pub fn feed(&mut self, byte: u8) {
        let x = byte as u32;
        self.a = (self.a + x) % MOD;
        self.b = (self.b + self.a) % MOD;
    }

    /// Slide the window: drop `old`, append `new`; `size` is the window length.
    #[inline]
    pub fn roll(&mut self, old: u8, new: u8, size: usize) {
        let x_out = old as u32;
        let x_in = new as u32;
        // a' = a - x_out + x_in  (mod M)
        self.a = (self.a + MOD - x_out + x_in) % MOD;
        // b' = b - S*x_out + a'  (mod M). Reduce S mod M first to keep the
        // intermediate in a small non-negative range.
        let weight = ((size as u64 % MOD as u64) * x_out as u64 % MOD as u64) as u32;
        self.b = (self.b + MOD + self.a - weight) % MOD;
    }

    /// Packed 32-bit value used as the hash-table key.
    #[inline]
    pub fn value(&self) -> u32 {
        (self.b << 16) | self.a
    }

    /// One-shot checksum of a slice (priming cost on the signature side).
    pub fn checksum(data: &[u8]) -> u32 {
        let mut rs = Rollsum::new();
        for &b in data {
            rs.feed(b);
        }
        rs.value()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roll_matches_recompute() {
        // For many random-ish windows, the O(1) rolled state must equal a
        // checksum computed from scratch over the new window.
        let buf: Vec<u8> = (0..5000u32)
            .map(|i| ((i.wrapping_mul(2654435761)) >> 13) as u8)
            .collect();
        for size in [1usize, 2, 3, 7, 64, 100, 255, 1024, 4096] {
            let mut rs = Rollsum::new();
            for &b in &buf[..size] {
                rs.feed(b);
            }
            assert_eq!(rs.value(), Rollsum::checksum(&buf[..size]));
            for start in 0..buf.len() - size - 1 {
                rs.roll(buf[start], buf[start + size], size);
                assert_eq!(
                    rs.value(),
                    Rollsum::checksum(&buf[start + 1..start + 1 + size]),
                    "size={size} start={start}"
                );
            }
        }
    }

    #[test]
    fn known_values_and_empty() {
        assert_eq!(Rollsum::checksum(b""), 0);
        // Deterministic simple value: single byte 1 -> a=1,b=1 -> 0x00010001
        assert_eq!(Rollsum::checksum(&[1u8]), 0x0001_0001);
    }
}
