//! CRC-32/ISO-HDLC (the zlib/gzip polynomial, reflected, init 0xFFFF_FFFF,
//! final XOR 0xFFFF_FFFF). Used to frame on-disk records so a torn write or
//! corrupted tail can be detected and truncated on open.

/// Precomputed table for the reflected polynomial 0xEDB8_8320.
const fn make_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut c = i as u32;
        let mut j = 0;
        while j < 8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
            j += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
}

const TABLE: [u32; 256] = make_table();

#[derive(Debug, Clone, Copy)]
pub struct Crc32 {
    state: u32,
}

impl Default for Crc32 {
    fn default() -> Self {
        Self { state: 0xFFFF_FFFF }
    }
}

impl Crc32 {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn update(&mut self, bytes: &[u8]) {
        let mut c = self.state;
        for &b in bytes {
            c = TABLE[((c ^ b as u32) & 0xff) as usize] ^ (c >> 8);
        }
        self.state = c;
    }

    pub fn finish(self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }

    /// One-shot checksum.
    pub fn checksum(bytes: &[u8]) -> u32 {
        let mut c = Crc32::new();
        c.update(bytes);
        c.finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        // Standard CRC-32 check values.
        assert_eq!(Crc32::checksum(b""), 0);
        assert_eq!(Crc32::checksum(b"123456789"), 0xCBF4_3926);
        assert_eq!(Crc32::checksum(b"hello"), 0x3610_A686);
    }

    #[test]
    fn streaming_matches_oneshot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let mut c = Crc32::new();
        c.update(&data[..10]);
        c.update(&data[10..20]);
        c.update(&data[20..]);
        assert_eq!(c.finish(), Crc32::checksum(data));
    }
}
