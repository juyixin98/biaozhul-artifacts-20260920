//! Deterministic demo byte stream used by `GET /sample` and
//! `examples/make_sample.rs`.
//!
//! Layout, in order:
//! 1. leading ASCII noise,
//! 2. frame seq=1 `ping`,
//! 3. frame seq=2 whose payload contains the magic bytes,
//! 4. four noise bytes (one is a lone `0xDE`),
//! 5. frame seq=3 with one payload bit flipped (bad CRC),
//! 6. frame seq=4 immediately after it (must survive the bad CRC),
//! 7. header advertising a 5000-byte payload (over the 4096 cap) + 2 bytes,
//! 8. frame seq=5 (must survive the oversized claim),
//! 9. a truncated frame seq=6 (only 6 of its bytes — stream ends mid-header).

use crate::codec::{encode_frame, MAGIC};

pub fn build(max_payload: usize) -> Vec<u8> {
    let mut out = Vec::new();

    out.extend_from_slice(b"NOISE-1234!!");
    out.extend_from_slice(&encode_frame(1, b"ping", max_payload).unwrap());

    let mut p = b"abc".to_vec();
    p.extend_from_slice(&MAGIC);
    p.extend_from_slice(b"xyz");
    out.extend_from_slice(&encode_frame(2, &p, max_payload).unwrap());

    out.extend_from_slice(&[0x00, 0xFF, 0xDE, 0x01]);

    let mut bad = encode_frame(3, b"corrupt-me-please", max_payload).unwrap();
    bad[10] ^= 0xFF; // flip a payload bit -> CRC mismatch
    out.extend_from_slice(&bad);
    out.extend_from_slice(&encode_frame(4, b"after-bad-crc", max_payload).unwrap());

    out.extend_from_slice(&MAGIC);
    out.extend_from_slice(&5000u16.to_be_bytes());
    out.extend_from_slice(&5u16.to_be_bytes());
    out.extend_from_slice(b"xx");
    out.extend_from_slice(&encode_frame(5, b"after-oversized", max_payload).unwrap());

    let cut = encode_frame(6, b"half", max_payload).unwrap();
    out.extend_from_slice(&cut[..6]); // truncated mid-header

    out
}
