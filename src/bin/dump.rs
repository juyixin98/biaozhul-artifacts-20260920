//! Offline RESP2 frame dumper.
//!
//! Reads RESP bytes from a file (or stdin), parses them *incrementally* —
//! feeding 64-byte chunks to the same [`Parser`] the server uses — and prints
//! one line per complete frame. Useful for inspecting samples and for
//! demonstrating the null-vs-empty-string distinction at top level.
//!
//! Usage:
//!
//! ```text
//! resp-dump [path ...]          # parse files, or stdin if none given
//! resp-dump --hex [path ...]    # also print a hexdump of each frame
//! ```
//!
//! Exit code is 0 if every byte parsed into frames, 1 on a protocol error.

use std::io::Read;

use resp_incremental::{Config, Parser, Poll, Value};

fn main() {
    let mut want_hex = false;
    let mut paths: Vec<String> = Vec::new();
    for a in std::env::args().skip(1) {
        match a.as_str() {
            "--hex" => want_hex = true,
            "-h" | "--help" => {
                println!("resp-dump [--hex] [path ...]   (stdin if no path)");
                return;
            }
            other => paths.push(other.to_string()),
        }
    }

    let mut input: Vec<u8> = Vec::new();
    if paths.is_empty() {
        std::io::stdin().read_to_end(&mut input).expect("read stdin");
        let code = dump(&input, "<stdin>", want_hex);
        std::process::exit(code);
    }

    let mut code = 0;
    for path in &paths {
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .and_then(|mut f| f.read_to_end(&mut bytes))
            .unwrap_or_else(|e| {
                eprintln!("{}: {}", path, e);
                std::process::exit(2);
            });
        println!("== {} ({} bytes)", path, bytes.len());
        let c = dump(&bytes, path, want_hex);
        if c != 0 {
            code = c;
        }
    }
    std::process::exit(code);
}

fn dump(input: &[u8], label: &str, want_hex: bool) -> i32 {
    let mut parser = Parser::new(Config::default());
    let mut frames = 0usize;
    // Emulate network chunking: feed fixed-size pieces.
    const CHUNK: usize = 64;
    let mut index = 0usize;
    loop {
        loop {
            match parser.try_next() {
                Poll::Ready(v) => {
                    frames += 1;
                    print_frame(&v, parser.consumed_bytes(), want_hex);
                }
                Poll::Pending => break,
                Poll::Error(e) => {
                    eprintln!(
                        "{}: fatal parse error after {} frame(s), {} consumed bytes: {}",
                        label, frames, parser.consumed_bytes(), e
                    );
                    return 1;
                }
            }
        }
        if index >= input.len() {
            break;
        }
        let end = (index + CHUNK).min(input.len());
        parser.feed(&input[index..end]).expect("feed before poison");
        index = end;
    }
    let leftover = parser.buffered_len();
    if leftover > 0 {
        eprintln!(
            "{}: {} trailing bytes do not form a complete frame (incomplete input)",
            label, leftover
        );
        return 1;
    }
    println!(
        "{}: {} frame(s), {} bytes consumed",
        label,
        frames,
        parser.consumed_bytes()
    );
    0
}

fn print_frame(v: &Value, total_consumed: usize, want_hex: bool) {
    println!("- {}", describe(v, 0));
    println!("  consumed_total={} bytes", total_consumed);
    if want_hex {
        let wire = v.to_wire().expect("encodable");
        print_hex(&wire);
    }
}

fn describe(v: &Value, indent: usize) -> String {
    let pad = "  ".repeat(indent + 2);
    match v {
        Value::Simple(s) => format!("Simple({:?})", s),
        Value::Error(s) => format!("Error({:?})", s),
        Value::Integer(i) => format!("Integer({})", i),
        Value::Bulk(b) => format!(
            "Bulk(len={}, bytes={:?})",
            b.len(),
            String::from_utf8_lossy(b)
        ),
        Value::Null => "Null".to_string(),
        Value::Array(items) => {
            if items.is_empty() {
                "Array(len=0)".to_string()
            } else {
                let inner: Vec<String> = items
                    .iter()
                    .map(|c| format!("{}{}", pad, describe(c, indent + 1)))
                    .collect();
                format!("Array(len={}) [\n{}\n{}]", items.len(), inner.join(",\n"), "  ".repeat(indent + 1))
            }
        }
    }
}

fn print_hex(bytes: &[u8]) {
    for (i, chunk) in bytes.chunks(16).enumerate() {
        let hex: Vec<String> = chunk.iter().map(|b| format!("{:02x}", b)).collect();
        let ascii: String = chunk
            .iter()
            .map(|&b| if b.is_ascii_graphic() || b == b' ' { b as char } else { '.' })
            .collect();
        println!("  {:04x}  {:48}  {}", i * 16, hex.join(" "), ascii);
    }
}
