//! Minimal library round-trip demo.
//!
//! Run with: `cargo run --release --example roundtrip`
//!
//! Encodes a short string into the `AC01` container, decodes it back, and
//! prints the coded size, number of rescales and the recovered text.

use aac::{pack, unpack};

fn main() {
    let original = b"adaptive arithmetic coding, streaming, from scratch";

    let (container, enc) = pack(original, None).expect("encode");
    let (decoded, dec) = unpack(&container, None).expect("decode");

    assert_eq!(decoded, original);
    println!("original : {original:?}");
    println!("container: {} bytes ({} coded bits)", container.len(), enc.coded_bits);
    println!("rescales : {} (encode) / {} (decode)", enc.rescales, dec.rescales);
    println!("decoded  : {:?}", String::from_utf8_lossy(&decoded));
}
