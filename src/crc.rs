//! CRC-32 (IEEE 802.3) — real implementation, no native/external crypto crates.
//!
//! Polynomial `0xEDB8_8320` (reflected form of 0x04C11DB7), init `0xFFFF_FFFF`,
//! input/output reflected, final XOR `0xFFFF_FFFF`. This is the same CRC used by
//! zlib/PNG/Ethernet; it detects all 1-, 2-, 3-bit errors, all burst errors up
//! to 32 bits in messages of practical length, so a corrupted frame is rejected
//! rather than silently accepted.

const POLY: u32 = 0xEDB8_8320;

/// Build the 256-entry lookup table at compile time.
const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i: u32 = 0;
    while i < 256 {
        let mut crc = i;
        let mut bit = 0;
        while bit < 8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ POLY
            } else {
                crc >> 1
            };
            bit += 1;
        }
        table[i as usize] = crc;
        i += 1;
    }
    table
}

const TABLE: [u32; 256] = build_table();

/// Streaming CRC-32 accumulator.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Crc32 {
    state: u32,
}

impl Default for Crc32 {
    fn default() -> Self {
        Self::new()
    }
}

impl Crc32 {
    pub fn new() -> Self {
        Self { state: 0xFFFF_FFFF }
    }

    /// Resume from a raw state (used only by tests/tooling).
    pub fn from_raw(state: u32) -> Self {
        Self { state }
    }

    pub fn update(&mut self, bytes: &[u8]) {
        let mut crc = self.state;
        for &b in bytes {
            let idx = ((crc ^ b as u32) & 0xFF) as usize;
            crc = (crc >> 8) ^ TABLE[idx];
        }
        self.state = crc;
    }

    /// Final checksum (XOR-out applied). Does not mutate the accumulator.
    pub fn finalize(self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }

    /// One-shot convenience.
    pub fn checksum(bytes: &[u8]) -> u32 {
        let mut c = Self::new();
        c.update(bytes);
        c.finalize()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Canonical check vectors for CRC-32/ISO-HDLC.
    #[test]
    fn known_vectors() {
        assert_eq!(Crc32::checksum(b""), 0x0000_0000);
        assert_eq!(Crc32::checksum(b"123456789"), 0xCBF4_3926);
        assert_eq!(Crc32::checksum(b"a"), 0xE8B7_BE43);
    }

    #[test]
    fn streaming_equals_oneshot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let mut c = Crc32::new();
        c.update(&data[..10]);
        c.update(&data[10..20]);
        c.update(&data[20..]);
        assert_eq!(c.finalize(), Crc32::checksum(data));
    }
}
