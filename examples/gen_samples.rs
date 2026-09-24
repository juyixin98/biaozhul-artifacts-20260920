//! Generates deterministic sample byte streams under `samples/`.
//!
//!     cargo run --example gen_samples
//!
//! Every frame is produced by the real `serialframe::encode_frame` encoder.
//! Corrupt/oversize/noise cases are crafted from real encodings so they exercise
//! exactly what the wire looks like.

use std::fs;
use std::path::{Path, PathBuf};

use serialframe::parser::{encode_frame, MAGIC0, MAGIC1};

fn write(dir: &Path, name: &str, data: &[u8]) {
    let path = dir.join(name);
    fs::write(&path, data).expect("write sample file");
    println!("  wrote {:?} ({} bytes)", path, data.len());
}

fn main() {
    let dir = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| "samples".to_string()));
    fs::create_dir_all(&dir).expect("create samples dir");
    println!("generating samples in {:?}", dir);

    // 1) Two clean, in-order frames (sticky packet).
    let mut s = Vec::new();
    s.extend_from_slice(&encode_frame(1, b"hello serial").unwrap());
    s.extend_from_slice(&encode_frame(2, b"frame two").unwrap());
    write(&dir, "01_good.bin", &s);

    // 2) Garbage before/between/after frames, including a lone 0xA5 tail.
    let mut s = Vec::new();
    s.extend_from_slice(&[0x00, 0xFF, 0x12, MAGIC0]); // noise ending in half-magic
    s.extend_from_slice(&encode_frame(10, b"after-noise").unwrap());
    s.extend_from_slice(&[0x7E, 0x7E, 0x01]); // inter-frame noise
    s.extend_from_slice(&encode_frame(11, b"ok").unwrap());
    s.push(MAGIC0); // dangling half-magic; must wait for more bytes
    write(&dir, "02_noise.bin", &s);

    // 3) Real frame whose payload is corrupted (bad CRC), immediately followed
    //    by a valid frame. The valid frame MUST still be decoded.
    let mut bad = encode_frame(20, b"CORRUPT").unwrap();
    // Flip one payload byte (payload starts after 6-byte header).
    bad[6] ^= 0xFF;
    let mut s = Vec::new();
    s.extend_from_slice(&bad);
    s.extend_from_slice(&[0xDE, 0xAD]); // noise between
    s.extend_from_slice(&encode_frame(21, b"survived").unwrap());
    write(&dir, "03_bad_crc_recovers.bin", &s);

    // 4) Magic + declared length 65535 (huge, must not allocate) + noise + frame.
    let mut s = Vec::new();
    s.push(MAGIC0);
    s.push(MAGIC1);
    s.extend_from_slice(&0xFFFFu16.to_be_bytes());
    s.extend_from_slice(&0x1234u16.to_be_bytes()); // bogus seq
    s.extend_from_slice(&[0xAA; 8]); // bytes belonging to the bogus "frame"
    s.extend_from_slice(&[0x33, 0x44]); // noise
    s.extend_from_slice(&encode_frame(30, b"right-sized").unwrap());
    write(&dir, "04_oversize_recovers.bin", &s);

    // 5) Sequence wraparound with a real gap: 65534, 65535, 0, [missing 1], 2.
    let mut s = Vec::new();
    for seq in [65534u16, 65535, 0, 2] {
        s.extend_from_slice(&encode_frame(seq, format!("seq{seq}").as_bytes()).unwrap());
    }
    write(&dir, "05_seq_wraparound_gap.bin", &s);

    // 6) One frame split into two pieces (half packet): feed them separately.
    let frame = encode_frame(40, b"split across requests").unwrap();
    let cut = frame.len() / 2;
    write(&dir, "06_split_a.bin", &frame[..cut]);
    write(&dir, "06_split_b.bin", &frame[cut..]);

    // 7) Payload that itself contains the magic bytes, back to back.
    let tricky = vec![MAGIC0, MAGIC1, 0, 0, MAGIC1, MAGIC0, MAGIC0, MAGIC1, 0xFF];
    let s = encode_frame(50, &tricky).unwrap();
    write(&dir, "07_payload_has_magic.bin", &s);

    println!("done. feed them with:");
    println!("  curl -s --data-binary @samples/01_good.bin \\");
    println!("       http://127.0.0.1:8080/v1/sessions/demo/feed");
}
