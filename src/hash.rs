//! Pluggable hash functions.
//!
//! The hash function is chosen at index-creation time and its id is recorded
//! in the on-disk header, so reopening a file always uses the same function.
//!
//! Two functions are provided beyond a production-quality default:
//!
//! * [`HashKind::Fnv1a64`] — 64-bit FNV-1a, good distribution (default).
//! * [`HashKind::Constant`] — always returns `0`. Used to deterministically
//!   reproduce an *all-collision* workload and verify the service reports a
//!   clear capacity error instead of looping forever.
//! * [`HashKind::U64LowBits`] — parses the key as a decimal `u64` and hashes
//!   the key to itself. This makes the low hash bits exactly the key value, so
//!   tests can craft precise split/merge scenarios (e.g. keys `0..` exercise
//!   the directory one slot at a time).
use crate::error::IndexError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HashKind {
    Fnv1a64 = 1,
    Constant = 2,
    U64LowBits = 3,
}

impl HashKind {
    pub fn from_id(id: u8) -> Result<HashKind, IndexError> {
        match id {
            1 => Ok(HashKind::Fnv1a64),
            2 => Ok(HashKind::Constant),
            3 => Ok(HashKind::U64LowBits),
            other => Err(IndexError::Corrupt(format!(
                "unknown hash kind id {}",
                other
            ))),
        }
    }

    pub fn id(self) -> u8 {
        self as u8
    }

    pub fn parse(name: &str) -> Option<HashKind> {
        match name.to_ascii_lowercase().as_str() {
            "fnv" | "fnv1a" | "fnv1a64" => Some(HashKind::Fnv1a64),
            "constant" | "zero" => Some(HashKind::Constant),
            "u64" | "u64lowbits" | "lowbits" => Some(HashKind::U64LowBits),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            HashKind::Fnv1a64 => "fnv1a64",
            HashKind::Constant => "constant",
            HashKind::U64LowBits => "u64lowbits",
        }
    }

    pub fn hash(self, key: &[u8]) -> u64 {
        match self {
            HashKind::Fnv1a64 => fnv1a_64(key),
            HashKind::Constant => 0,
            HashKind::U64LowBits => parse_dec_u64(key),
        }
    }
}

fn fnv1a_64(data: &[u8]) -> u64 {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut h = OFFSET;
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

/// Parse a key as a decimal u64; non-numeric (or overflowing) keys hash to 0.
/// Test scenarios use plain decimal keys so collisions remain intentional.
fn parse_dec_u64(data: &[u8]) -> u64 {
    let mut v: u64 = 0;
    let mut any = false;
    for &b in data {
        if !b.is_ascii_digit() {
            return 0;
        }
        any = true;
        v = match v
            .checked_mul(10)
            .and_then(|x| x.checked_add((b - b'0') as u64))
        {
            Some(x) => x,
            None => return 0,
        };
    }
    if any {
        v
    } else {
        0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_hashes_always_collide() {
        assert_eq!(HashKind::Constant.hash(b"a"), 0);
        assert_eq!(HashKind::Constant.hash(b"anything"), 0);
    }

    #[test]
    fn u64_hash_is_identity() {
        let h = HashKind::U64LowBits;
        assert_eq!(h.hash(b"0"), 0);
        assert_eq!(h.hash(b"7"), 7);
        assert_eq!(h.hash(b"255"), 255);
        assert_eq!(h.hash(b"not-a-number"), 0);
    }

    #[test]
    fn fnv_distributes_prefix_keys() {
        let h = HashKind::Fnv1a64;
        let a = h.hash(b"key-0");
        let b = h.hash(b"key-1");
        assert_ne!(a, b);
    }
}
