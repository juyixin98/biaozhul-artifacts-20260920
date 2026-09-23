//! CRC-32 (ISO-HDLC, poly 0xEDB88320 reflected) — the same variant used by
//! zlib/gzip/zip. Used for per-frame and whole-run integrity checks.

#[derive(Clone, Debug, Default)]
pub struct Crc32 {
    state: u32,
}

const TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut i = 0u32;
    while i < 256 {
        let mut c = i;
        let mut j = 0;
        while j < 8 {
            c = if c & 1 != 0 { 0xEDB88320 ^ (c >> 1) } else { c >> 1 };
            j += 1;
        }
        table[i as usize] = c;
        i += 1;
    }
    table
};

impl Crc32 {
    pub fn new() -> Self {
        Crc32 { state: 0xFFFF_FFFF }
    }

    pub fn update(&mut self, bytes: &[u8]) {
        let mut c = self.state;
        for &b in bytes {
            c = TABLE[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
        }
        self.state = c;
    }

    pub fn finish(&self) -> u32 {
        self.state ^ 0xFFFF_FFFF
    }

    /// One-shot helper.
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
        assert_eq!(Crc32::checksum(b""), 0);
        assert_eq!(Crc32::checksum(b"123456789"), 0xCBF43926);
        assert_eq!(Crc32::checksum(b"The quick brown fox jumps over the lazy dog"), 0x414FA339);
    }

    #[test]
    fn split_feeds_match_oneshot() {
        let data = b"external-sort-segment";
        let mut c = Crc32::new();
        c.update(&data[..7]);
        c.update(&data[7..]);
        assert_eq!(c.finish(), Crc32::checksum(data));
    }
}
