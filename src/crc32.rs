//! CRC-32/ISO-HDLC (poly 0xEDB88320, init 0xFFFF_FFFF, xorout 0xFFFF_FFFF).
//!
//! Same variant used by zlib/PNG/gzip. Implemented with a runtime-built
//! lookup table so the crate has zero external dependencies.

#[rustfmt::skip]
const TABLE: [u32; 256] = {
    // const table generation
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

pub fn checksum(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc = TABLE[((crc ^ b as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    crc ^ 0xFFFF_FFFF
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        // CRC-32 check value of "123456789" is 0xCBF43926
        assert_eq!(checksum(b"123456789"), 0xCBF43926);
        assert_eq!(checksum(b""), 0);
        assert_eq!(checksum(b"a"), 0xE8B7BE43);
    }
}
