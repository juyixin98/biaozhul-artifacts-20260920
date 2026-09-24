//! Offline helper: computes the HMAC an operator must present to clear
//! safe mode. Performs the real HMAC-SHA256 computation locally so the
//! operator never sends the key itself.
//!
//! Usage:
//!   cargo run --bin sign-clearance -- \
//!       --key 'dev-operator-key-change-me' \
//!       --generation 1 \
//!       --challenge <hex-nonce-from-challenge-endpoint>

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut key = String::from("dev-operator-key-change-me");
    let mut generation: Option<String> = None;
    let mut challenge: Option<String> = None;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--key" => {
                key = args[i + 1].clone();
                i += 2;
            }
            "--generation" => {
                generation = Some(args[i + 1].clone());
                i += 2;
            }
            "--challenge" => {
                challenge = Some(args[i + 1].clone());
                i += 2;
            }
            other => {
                eprintln!("unknown argument: {other}");
                std::process::exit(2);
            }
        }
    }

    let (Some(generation), Some(challenge)) = (generation, challenge) else {
        eprintln!("usage: sign-clearance --key <KEY> --generation <N> --challenge <HEX>");
        std::process::exit(2);
    };

    let message = format!("{generation}:{challenge}");
    let mut mac = <HmacSha256 as Mac>::new_from_slice(key.as_bytes())
        .expect("HMAC accepts keys of any length");
    Mac::update(&mut mac, message.as_bytes());
    println!("{}", hex::encode(mac.finalize().into_bytes()));
}
