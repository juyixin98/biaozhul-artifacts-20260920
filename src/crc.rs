//! CRC-32/ISO-HDLC (a.k.a. zlib/PNG CRC-32).
//!
//! Reflected polynomial `0xEDB88320`, init `0xFFFF_FFFF`, final XOR `0xFFFF_FFFF`.
//! The lookup table is built at compile time, so no allocation or lazy
//! initialization is needed.

const POLY: u32 = 0xEDB8_8320;

const fn build_table() -> [u32; 256] {
    let mut table = [0u32; 256];
    let mut i = 0u32;
    while i < 256 {
        let mut crc = i;
        let mut j = 0;
        while j < 8 {
            crc = if crc & 1 != 0 {
                POLY ^ (crc >> 1)
            } else {
                crc >> 1
            };
            j += 1;
        }
        table[i as usize] = crc;
        i += 1;
    }
    table
}

const TABLE: [u32; 256] = build_table();

/// Compute the CRC-32 of `data` in one shot.
pub fn checksum(data: &[u8]) -> u32 {
    checksum_update(0xFFFF_FFFF, data) ^ 0xFFFF_FFFF
}

/// Continue a running CRC. `state` must be a value previously returned by
/// [`checksum_update`] (i.e. the un-finalized, not XORed-out CRC).
pub fn checksum_update(state: u32, data: &[u8]) -> u32 {
    let mut crc = state;
    for &byte in data {
        let idx = ((crc ^ byte as u32) & 0xFF) as usize;
        crc = TABLE[idx] ^ (crc >> 8);
    }
    crc
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The canonical "check" value for CRC-32/ISO-HDLC over b"123456789".
    #[test]
    fn known_check_value() {
        assert_eq!(checksum(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn empty_is_zero() {
        assert_eq!(checksum(b""), 0);
    }

    #[test]
    fn streaming_matches_oneshot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let split = 13;
        let s1 = checksum_update(0xFFFF_FFFF, &data[..split]);
        let s2 = checksum_update(s1, &data[split..]);
        assert_eq!(s2 ^ 0xFFFF_FFFF, checksum(data));
    }
}
