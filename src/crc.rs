//! IEEE CRC-32 (poly 0xEDB88320, the zlib/checksum32 variant), std-only.
//!
//! Used to detect torn writes and corrupted temporary segments. This is an
//! integrity check, not a cryptographic hash.

/// Precomputed 256-entry lookup table.
#[derive(Clone, Copy)]
pub struct Table {
    entries: [u32; 256],
}

impl Default for Table {
    fn default() -> Self {
        Table::new()
    }
}

impl Table {
    pub const fn new() -> Self {
        let mut entries = [0u32; 256];
        let mut i = 0usize;
        while i < 256 {
            let mut c = i as u32;
            let mut k = 0;
            while k < 8 {
                c = if c & 1 != 0 {
                    0xEDB88320 ^ (c >> 1)
                } else {
                    c >> 1
                };
                k += 1;
            }
            entries[i] = c;
            i += 1;
        }
        Table { entries }
    }

    /// Update `crc` with `bytes`; start from [`Crc::INIT`].
    pub const fn update(&self, mut crc: u32, bytes: &[u8]) -> u32 {
        let mut i = 0;
        while i < bytes.len() {
            let idx = ((crc ^ bytes[i] as u32) & 0xFF) as usize;
            crc = self.entries[idx] ^ (crc >> 8);
            i += 1;
        }
        crc
    }
}

/// Streaming CRC-32 accumulator.
#[derive(Clone)]
pub struct Crc {
    table: Table,
    value: u32,
}

impl Default for Crc {
    fn default() -> Self {
        Self::new()
    }
}

impl Crc {
    pub const INIT: u32 = 0xFFFF_FFFF;

    pub fn new() -> Self {
        Crc {
            table: Table::new(),
            value: Self::INIT,
        }
    }

    pub fn write(&mut self, bytes: &[u8]) {
        self.value = self.table.update(self.value, bytes);
    }

    pub fn finish(&self) -> u32 {
        self.value ^ 0xFFFF_FFFF
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Known-answer vectors (CRC-32/ISO-HDLC as produced by `crc32` util).
    #[test]
    fn known_vectors() {
        fn crc(b: &[u8]) -> u32 {
            let mut c = Crc::new();
            c.write(b);
            c.finish()
        }
        assert_eq!(crc(b""), 0);
        assert_eq!(crc(b"123456789"), 0xCBF43926);
        assert_eq!(crc(b"a"), 0xE8B7_BE43);
        // Streaming == one-shot.
        let mut c = Crc::new();
        c.write(b"1234");
        c.write(b"56789");
        assert_eq!(c.finish(), 0xCBF43926);
    }
}
