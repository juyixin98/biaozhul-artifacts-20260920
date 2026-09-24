//! Generate the deterministic demo stream used in the README acceptance flow.
//!
//! ```sh
//! cargo run --example make_sample            # writes examples/sample_stream.bin
//! cargo run --example make_sample -- out.hex  # writes hex text instead
//! ```

use std::fs;
use std::path::PathBuf;

use serial_frame_parser::{sample::build, DEFAULT_MAX_PAYLOAD};

fn hex_encode(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 0xF) as usize] as char);
    }
    s
}

fn main() {
    let data = build(DEFAULT_MAX_PAYLOAD);
    let arg = std::env::args().nth(1);
    match arg.as_deref() {
        Some(path) if path.ends_with(".hex") => {
            fs::write(path, hex_encode(&data)).expect("write hex file");
            println!("wrote {} hex chars to {path}", data.len() * 2);
        }
        Some(path) => {
            fs::write(path, &data).expect("write bin file");
            println!("wrote {} bytes to {path}", data.len());
        }
        None => {
            let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("examples")
                .join("sample_stream.bin");
            fs::write(&path, &data).expect("write sample file");
            let hex = path.with_extension("hex");
            fs::write(&hex, hex_encode(&data)).expect("write hex file");
            println!("wrote {} bytes to {}", data.len(), path.display());
            println!("wrote hex view to    {}", hex.display());
        }
    }
}
