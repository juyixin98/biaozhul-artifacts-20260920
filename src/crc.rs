//! CRC-32 (IEEE, reflected) — hand-rolled, no external CRC crate.
//!
//! Polynomial `0xEDB88320`, init `0xFFFF_FFFF`, final XOR `0xFFFF_FFFF`.

/// A lazily-built CRC-32 table.
#[derive(Clone)]
pub struct Crc32 {
    state: u32,
}

const POLY: u32 = 0xEDB8_8320;

/// Compute the 256-entry lookup table (const-evaluated at compile time).
const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i: u32 = 0;
    while i < 256 {
        let mut c = i;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 { POLY ^ (c >> 1) } else { c >> 1 };
            k += 1;
        }
        table[i as usize] = c;
        i += 1;
    }
    table
}

static TABLE: [u32; 256] = build_table();

impl Crc32 {
    pub fn new() -> Self {
        Crc32 { state: u32::MAX }
    }

    pub fn update(&mut self, bytes: &[u8]) {
        let mut c = self.state;
        for &b in bytes {
            c = TABLE[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
        }
        self.state = c;
    }

    pub fn finish(self) -> u32 {
        self.state ^ u32::MAX
    }

    /// One-shot checksum of `bytes`.
    pub fn checksum(bytes: &[u8]) -> u32 {
        let mut h = Crc32::new();
        h.update(bytes);
        h.finish()
    }
}

impl Default for Crc32 {
    fn default() -> Self {
        Crc32::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Known-answer vectors (CRC-32/ISO-HDLC values).
    #[test]
    fn known_vectors() {
        assert_eq!(Crc32::checksum(b""), 0x0000_0000);
        assert_eq!(Crc32::checksum(b"123456789"), 0xCBF4_3926);
        assert_eq!(Crc32::checksum(b"a"), 0xE8B7_BE43);
    }

    #[test]
    fn split_update_matches_oneshot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let whole = Crc32::checksum(data);
        let mut h = Crc32::new();
        h.update(&data[..7]);
        h.update(&data[7..20]);
        h.update(&data[20..]);
        assert_eq!(h.finish(), whole);
    }
}
