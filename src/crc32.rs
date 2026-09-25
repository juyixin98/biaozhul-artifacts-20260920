//! CRC-32 (IEEE 802.3 / zlib polynomial 0xEDB88320, reflected).
//!
//! Hand-written table-driven implementation — no external crate. Used by the
//! frame codec to detect corrupted headers and payloads.

use std::sync::OnceLock;

static TABLE: OnceLock<[u32; 256]> = OnceLock::new();

fn table() -> &'static [u32; 256] {
    TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        let mut i = 0usize;
        while i < 256 {
            let mut crc = i as u32;
            let mut j = 0;
            while j < 8 {
                crc = if crc & 1 != 0 {
                    0xEDB88320 ^ (crc >> 1)
                } else {
                    crc >> 1
                };
                j += 1;
            }
            t[i] = crc;
            i += 1;
        }
        t
    })
}

/// Streaming CRC-32: feed header and payload in separate `update` calls
/// without copying them into one buffer.
#[derive(Clone)]
pub struct Crc32 {
    crc: u32,
}

impl Crc32 {
    pub fn new() -> Self {
        Crc32 { crc: 0xFFFF_FFFF }
    }

    pub fn update(&mut self, data: &[u8]) {
        let t = table();
        for &b in data {
            let idx = ((self.crc ^ b as u32) & 0xFF) as usize;
            self.crc = (self.crc >> 8) ^ t[idx];
        }
    }

    pub fn finalize(self) -> u32 {
        self.crc ^ 0xFFFF_FFFF
    }
}

impl Default for Crc32 {
    fn default() -> Self {
        Self::new()
    }
}

/// Compute CRC-32 over `data` (initial 0xFFFFFFFF, final XOR 0xFFFFFFFF).
pub fn checksum(data: &[u8]) -> u32 {
    let mut h = Crc32::new();
    h.update(data);
    h.finalize()
}

#[cfg(test)]
mod tests {
    use super::checksum;

    // Known vectors (zlib/PNG CRC-32).
    #[test]
    fn known_vectors() {
        assert_eq!(checksum(b""), 0);
        assert_eq!(checksum(b"123456789"), 0xCBF43926);
        assert_eq!(checksum(b"a"), 0xE8B7_BE43);
        assert_eq!(checksum(b"hello"), 0x3610_A686);
    }

    #[test]
    fn differs_on_one_bit() {
        assert_ne!(checksum(b"abc"), checksum(b"abd"));
    }
}
